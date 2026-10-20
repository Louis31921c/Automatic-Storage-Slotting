"""'In stock' input: a snapshot of what is physically in the warehouse, optionally with its case.

Typical use: first load from an existing system, or a stock list kept by hand. One row = a quantity of one
SKU (optionally PO + line) in one case. Rows without a case are received and slotted by the engine.
Keyed by PO + line (default PO "STOCK", line = SKU); re-importing the same file gives the same state,
a row removed from the file releases its units. Lines that came from PO/DDT files are never touched here.
"""
from collections import Counter, defaultdict
from typing import Callable

from pydantic import Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .contract import IngestResult
from .db import utcnow
from .events import emit
from .ingest import _Row, normalise_row, row_hash
from .models import EXTRA, PENDING, PLACED, RESERVED, Article, Case, PackagingProfile, PoLine, PurchaseOrder
from .packaging import apply_packaging
from .receiving import place_on_receipt
from .settings import settings
from .slotting import cancel_article, is_active

STOCK_PO = "STOCK"
MARK = "stock:"                              # PoLine.row_hash prefix: line owned by a stock snapshot
PACK_OF_CASE = {"pallet": "pallet", "parcel": "parcel", "floor": "parcel", "bin": "small"}


class StockRow(_Row):
    case_id: str | None = None
    po_number: str | None = None
    line_ref: str | None = None
    supplier: str | None = None
    sku: str
    description: str | None = ""
    qty_units: int = Field(ge=1, le=10_000)
    packaging: str | None = None
    length_mm: float | None = Field(default=None, gt=0, le=20_000)   # goods per unit, excl. packaging
    width_mm: float | None = Field(default=None, gt=0, le=20_000)
    height_mm: float | None = Field(default=None, gt=0, le=20_000)
    weight_kg: float | None = Field(default=None, gt=0, le=5_000)
    length_m: float | None = Field(default=None, gt=0, le=20)         # handling unit incl. packaging
    width_m: float | None = Field(default=None, gt=0, le=20)
    height_m: float | None = Field(default=None, gt=0, le=20)
    unit_kg: float | None = Field(default=None, gt=0, le=5_000)
    fragility: int | None = Field(default=None, ge=0, le=2)


def _unit_dims(s: Session, r: StockRow, packaging: str) -> tuple:
    if None not in (r.length_m, r.width_m, r.height_m, r.unit_kg):
        return r.length_m, r.width_m, r.height_m, r.unit_kg
    if None not in (r.length_mm, r.width_mm, r.height_mm, r.weight_kg):
        u = apply_packaging(s.get(PackagingProfile, packaging), r.length_mm, r.width_mm, r.height_mm, r.weight_kg)
        return u.length_m, u.width_m, u.height_m, u.weight_kg
    return None, None, None, None


def _place_into(s: Session, a: Article, c: Case) -> None:
    """The unit is already physically there: book it as is, flag (not refuse) a second pallet in one position."""
    if c.state != "FREE" and c.storage_type not in settings.shared_storage_types:
        emit(s, "stock.case_conflict", case_id=c.id, sku=a.sku)
    now = utcnow()
    a.state, a.case_id, a.slot, a.placed_at = PLACED, c.id, c.id, now
    a.delivered_at = a.delivered_at or now
    c.state = "OCCUPIED"


