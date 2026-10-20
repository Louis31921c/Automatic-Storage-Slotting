"""Receiving: delivery-note quantities -> units marked delivered -> (optionally) case assigned and booked."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .dimensions import resolve
from .events import emit
from .models import EXTRA, PENDING, Article, DeliveryLine, PoLine
from .settings import settings
from .slotting import SlottingError, confirm_placed, is_active, slot_article


def deliveries(s: Session, line: PoLine) -> list[DeliveryLine]:
    return list(s.scalars(select(DeliveryLine).where(
        DeliveryLine.po_number == line.po_number, DeliveryLine.line_ref == line.line_ref,
        DeliveryLine.cancelled.is_(False)).order_by(DeliveryLine.id)))


def received_qty(s: Session, line: PoLine) -> int:
    total = sum(line.qty_units if d.qty_units is None else d.qty_units for d in deliveries(s, line))
    return min(total, line.qty_units)


def place_on_receipt(s: Session, a: Article) -> str | None:
    """Assign the best case and book it (no operator confirmation). Returns case id, EXTRA, or None (no dims)."""
    if a.state != PENDING or a.slot == EXTRA:
        return a.slot
    if a.length_m is None and resolve(s, a) is None:
        emit(s, "article.needs_dimensions", article_id=a.id, sku=a.sku)
        return None
    try:
        c, _, _ = slot_article(s, a)
    except SlottingError:
        return None
    if c is None:
        return EXTRA
    confirm_placed(s, a)
    return c.id


def sync_receipts(s: Session, line: PoLine) -> int:
    """Mark the first N active units of the line delivered, N = delivered qty. Returns newly received count."""
    s.flush()
    n = received_qty(s, line)
    active = sorted((a for a in line.units if is_active(a)), key=lambda a: a.unit_index)
    now, new = utcnow(), 0
    for i, a in enumerate(active):
        if i < n and a.delivered_at is None:
            a.delivered_at = a.received_at = now
            new += 1
            emit(s, "article.received", article_id=a.id, sku=a.sku, po=line.po_number, line=line.line_ref)
            if settings.auto_place_on_receipt:
                place_on_receipt(s, a)
        elif i >= n and a.delivered_at is not None and a.state == PENDING:
            a.delivered_at = None                      # delivery note row removed/reduced before placement
    return new
