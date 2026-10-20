import asyncio
import contextlib
import logging
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import events, params
from .contract import (CONTRACT_VERSION, ArticleOut, Candidate, CaseOut, ComponentHealth, DimensionsRequest,
                       DimensionsResult, EventOut, FileEvent, Health, IngestResult, LocationOut, LookupOut, MapOut,
                       MapRack, PurchaseOrderIn, SlotRequest, SlotResult, Weights)
from .db import get_session, init_db, session_scope, utcnow
from .dimensions import resolve
from .export_xlsx import write_export
from .ingest import handle_file_event, ingest_purchase_order, sweep_orphans
from .models import (EXTRA, OCCUPIED, PENDING, PLACED, RESERVED, STAGED, Article, Case, DeadLetter, Event, OrderLine,
                     PoLine, Truck, WeightSet)
from .settings import settings
from .shipping import allocate, apply_pre_ship_rule, confirm_loaded, confirm_picked
from .slotting import (SlottingError, active_weights, candidates, confirm_placed, expire_reservations, free_now,
                       slot_article)
from .storage_import import import_storage, load_storage
from .tables import summary as table_summary

log = logging.getLogger("engine")


# ---------------------------------------------------------------- lifecycle
def tick(s: Session) -> dict[str, int]:
    """Scheduler pass (every 15 min + after every file change)."""
    return {"expired": expire_reservations(s), "orphans": sweep_orphans(s), "allocated": allocate(s),
            "staged": apply_pre_ship_rule(s)}


async def _scheduler() -> None:
    while True:
        await asyncio.sleep(settings.scheduler_interval_s)
        try:
            with session_scope() as s:
                await asyncio.to_thread(tick, s)
        except Exception:  # never let the loop die
            log.exception("scheduler tick failed")


def _export_now() -> None:
    with session_scope() as s:
        write_export(s)


async def _exporter(dirty: asyncio.Event) -> None:
    """Regenerate the status spreadsheet after changes (debounced). Separate from the ingest path."""
    while True:
        await dirty.wait()
        await asyncio.sleep(settings.export_debounce_s)
        dirty.clear()
        try:
            await asyncio.to_thread(_export_now)
        except Exception:  # e.g. disk full: keep serving, retry on next change
            log.exception("spreadsheet export failed")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with session_scope() as s:
        params.load(s)
        if s.scalar(select(func.count()).select_from(Case)) == 0 and settings.storage_json.exists():
            log.info("importing storage spaces: %s", import_storage(s))
    loop = asyncio.get_running_loop()
    events.bind_loop(loop)
    tasks = [asyncio.create_task(_scheduler())]
    if settings.export_enabled:
        dirty = asyncio.Event()
        dirty.set()                                      # initial export at startup
        events.on_change(lambda: loop.call_soon_threadsafe(dirty.set))
        tasks.append(asyncio.create_task(_exporter(dirty)))
    yield
    events.clear_on_change()
    for t in tasks:
        t.cancel()


app = FastAPI(title="Warehouse slotting engine", version=CONTRACT_VERSION, lifespan=lifespan)


