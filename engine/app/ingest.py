"""Ingest pipeline: input tables (tables.py, headers.py) -> validate -> idempotent upsert by business key.

Excel is input, DB is truth. Rows are keyed by business key (PO#+line, order#+line, truck ref), never by
row index; row hashes skip unchanged rows; rows gone from a file are cancelled (shelves released).
"""
import hashlib
import json
import math
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .contract import FileEvent, IngestResult, PoLineIn, PurchaseOrderIn
from .db import utcnow
from .dimensions import apply_to_units
from .events import emit
from .models import (EXTRA, PENDING, RESERVED, Article, DeadLetter, DeliveryLine, FileState, OrderLine, PoLine,
                     PurchaseOrder, Truck)
from .receiving import deliveries, sync_receipts
from .settings import settings
from .slotting import cancel_article, free_case, is_active

# ---------------------------------------------------------------- value parsing
def _clean(v: Any) -> Any:
    if v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def parse_num(v: Any) -> float | None:
    v = _clean(v)
    if v is None or isinstance(v, (int, float)):
        return v
    t = str(v).replace(" ", "").replace(" ", "")
    if "," in t and "." in t:                       # 1.234,5 (IT) or 1,234.5 (EN)
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    return float(t)


def _plain(v: Any) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(v).strip().lower())
    return "".join(c for c in t if not unicodedata.combining(c))


PACKAGING_WORDS = {
    "pallet": {"pallet", "palette", "palet", "plt", "pal", "bancale", "pedana", "epal", "eur pallet", "europallet",
               "palett", "palle"},
    "parcel": {"parcel", "colis", "carton", "box", "collo", "colli", "scatola", "cartone", "pacco", "package",
               "paquet", "karton", "caja", "case"},
    "small": {"small", "small item", "petit", "piccolo", "minuteria", "bin", "sachet", "bag", "busta", "inner",
              "inner carton"},
}


def parse_packaging(v: Any) -> str:
    t = _plain(v)
    forms = {t, t[:-1] if t.endswith("s") else t}       # plurals: pallets, cartons
    for name, words in PACKAGING_WORDS.items():
        if forms & words:
            return name
    return t                                            # unknown: the row validator reports it


def parse_fragility(v: Any) -> int | None:
    v = _clean(v)
    if v is None:
        return None
    t = _plain(v)
    if t in {"y", "yes", "true", "si", "x", "fragile", "1", "oui", "o", "ja", "fragil", "fragilissimo"}:
        return 1 if t != "fragilissimo" else 2
    if t in {"very", "very fragile", "2", "molto", "molto fragile", "tres fragile", "sehr"}:
        return 2
    if t in {"n", "no", "false", "0", "-", "non", "nein"}:
        return 0
    raise ValueError(f"fragility: unknown value {v!r}")


