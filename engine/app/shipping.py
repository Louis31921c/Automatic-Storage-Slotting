"""Customer-order allocation (LIFO) + 24h pre-ship release rule + loading."""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import utcnow
from .events import emit
from .models import OCCUPIED, PENDING, PLACED, SHIPPED, STAGED, Article, Case, OrderLine, Truck
from .settings import settings
from .slotting import free_case


def effective_ship_at(s: Session, ol: OrderLine) -> datetime | None:
    """Truck departure wins over the order's ship date (truck planning is the most precise source)."""
    if ol.truck_ref:
        t = s.get(Truck, ol.truck_ref)
        if t and not t.cancelled and t.departure_at:
            return t.departure_at
    return ol.ship_at


def _unstage(s: Session, a: Article, reason: str) -> None:
    """Order gone after staging: free the SHIP slot, article goes back to dropoff for re-slotting."""
    free_case(s, a, reason)
    a.state, a.slot, a.staged_from, a.picked_at, a.ship_at = PENDING, None, None, None, None
    emit(s, "article.unstaged", article_id=a.id, reason=reason)


def deallocate(s: Session, a: Article, reason: str) -> None:
    a.order_line_id, a.ship_at = None, None
    if a.state == STAGED:
        _unstage(s, a, reason)


def allocate(s: Session) -> int:
    """Match open order lines to stored units of the same SKU, newest first (LIFO)."""
    n = 0
    for ol in s.scalars(select(OrderLine)):
        units = list(s.scalars(select(Article).where(Article.order_line_id == ol.id,
                                                     Article.state.in_([PLACED, STAGED, SHIPPED]))
                               .order_by(Article.received_at.asc())))
        if ol.cancelled:
            for a in units:
                if a.state != SHIPPED:
                    deallocate(s, a, "order cancelled")
            continue
        ship_at = effective_ship_at(s, ol)
        for a in units:
            if a.state != SHIPPED and a.ship_at != ship_at:
                a.ship_at = ship_at
        extra = len(units) - ol.qty_units
        for a in [u for u in units if u.state == PLACED][:max(extra, 0)]:
            deallocate(s, a, "order qty reduced")
        need = ol.qty_units - len(units)
        if need <= 0 or ol.fulfilled:
            continue
        free_units = s.scalars(select(Article).where(Article.sku == ol.sku, Article.state == PLACED,
                                                     Article.order_line_id.is_(None))
                               .order_by(Article.received_at.desc(), Article.id.desc()).limit(need))
        for a in free_units:
            a.order_line_id, a.ship_at = ol.id, ship_at
            emit(s, "article.allocated", article_id=a.id, order=ol.order_number, line=ol.line_ref)
            n += 1
    return n


def _ship_slot(s: Session) -> Case | None:
    from .scoring import natural_key
    from .slotting import free_now
    free = list(s.scalars(select(Case).where(Case.storage_type == "ship", free_now())))
    return min(free, key=lambda c: natural_key(c.id)) if free else None


def apply_pre_ship_rule(s: Session, now: datetime | None = None) -> int:
    """At ship_at - 24h: PLACED -> STAGED, rack case FREE, unit slotted in the SHIP area."""
    now = now or utcnow()
    horizon = timedelta(hours=settings.pre_ship_release_h)
    n = 0
    for a in s.scalars(select(Article).where(Article.state == PLACED, Article.ship_at.is_not(None),
                                             Article.order_line_id.is_not(None))):
        if now < a.ship_at - horizon:
            continue
        src = a.case_id
        free_case(s, a, "pre-ship 24h")
        a.state, a.staged_from = STAGED, src
        ship = _ship_slot(s)
        if ship is None:
            a.slot = None
            emit(s, "ship.full", article_id=a.id)
        else:
            ship.state = OCCUPIED
            a.case_id, a.slot = ship.id, ship.id
        emit(s, "article.staged", article_id=a.id, from_case=src, ship_slot=a.slot)
        n += 1
    # ship time moved out of the window after staging -> needs re-handling (alert only)
    for a in s.scalars(select(Article).where(Article.state == STAGED, Article.ship_at.is_not(None))):
        if now < a.ship_at - horizon:
            emit(s, "article.rehandle", article_id=a.id, ship_at=a.ship_at.isoformat() + "Z")
    return n


def is_blocked_by_pick(s: Session) -> set[str]:
    """Rack cases freed by the 24h rule whose unit has not physically been picked yet."""
    return set(s.scalars(select(Article.staged_from).where(Article.state == STAGED, Article.picked_at.is_(None),
                                                            Article.staged_from.is_not(None))))


def confirm_picked(s: Session, a: Article) -> None:
    if a.state != STAGED:
        raise ValueError(f"article is {a.state}, not staged")
    if a.picked_at is None:
        a.picked_at = utcnow()
        emit(s, "article.picked", article_id=a.id, from_case=a.staged_from, ship_slot=a.slot)


def confirm_loaded(s: Session, a: Article) -> None:
    if a.state != STAGED:
        raise ValueError(f"article is {a.state}, not staged")
    a.picked_at = a.picked_at or utcnow()
    free_case(s, a, "loaded")
    a.state, a.shipped_at = SHIPPED, utcnow()
    emit(s, "article.shipped", article_id=a.id)
    ol = a.order_line
    if ol is not None:
        shipped = s.scalar(select(func.count()).where(Article.order_line_id == ol.id, Article.state == SHIPPED))
        if shipped >= ol.qty_units:
            ol.fulfilled = True
            emit(s, "order.fulfilled", order=ol.order_number, line=ol.line_ref)