# ---------------------------------------------------------------- serialisation
def utc(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt else None


def article_out(a: Article) -> ArticleOut:
    ln = a.line
    reserved = a.case.reserved_until if (a.state == RESERVED and a.case) else None
    return ArticleOut(id=a.id, code=a.code, po_number=ln.po_number, line_ref=ln.line_ref, unit_index=a.unit_index,
                      qty_units=ln.qty_units, sku=a.sku, description=ln.description, packaging=ln.packaging,
                      length_m=a.length_m, width_m=a.width_m, height_m=a.height_m, weight_kg=a.weight_kg,
                      dims_source=a.dims_source, fragility=a.fragility, dwell_days=a.dwell_days, state=a.state,
                      slot=a.slot, reserved_until=utc(reserved), placed_at=utc(a.placed_at), ship_at=utc(a.ship_at))


def case_out(c: Case) -> CaseOut:
    return CaseOut(id=c.id, shelf_id=c.shelf_id, building=c.building, rack=c.rack, storage_type=c.storage_type,
                   level=c.level, pos=c.pos, width_m=c.width_m, depth_m=c.depth_m, height_m=c.height_m,
                   volume_m3=c.volume_m3, max_kg=c.max_kg, state=c.state, rect=c.rect, dock_dist_m=c.dock_dist_m)


def get_article(s: Session, article_id: int) -> Article:
    a = s.get(Article, article_id)
    if a is None:
        raise HTTPException(404, f"article {article_id} not found")
    return a


def _conflict(e: Exception) -> HTTPException:
    return HTTPException(409, str(e))


# ---------------------------------------------------------------- health + events
def _db_health() -> ComponentHealth:
    try:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(settings.db_path) as con:
            con.execute("SELECT 1")
        return ComponentHealth(ok=True, detail=str(settings.db_path.name))
    except sqlite3.Error as e:
        return ComponentHealth(ok=False, detail=str(e))


def _watcher_health() -> ComponentHealth:
    hb = settings.run_dir / "watcher.heartbeat"
    if not hb.exists():
        return ComponentHealth(ok=False, detail="no heartbeat")
    age = time.time() - hb.stat().st_mtime
    if age > settings.watcher_stale_after_s:
        return ComponentHealth(ok=False, detail=f"heartbeat {age:.0f}s old")
    return ComponentHealth(ok=True, detail=f"heartbeat {age:.0f}s ago")


@app.get("/health", response_model=Health)
def health() -> Health:
    db, watcher = _db_health(), _watcher_health()
    return Health(status="ok" if db.ok and watcher.ok else "degraded", db=db, watcher=watcher)


@app.get("/events")
async def stream_events() -> StreamingResponse:
    """SSE: every state change (article placed, shelf freed, new delivery, ...)."""
    q = events.subscribe()

    async def gen():
        try:
            yield f"event: hello\ndata: {{\"contract_version\": \"{CONTRACT_VERSION}\"}}\n\n"
            while True:
                try:
                    yield await asyncio.wait_for(q.get(), timeout=settings.sse_ping_s)
                except TimeoutError:
                    yield ": ping\n\n"
        finally:
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/events/recent", response_model=list[EventOut])
def recent_events(limit: int = Query(50, le=500), s: Session = Depends(get_session)):
    return [EventOut(id=e.id, ts=utc(e.ts), type=e.type, data=e.data)
            for e in s.scalars(select(Event).order_by(Event.id.desc()).limit(limit))]


# ---------------------------------------------------------------- ingest
@app.post("/ingest/purchase_order", response_model=IngestResult)
def ingest_po(po: PurchaseOrderIn, s: Session = Depends(get_session)):
    return ingest_purchase_order(s, po)


class InspectRequest(BaseModel):
    path: str


@app.post("/ingest/inspect")
def ingest_inspect(req: InspectRequest):
    """What datasets a file holds (sheets / PDF tables), without ingesting it."""
    p = Path(req.path)
    if not p.is_file():
        raise HTTPException(404, f"file not found: {req.path}")
    try:
        return table_summary(p)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/ingest/file", response_model=IngestResult)
def ingest_file_event(ev: FileEvent, s: Session = Depends(get_session)):
    """Called by the watcher after debounce. Path must be a local file the engine can read."""
    try:
        res = handle_file_event(s, ev)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:                       # locked by Excel on a share: watcher retries
        raise HTTPException(423, str(e))
    s.flush()
    tick(s)
    return res


# ---------------------------------------------------------------- dimensions + slotting
@app.post("/resolve/dimensions", response_model=DimensionsResult)
def resolve_dimensions(req: DimensionsRequest, s: Session = Depends(get_session)):
    a = get_article(s, req.article_id)
    if a.state not in (PENDING, RESERVED):
        raise HTTPException(409, f"article is {a.state}")
    source = resolve(s, a, req.manual)
    s.flush()
    return DimensionsResult(article_id=a.id, resolved=source is not None, source=source,
                            needs_manual=source is None, article=article_out(a))


@app.post("/slot", response_model=SlotResult)
def slot(req: SlotRequest, s: Session = Depends(get_session)):
    a = get_article(s, req.article_id)
    if a.delivered_at is None and a.state == PENDING:     # scanned at drop-off = physically here
        a.delivered_at = a.received_at = utcnow()
    if None in (a.length_m, a.width_m, a.height_m, a.weight_kg):
        resolve(s, a)
        s.flush()
    try:
        c, best, alts = slot_article(s, a)
    except SlottingError as e:
        raise _conflict(e)
    s.flush()
    return SlotResult(
        article=article_out(a), slot=c.id if c else EXTRA, case=case_out(c) if c else None,
        reserved_until=utc(c.reserved_until) if c else None,
        score=Candidate(case_id=c.id, total=best[0], components=best[1]) if (c and best) else None,
        alternatives=[Candidate(case_id=i, total=t, components=k) for i, t, k in alts])


@app.get("/shelves/free", response_model=list[CaseOut])
def shelves_free(article_id: int | None = None, storage_type: str | None = None, building: int | None = None,
                 min_height_m: float = 0.0, limit: int = Query(50, le=1000), s: Session = Depends(get_session)):
    """Capacity-filtered candidates. With article_id: hard constraints + ranking for that article."""
    if article_id is not None:
        a = get_article(s, article_id)
        try:
            ranked = candidates(s, a, storage_types=[storage_type] if storage_type else None,
                                buildings=[building] if building else None, limit=limit)
        except SlottingError as e:
            raise _conflict(e)
        return [case_out(c) for c, _, _ in ranked]
    q = select(Case).where(free_now(), Case.height_m >= min_height_m)
    if storage_type:
        q = q.where(Case.storage_type == storage_type)
    if building:
        q = q.where(Case.building == building)
    return [case_out(c) for c in s.scalars(q.order_by(Case.level, Case.dock_dist_m).limit(limit))]


@app.get("/cases", response_model=list[CaseOut])
def list_cases(rack: str | None = None, aisle: int | None = None, limit: int = Query(2000, le=5000),
               s: Session = Depends(get_session)):
    """Cases of one rack (labels, overrides)."""
    q = select(Case)
    if rack:
        q = q.where(Case.rack == rack)
    if aisle is not None:
        q = q.where(Case.aisle == aisle)
    return [case_out(c) for c in s.scalars(q.order_by(Case.shelf, Case.level, Case.pos, Case.id).limit(limit))]


# ---------------------------------------------------------------- articles
@app.get("/articles", response_model=list[ArticleOut])
def list_articles(state: str | None = None, slot: str | None = None, limit: int = Query(100, le=1000),
                  s: Session = Depends(get_session)):
    q = select(Article).order_by(Article.id.desc()).limit(limit)
    if state:
        q = q.where(Article.state.in_(state.split(",")))
    if slot:
        q = q.where(Article.slot == slot)
    return [article_out(a) for a in s.scalars(q)]


@app.get("/articles/{article_id}", response_model=ArticleOut)
def get_article_route(article_id: int, s: Session = Depends(get_session)):
    return article_out(get_article(s, article_id))


@app.get("/articles/{article_id}/location", response_model=LocationOut)
def article_location(article_id: int, s: Session = Depends(get_session)):
    a = get_article(s, article_id)
    return LocationOut(article=article_out(a), case=case_out(a.case) if a.case else None)


@app.post("/articles/{article_id}/placed", response_model=LocationOut)
def article_placed(article_id: int, s: Session = Depends(get_session)):
    a = get_article(s, article_id)
    try:
        c = confirm_placed(s, a)
    except SlottingError as e:
        raise _conflict(e)
    s.flush()
    tick(s)                      # may allocate it to an open order right away
    return LocationOut(article=article_out(a), case=case_out(c))


@app.post("/articles/{article_id}/picked", response_model=LocationOut)
def article_picked(article_id: int, s: Session = Depends(get_session)):
    a = get_article(s, article_id)
    try:
        confirm_picked(s, a)
    except ValueError as e:
        raise _conflict(e)
    return LocationOut(article=article_out(a), case=case_out(a.case) if a.case else None)


@app.post("/articles/{article_id}/loaded", response_model=ArticleOut)
def article_loaded(article_id: int, s: Session = Depends(get_session)):
    a = get_article(s, article_id)
    try:
        confirm_loaded(s, a)
    except ValueError as e:
        raise _conflict(e)
    return article_out(a)


@app.get("/lookup/{code}", response_model=LookupOut)
def lookup(code: str, s: Session = Depends(get_session)):
    """Scanned code -> units. Accepts unit code U123, 'PO-LINE' (e.g. PO-2026-0012-10) or SKU."""
    code = code.strip()
    arts: list[Article] = []
    if code[:1] in "Uu" and code[1:].isdigit():
        a = s.get(Article, int(code[1:]))
        arts = [a] if a else []
    if not arts:
        po, _, line = code.rpartition("-")
        if po:
            arts = list(s.scalars(select(Article).join(PoLine).where(PoLine.po_number == po, PoLine.line_ref == line)))
    if not arts:
        arts = list(s.scalars(select(Article).where(Article.sku == code)))
    rank = {RESERVED: 0, PENDING: 1, STAGED: 2, PLACED: 3}
    arts.sort(key=lambda a: (rank.get(a.state, 9), a.unit_index))
    return LookupOut(code=code, articles=[article_out(a) for a in arts[:50]])


# ---------------------------------------------------------------- map
@app.get("/map", response_model=MapOut)
def warehouse_map(s: Session = Depends(get_session)):
    data = load_storage(settings.storage_json)
    occ = dict(s.execute(select(Case.rack, func.count()).where(Case.state == OCCUPIED).group_by(Case.rack)).all())
    xs = [v for w in data["walls"] for v in (w[0], w[2])] + [v for r in data["racks"] for v in (r["rect"][0], r["rect"][2])]
    ys = [v for w in data["walls"] for v in (w[1], w[3])] + [v for r in data["racks"] for v in (r["rect"][1], r["rect"][3])]
    floor = [{"id": c.id, "rect": c.rect, "state": c.state}
             for c in s.scalars(select(Case).where(Case.storage_type == "floor"))]
    return MapOut(bounds=[min(xs), min(ys), max(xs), max(ys)],
                  racks=[MapRack(aisle=r["aisle"], rack=r["rack"], building=r["building"],
                                 storage_type=r["storage_type"], rect=r["rect"], cases=r["cases"],
                                 occupied=occ.get(r["rack"], 0)) for r in data["racks"]],
                  floor=floor, docks=data["docks"], walls=data["walls"])


# ---------------------------------------------------------------- pick + trucks
def _center(rect: list[float]) -> tuple[float, float]:
    return (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2


@app.get("/pick/orders")
def pick_orders(s: Session = Depends(get_session)):
    """Open orders with units to pick (STAGED, not yet moved) or to load (moved to SHIP area)."""
    rows = s.execute(select(OrderLine.order_number, OrderLine.customer, Article)
                     .join(Article, Article.order_line_id == OrderLine.id)
                     .where(Article.state == STAGED)).all()
    out: dict[str, dict] = {}
    for order, customer, a in rows:
        o = out.setdefault(order, {"order_number": order, "customer": customer, "to_pick": 0, "to_load": 0,
                                   "ship_at": None, "truck_ref": a.order_line.truck_ref})
        o["to_pick" if a.picked_at is None else "to_load"] += 1
        if a.ship_at and (o["ship_at"] is None or a.ship_at < o["ship_at"]):
            o["ship_at"] = a.ship_at
    res = sorted(out.values(), key=lambda o: (o["ship_at"] or datetime.max, o["order_number"]))
    for o in res:
        o["ship_at"] = utc(o["ship_at"])
    return res


@app.get("/pick/orders/{order_number}")
def pick_route(order_number: str, s: Session = Depends(get_session)):
    """One order at a time (Q33); stops ordered by nearest neighbour from the nearest dock."""
    arts = list(s.scalars(select(Article).join(OrderLine, Article.order_line_id == OrderLine.id)
                          .where(OrderLine.order_number == order_number, Article.state == STAGED)))
    if not arts:
        raise HTTPException(404, f"nothing staged for order {order_number}")
    data = load_storage(settings.storage_json)
    to_pick = [a for a in arts if a.picked_at is None]
    stops = []
    for a in to_pick:
        src = s.get(Case, a.staged_from) if a.staged_from else None
        stops.append((a, src))
    docks = [(d["x"], d["y"]) for d in data["docks"]] or [(0.0, 0.0)]
    located = [(a, c) for a, c in stops if c is not None and c.rect]
    pos = min(docks, key=lambda d: min((abs(d[0] - _center(c.rect)[0]) + abs(d[1] - _center(c.rect)[1])
                                        for _, c in located), default=0.0))
    route = []
    while located:
        nxt = min(located, key=lambda t: abs(pos[0] - _center(t[1].rect)[0]) + abs(pos[1] - _center(t[1].rect)[1]))
        located.remove(nxt)
        pos = _center(nxt[1].rect)
        route.append(nxt)
    route += [(a, c) for a, c in stops if c is None or not c.rect]
    return {
        "order_number": order_number,
        "to_pick": [{"article": article_out(a), "from_case": case_out(c) if c else None, "ship_slot": a.slot}
                    for a, c in route],
        "to_load": [{"article": article_out(a), "ship_slot": a.slot} for a in arts if a.picked_at is not None],
    }


@app.get("/trucks")
def trucks(s: Session = Depends(get_session)):
    """Truck schedule; manifest = STAGED units of the orders on that truck."""
    out = []
    for t in s.scalars(select(Truck).where(Truck.cancelled.is_(False))
                       .order_by(Truck.departure_at.is_(None), Truck.departure_at, Truck.arrival_at)):
        manifest = list(s.scalars(select(Article).join(OrderLine, Article.order_line_id == OrderLine.id)
                                  .where(OrderLine.truck_ref == t.truck_ref, Article.state == STAGED)))
        out.append({"truck_ref": t.truck_ref, "carrier": t.carrier, "direction": t.direction, "dock": t.dock,
                    "arrival_at": utc(t.arrival_at), "departure_at": utc(t.departure_at),
                    "manifest": [{**article_out(a).model_dump(), "picked": a.picked_at is not None} for a in manifest]})
    return out


# ---------------------------------------------------------------- admin
@app.get("/admin/params")
def get_params():
    return params.current()


@app.put("/admin/params")
def put_params(values: dict[str, Any], s: Session = Depends(get_session)):
    try:
        return params.update(s, values)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e))