def local_to_utc(v: Any, default_hour: int | None = None) -> datetime | None:
    """Excel dates are local (Europe/Rome); stored naive UTC. Date-only -> default hour."""
    v = _clean(v)
    if v is None:
        return None
    if not isinstance(v, datetime):
        v = pd.to_datetime(str(v), dayfirst=True).to_pydatetime()
    if default_hour is not None and v.time() == time(0, 0):
        v = v.replace(hour=default_hour)
    if v.tzinfo is None:
        v = v.replace(tzinfo=ZoneInfo(settings.tz))
    return v.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def row_hash(row: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- row schemas (Excel)
class _Row(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def _c(cls, v):
        return _clean(v)


class DnRow(_Row):
    dn_number: str
    po_number: str
    line_ref: str
    length_mm: float | None = Field(default=None, gt=0, le=20_000)
    width_mm: float | None = Field(default=None, gt=0, le=20_000)
    height_mm: float | None = Field(default=None, gt=0, le=20_000)
    weight_kg: float | None = Field(default=None, gt=0, le=5_000)
    fragility: int | None = Field(default=None, ge=0, le=2)
    qty_units: int | None = Field(default=None, ge=0, le=10_000)       # delivered qty (blank = whole line)
    delivery_date: datetime | None = None


class OrderRow(_Row):
    order_number: str
    line_ref: str
    customer: str = ""
    sku: str
    qty_units: int = Field(ge=0, le=10_000)
    ship_at: datetime | None = None
    truck_ref: str | None = None


class TruckRow(_Row):
    truck_ref: str
    carrier: str = ""
    direction: str = "out"
    dock: str | None = None
    arrival_at: datetime | None = None
    departure_at: datetime | None = None


def _as_str(v: Any) -> Any:
    """Excel gives 10.0 for a line '10': keep business keys as clean strings."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v) if isinstance(v, (int, float)) else v


KEYS = {"po_number", "line_ref", "dn_number", "order_number", "truck_ref", "sku", "case_id"}
NUMS = {"length_mm", "width_mm", "height_mm", "weight_kg", "qty_units", "length_m", "width_m", "height_m", "unit_kg"}
DATES = {"expected_date": None, "delivery_date": None, "ship_at": "ship", "arrival_at": None, "departure_at": None}


def normalise_row(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw.items():
        v = _clean(v)
        if k in KEYS:
            v = _as_str(v)
            v = v.strip() if isinstance(v, str) else v
        elif k in NUMS:
            v = parse_num(v)
        elif k == "fragility":
            v = parse_fragility(v)
        elif k in DATES:
            v = local_to_utc(v, settings.default_ship_hour if DATES[k] == "ship" else None)
        elif k == "packaging" and isinstance(v, str):
            v = parse_packaging(v)
        elif k == "direction" and isinstance(v, str):
            t = _plain(v)
            v = "in" if t.startswith(("in", "unload", "arr", "entr", "ingress", "recep", "ricev", "ein")) else "out"
        out[k] = v
    return out


# ---------------------------------------------------------------- PO lines
def _sync_units(s: Session, line: PoLine) -> int:
    """Create / cancel handling units so active units == qty_units. Returns cancelled count."""
    active = [a for a in line.units if is_active(a)]
    have = {a.unit_index for a in line.units}
    nxt = max(have, default=0)
    for _ in range(line.qty_units - len(active)):
        nxt += 1
        line.units.append(Article(unit_index=nxt, sku=line.sku, state=PENDING, fragility=line.fragility,
                                  dwell_days=settings.default_dwell_days))
    cancelled = 0
    if len(active) > line.qty_units:
        # drop the least advanced units first (pending before placed), highest index first
        order = {PENDING: 0, RESERVED: 1}
        for a in sorted(active, key=lambda a: (order.get(a.state, 2), -a.unit_index))[:len(active) - line.qty_units]:
            cancel_article(s, a, "qty reduced")
            cancelled += 1
    s.flush()
    apply_to_units(s, line)
    return cancelled


def _reslot_unassigned(s: Session, line: PoLine) -> None:
    """Row edited: units not placed yet must be slotted again with the new data."""
    for a in line.units:
        if a.state == RESERVED:
            free_case(s, a, "line edited")
            a.state, a.slot = PENDING, None
        elif a.state == PENDING and a.slot == EXTRA:
            a.slot = None


def cancel_line(s: Session, line: PoLine, reason: str) -> int:
    line.cancelled = True
    n = 0
    for a in line.units:
        if is_active(a):
            cancel_article(s, a, reason)
            n += 1
    emit(s, "po_line.cancelled", po=line.po_number, line=line.line_ref, reason=reason)
    return n


def upsert_po_line(s: Session, po_number: str, supplier: str, li: PoLineIn, source_file: str | None,
                   rhash: str) -> str:
    po = s.get(PurchaseOrder, po_number)
    if po is None:
        po = PurchaseOrder(po_number=po_number, supplier=supplier)
        s.add(po)
    elif supplier and po.supplier != supplier:
        po.supplier = supplier
    line = s.scalar(select(PoLine).where(PoLine.po_number == po_number, PoLine.line_ref == li.line_ref))
    has_dims = None not in (li.length_mm, li.width_mm, li.height_mm, li.weight_kg)
    if line is None:
        line = PoLine(po_number=po_number, line_ref=li.line_ref, sku=li.sku, description=li.description,
                      qty_units=li.qty_units, packaging=li.packaging, length_mm=li.length_mm, width_mm=li.width_mm,
                      height_mm=li.height_mm, weight_kg=li.weight_kg,
                      dims_source="purchase_order" if has_dims else None, fragility=li.fragility or 0,
                      expected_date=li.expected_date, source_file=source_file, row_hash=rhash)
        s.add(line)
        s.flush()
        _sync_units(s, line)
        emit(s, "po_line.new", po=po_number, line=li.line_ref, sku=li.sku, units=li.qty_units)
        apply_dn_info(s, line)                 # DDT that arrived before the PO
        sync_receipts(s, line)
        return "inserted"
    line.missing_since = None
    if line.row_hash == rhash and not line.cancelled:
        if source_file and line.source_file != source_file:
            line.source_file = source_file
        return "unchanged"
    if line.source_file and source_file and line.source_file != source_file:
        emit(s, "ingest.conflict", po=po_number, line=li.line_ref, old_file=line.source_file, new_file=source_file)
    line.sku, line.description, line.qty_units, line.packaging = li.sku, li.description, li.qty_units, li.packaging
    if has_dims:
        line.length_mm, line.width_mm, line.height_mm, line.weight_kg = (li.length_mm, li.width_mm, li.height_mm,
                                                                         li.weight_kg)
        line.dims_source = "purchase_order"
    if li.fragility is not None:
        line.fragility = li.fragility
    line.expected_date, line.source_file, line.row_hash, line.cancelled = li.expected_date, source_file, rhash, False
    _reslot_unassigned(s, line)
    _sync_units(s, line)
    sync_receipts(s, line)
    emit(s, "po_line.updated", po=po_number, line=li.line_ref)
    return "updated"


def ingest_purchase_order(s: Session, po: PurchaseOrderIn, source_file: str | None = None) -> IngestResult:
    res = IngestResult(kind="purchase_order")
    for li in po.lines:
        r = upsert_po_line(s, po.po_number, po.supplier, li, source_file, row_hash(li.model_dump(mode="json")))
        setattr(res, r, getattr(res, r) + 1)
    return res


# ---------------------------------------------------------------- other kinds
def apply_dn_info(s: Session, line: PoLine) -> bool:
    """Dims (only where the PO had none, chain PO -> DN) and fragility from this line's delivery notes."""
    changed = False
    for dl in deliveries(s, line):
        dims = (dl.length_mm, dl.width_mm, dl.height_mm, dl.weight_kg)
        if line.dims_source in (None, "delivery_note") and None not in dims and \
                (line.length_mm, line.width_mm, line.height_mm, line.weight_kg) != dims:
            line.length_mm, line.width_mm, line.height_mm, line.weight_kg = dims
            line.dims_source = "delivery_note"
            changed = True
        if dl.fragility is not None and dl.fragility != line.fragility:
            line.fragility = dl.fragility
            changed = True
    if changed:
        _reslot_unassigned(s, line)
        apply_to_units(s, line)
        emit(s, "po_line.updated", po=line.po_number, line=line.line_ref, source="delivery_note")
    return changed


def apply_delivery_note(s: Session, r: DnRow, source_file: str | None = None, rhash: str = "") -> str:
    """DN line = goods physically received. Keyed by DN# + PO# + line. A DDT may arrive before its PO:
    it is kept and applied when the PO line shows up."""
    dl = s.scalar(select(DeliveryLine).where(DeliveryLine.dn_number == r.dn_number,
                                             DeliveryLine.po_number == r.po_number,
                                             DeliveryLine.line_ref == r.line_ref))
    if dl is not None and dl.row_hash == rhash and not dl.cancelled:
        return "unchanged"
    result = "inserted" if dl is None else "updated"
    if dl is None:
        dl = DeliveryLine(dn_number=r.dn_number, po_number=r.po_number, line_ref=r.line_ref, received_at=utcnow())
        s.add(dl)
    dl.qty_units, dl.delivery_date, dl.source_file, dl.row_hash, dl.cancelled = (
        r.qty_units, r.delivery_date, source_file, rhash, False)
    dl.length_mm, dl.width_mm, dl.height_mm, dl.weight_kg, dl.fragility = (
        r.length_mm, r.width_mm, r.height_mm, r.weight_kg, r.fragility)
    s.flush()
    line = s.scalar(select(PoLine).where(PoLine.po_number == r.po_number, PoLine.line_ref == r.line_ref))
    if line is None:
        emit(s, "delivery_line.unmatched", dn=r.dn_number, po=r.po_number, line=r.line_ref)
        return result
    apply_dn_info(s, line)
    sync_receipts(s, line)
    return result


def upsert_order_line(s: Session, r: OrderRow, source_file: str, rhash: str) -> str:
    ol = s.scalar(select(OrderLine).where(OrderLine.order_number == r.order_number, OrderLine.line_ref == r.line_ref))
    if ol is None:
        s.add(OrderLine(order_number=r.order_number, line_ref=r.line_ref, customer=r.customer, sku=r.sku,
                        qty_units=r.qty_units, ship_at=r.ship_at, truck_ref=r.truck_ref, source_file=source_file,
                        row_hash=rhash))
        emit(s, "order_line.new", order=r.order_number, line=r.line_ref, sku=r.sku)
        return "inserted"
    ol.missing_since = None
    if ol.row_hash == rhash and not ol.cancelled:
        ol.source_file = source_file
        return "unchanged"
    if ol.ship_at != r.ship_at:
        emit(s, "order_line.ship_time_changed", order=r.order_number, line=r.line_ref)
    ol.customer, ol.sku, ol.qty_units, ol.ship_at, ol.truck_ref = r.customer, r.sku, r.qty_units, r.ship_at, r.truck_ref
    ol.source_file, ol.row_hash, ol.cancelled = source_file, rhash, False
    emit(s, "order_line.updated", order=r.order_number, line=r.line_ref)
    return "updated"


def upsert_truck(s: Session, r: TruckRow, source_file: str, rhash: str) -> str:
    t = s.get(Truck, r.truck_ref)
    if t is None:
        s.add(Truck(truck_ref=r.truck_ref, carrier=r.carrier, direction=r.direction, dock=r.dock,
                    arrival_at=r.arrival_at, departure_at=r.departure_at, source_file=source_file, row_hash=rhash))
        emit(s, "truck.new", truck=r.truck_ref)
        return "inserted"
    t.missing_since = None
    if t.row_hash == rhash and not t.cancelled:
        t.source_file = source_file
        return "unchanged"
    t.carrier, t.direction, t.dock, t.arrival_at, t.departure_at = (r.carrier, r.direction, r.dock, r.arrival_at,
                                                                     r.departure_at)
    t.source_file, t.row_hash, t.cancelled = source_file, rhash, False
    emit(s, "truck.updated", truck=r.truck_ref)
    return "updated"


# ---------------------------------------------------------------- files
def _reject(s: Session, path: str, rownum: int, err: str, raw: dict[str, Any], res: IngestResult) -> None:
    clean = {str(k): _clean(v) for k, v in raw.items()}
    s.add(DeadLetter(source_file=path, row_number=rownum, error=err[:1000],
                     raw=json.loads(json.dumps(clean, default=str))))
    res.rejected += 1


def _ingest_row(s: Session, kind: str, row: dict[str, Any], key: str, rh: str) -> tuple[str, tuple[str, str]]:
    if kind == "purchase_order":
        li = PoLineIn.model_validate({k: v for k, v in row.items() if k in PoLineIn.model_fields})
        if li.packaging not in ("pallet", "parcel", "small"):
            raise ValueError(f"packaging {li.packaging!r}")
        po_number = row.get("po_number")
        if not po_number:
            raise ValueError("po_number missing")
        return upsert_po_line(s, po_number, row.get("supplier") or "", li, key, rh), (po_number, li.line_ref)
    if kind == "delivery_note":
        dn = DnRow.model_validate(row)
        return apply_delivery_note(s, dn, key, rh), (f"{dn.dn_number}|{dn.po_number}", dn.line_ref)
    if kind == "customer_order":
        o = OrderRow.model_validate(row)
        return upsert_order_line(s, o, key, rh), (o.order_number, o.line_ref)
    t = TruckRow.model_validate(row)
    return upsert_truck(s, t, key, rh), (t.truck_ref, "")


def ingest_file(s: Session, path: Path, force: bool = False) -> IngestResult:
    """Every table of the file (sheets / PDF tables) is detected and ingested on its own, so one workbook
    can carry POs, delivery notes, stock, orders and trucks together."""
    from .stock_snapshot import ingest_stock
    from .tables import ORDER, is_own_export, read_tables
    path = path.resolve()
    key = str(path)
    if is_own_export(path):
        res = IngestResult(kind="rejected")
        _reject(s, key, 0, "this is the engine's own status spreadsheet: it is an output, never an input", {}, res)
        return res
    h = file_hash(path)
    fs = s.get(FileState, key)
    tables = read_tables(path)
    known = sorted((t for t in tables if t.kind), key=lambda t: ORDER.index(t.kind))
    kinds = sorted({t.kind for t in known}, key=ORDER.index)
    res = IngestResult(kind="+".join(kinds) or "unknown",
                       tables=[{"name": t.name, "kind": t.kind, "rows": len(t.df)} for t in tables])
    if not known:
        _reject(s, key, 0, "no recognised table: " + "; ".join(f"{t.name}: {t.raw_headers[:8]}" for t in tables), {}, res)
        return res
    if fs is not None and fs.sha256 == h and not force:
        res.skipped_file = True
        return res

    seen: dict[str, set[tuple[str, str]]] = {k: set() for k in kinds}
    for t in known:
        if t.kind == "stock":
            ingest_stock(s, t, key, res, seen["stock"], _reject)
            continue
        for i, raw in enumerate(t.df.to_dict(orient="records"), start=2):     # row 1 = header
            try:
                row = normalise_row(raw)
                r, bk = _ingest_row(s, t.kind, row, key, row_hash(row))
                seen[t.kind].add(bk)
                setattr(res, r, getattr(res, r) + 1)
            except (ValidationError, ValueError) as e:
                _reject(s, key, i, f"[{t.name}] {e}", raw, res)

    for k in kinds:
        res.cancelled += _cancel_missing(s, k, key, seen[k])
    if fs is None:
        s.add(FileState(path=key, sha256=h, kind=res.kind, ingested_at=utcnow()))
    else:
        fs.sha256, fs.kind, fs.ingested_at = h, res.kind, utcnow()
    emit(s, "ingest.file", path=key, **res.model_dump())
    return res


def _cancel_missing(s: Session, kind: str, path: str, seen: set[tuple[str, str]]) -> int:
    """Rows that were in this file before but are gone now -> cancelled."""
    n = 0
    if kind == "purchase_order":
        for line in s.scalars(select(PoLine).where(PoLine.source_file == path, PoLine.cancelled.is_(False))
                              .where(~func.coalesce(PoLine.row_hash, "").like("stock:%"))):   # stock lines: own kind
            if (line.po_number, line.line_ref) not in seen:
                cancel_line(s, line, "row deleted")
                n += 1
    elif kind == "delivery_note":
        for dl in s.scalars(select(DeliveryLine).where(DeliveryLine.source_file == path,
                                                       DeliveryLine.cancelled.is_(False))):
            if (f"{dl.dn_number}|{dl.po_number}", dl.line_ref) not in seen:
                dl.cancelled = True
                line = s.scalar(select(PoLine).where(PoLine.po_number == dl.po_number,
                                                     PoLine.line_ref == dl.line_ref))
                if line is not None:
                    sync_receipts(s, line)
                emit(s, "delivery_line.cancelled", dn=dl.dn_number, po=dl.po_number, line=dl.line_ref)
                n += 1
    elif kind == "stock":
        from .stock_snapshot import cancel_missing_stock
        n += cancel_missing_stock(s, path, seen)
    elif kind == "customer_order":
        for ol in s.scalars(select(OrderLine).where(OrderLine.source_file == path, OrderLine.cancelled.is_(False))):
            if (ol.order_number, ol.line_ref) not in seen:
                ol.cancelled = True
                emit(s, "order_line.cancelled", order=ol.order_number, line=ol.line_ref)
                n += 1
    elif kind == "truck_planning":
        for t in s.scalars(select(Truck).where(Truck.source_file == path, Truck.cancelled.is_(False))):
            if (t.truck_ref, "") not in seen:
                t.cancelled = True
                emit(s, "truck.cancelled", truck=t.truck_ref)
                n += 1
    return n


def handle_file_event(s: Session, ev: FileEvent) -> IngestResult:
    src = str(Path(ev.path).resolve())
    if ev.event == "deleted":
        now = utcnow()
        for model in (PoLine, OrderLine, Truck):
            s.execute(update(model).where(model.source_file == src, model.missing_since.is_(None))
                      .values(missing_since=now))
        fs = s.get(FileState, src)
        if fs is not None:
            s.delete(fs)
        emit(s, "ingest.file_deleted", path=src)
        return IngestResult(kind="deleted")
    if ev.event == "moved":
        dst = str(Path(ev.dest_path or "").resolve())
        for model in (PoLine, OrderLine, Truck):
            s.execute(update(model).where(model.source_file == src).values(source_file=dst, missing_since=None))
        fs = s.get(FileState, src)
        if fs is not None:
            s.delete(fs)
        s.flush()
        emit(s, "ingest.file_moved", path=src, dest=dst)
        return ingest_file(s, Path(dst))
    return ingest_file(s, Path(src))


def sweep_orphans(s: Session) -> int:
    """Rows whose file vanished longer than the rename grace ago -> cancelled."""
    limit = utcnow() - timedelta(seconds=settings.rename_grace_s)
    n = 0
    for line in s.scalars(select(PoLine).where(PoLine.missing_since < limit, PoLine.cancelled.is_(False))):
        cancel_line(s, line, "file deleted")
        n += 1
    for ol in s.scalars(select(OrderLine).where(OrderLine.missing_since < limit, OrderLine.cancelled.is_(False))):
        ol.cancelled = True
        emit(s, "order_line.cancelled", order=ol.order_number, line=ol.line_ref, reason="file deleted")
        n += 1
    for t in s.scalars(select(Truck).where(Truck.missing_since < limit, Truck.cancelled.is_(False))):
        t.cancelled = True
        n += 1
    return n