def ingest_stock(s: Session, table, key: str, res: IngestResult, seen: set[tuple[str, str]],
                 reject: Callable[..., None]) -> None:
    groups: dict[tuple[str, str], list[tuple[int, StockRow, dict]]] = defaultdict(list)
    for i, raw in enumerate(table.df.to_dict(orient="records"), start=2):
        try:
            r = StockRow.model_validate(normalise_row(raw))
        except (ValidationError, ValueError) as e:
            reject(s, key, i, f"[{table.name}] {e}", raw, res)
            continue
        if r.case_id and s.get(Case, r.case_id) is None:
            reject(s, key, i, f"[{table.name}] unknown case {r.case_id}", raw, res)
            continue
        groups[(r.po_number or STOCK_PO, r.line_ref or r.sku)].append((i, r, raw))

    for (po_no, line_ref), rows in groups.items():
        rh = MARK + row_hash([r.model_dump(mode="json") for _, r, _ in sorted(rows, key=lambda t: t[0])])
        line = s.scalar(select(PoLine).where(PoLine.po_number == po_no, PoLine.line_ref == line_ref))
        if line is not None and not (line.row_hash or "").startswith(MARK):
            for i, _, raw in rows:
                reject(s, key, i, f"[{table.name}] {po_no}/{line_ref} is managed by PO / delivery-note files", raw, res)
            continue
        seen.add((po_no, line_ref))
        if line is not None and line.row_hash == rh and not line.cancelled:
            res.unchanged += 1
            continue
        first = rows[0][1]
        packaging = (first.packaging or "").lower() or (
            PACK_OF_CASE.get(s.get(Case, first.case_id).storage_type, "parcel") if first.case_id else "parcel")
        if packaging not in ("pallet", "parcel", "small"):
            for i, _, raw in rows:
                reject(s, key, i, f"[{table.name}] packaging {packaging!r}", raw, res)
            continue
        if s.get(PurchaseOrder, po_no) is None:
            s.add(PurchaseOrder(po_number=po_no, supplier=first.supplier or "(stock import)"))
        desired = Counter()
        for _, r, _ in rows:
            desired[r.case_id] += r.qty_units
        L, W, H, KG = _unit_dims(s, first, packaging)
        result = "inserted" if line is None else "updated"
        if line is None:
            line = PoLine(po_number=po_no, line_ref=line_ref)
            s.add(line)
        line.sku, line.description, line.packaging = first.sku, first.description or "", packaging
        line.qty_units, line.fragility = sum(desired.values()), first.fragility or 0
        line.length_mm, line.width_mm, line.height_mm, line.weight_kg = (first.length_mm, first.width_mm,
                                                                          first.height_mm, first.weight_kg)
        line.dims_source = "stock_import" if L is not None else None
        line.source_file, line.row_hash, line.cancelled, line.missing_since = key, rh, False, None
        s.flush()

        have: dict[str | None, list[Article]] = defaultdict(list)
        for a in line.units:
            if is_active(a):
                have[a.case_id if a.state in (PLACED, RESERVED) else None].append(a)
        nxt = max((a.unit_index for a in line.units), default=0)
        for cid in set(desired) | set(have):
            cur, want = have.get(cid, []), desired.get(cid, 0)
            for a in sorted(cur, key=lambda a: -a.unit_index)[:max(len(cur) - want, 0)]:
                cancel_article(s, a, "stock snapshot changed")
            for _ in range(want - len(cur)):
                nxt += 1
                a = Article(unit_index=nxt, sku=first.sku, length_m=L, width_m=W, height_m=H, weight_kg=KG,
                            dims_source=line.dims_source, fragility=line.fragility,
                            dwell_days=settings.default_dwell_days, state=PENDING, delivered_at=utcnow(),
                            received_at=utcnow())
                line.units.append(a)
                s.flush()
                if cid is not None:
                    _place_into(s, a, s.get(Case, cid))
                elif settings.auto_place_on_receipt:
                    place_on_receipt(s, a)
        for a in line.units:                    # unplaced units take the (new) dims
            if a.state == PENDING and a.slot in (None, EXTRA):
                a.length_m, a.width_m, a.height_m, a.weight_kg = L, W, H, KG
        emit(s, "stock.line", po=po_no, line=line_ref, sku=first.sku, units=line.qty_units)
        setattr(res, result, getattr(res, result) + 1)


def cancel_missing_stock(s: Session, path: str, seen: set[tuple[str, str]]) -> int:
    from .ingest import cancel_line
    n = 0
    for line in s.scalars(select(PoLine).where(PoLine.source_file == path, PoLine.cancelled.is_(False),
                                               PoLine.row_hash.like(MARK + "%"))):
        if (line.po_number, line.line_ref) not in seen:
            cancel_line(s, line, "row removed from stock file")
            n += 1
    return n