@app.get("/admin/weights", response_model=Weights)
def get_weights(s: Session = Depends(get_session)):
    ws = active_weights(s)
    return Weights(size_fit=ws.size_fit, dwell=ws.dwell, fragility=ws.fragility, weight=ws.weight)


@app.put("/admin/weights", response_model=Weights)
def put_weights(w: Weights, s: Session = Depends(get_session)):
    if abs(w.size_fit + w.dwell + w.fragility + w.weight - 100) > 0.01:
        raise HTTPException(422, "weights must sum to 100")
    for old in s.scalars(select(WeightSet).where(WeightSet.active)):
        old.active = False
    s.add(WeightSet(**w.model_dump(), active=True))
    events.emit(s, "weights.changed", **w.model_dump())
    return w


@app.get("/admin/extra", response_model=list[ArticleOut])
def extra_queue(s: Session = Depends(get_session)):
    return [article_out(a) for a in s.scalars(select(Article).where(Article.slot == EXTRA, Article.state == PENDING)
                                               .order_by(Article.id))]


@app.post("/admin/cases/{case_id}/enabled", response_model=CaseOut)
def set_case_enabled(case_id: str, enabled: bool, s: Session = Depends(get_session)):
    c = s.get(Case, case_id)
    if c is None:
        raise HTTPException(404, f"case {case_id} not found")
    c.enabled = enabled
    events.emit(s, "case.override", case_id=case_id, enabled=enabled)
    return case_out(c)


@app.get("/admin/dead_letter")
def dead_letter(limit: int = Query(100, le=1000), s: Session = Depends(get_session)):
    return [{"id": d.id, "ts": utc(d.ts), "source_file": d.source_file, "row_number": d.row_number,
             "error": d.error, "raw": d.raw}
            for d in s.scalars(select(DeadLetter).order_by(DeadLetter.id.desc()).limit(limit))]


@app.get("/admin/stats")
def stats(s: Session = Depends(get_session)):
    cases = dict(s.execute(select(Case.state, func.count()).where(Case.enabled).group_by(Case.state)).all())
    arts = dict(s.execute(select(Article.state, func.count()).group_by(Article.state)).all())
    extra = s.scalar(select(func.count()).where(Article.slot == EXTRA, Article.state == PENDING))
    dl = s.scalar(select(func.count()).select_from(DeadLetter))
    return {"cases": cases, "articles": arts, "extra": extra, "dead_letter": dl}


@app.get("/export.xlsx")
def export_xlsx():
    """Fresh status spreadsheet (Ordered / In stock / Shipments)."""
    path = settings.export_path.with_name("download_" + settings.export_path.name)
    with session_scope() as s:
        write_export(s, path)
    return FileResponse(path, filename="warehouse_status.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/admin/tick")
def run_tick(s: Session = Depends(get_session)):
    return tick(s)


@app.post("/admin/import_storage")
def run_import_storage(s: Session = Depends(get_session)):
    return import_storage(s)
