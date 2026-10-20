"""Slot selection + reservation + placement + release. All policy lives here and in scoring.py."""
from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from . import scoring
from .events import emit
from .models import (CANCELLED, EXTRA, FREE, OCCUPIED, PENDING, PLACED, RESERVED, STAGED, Article, Case,
                     PackagingProfile, SlottingScore, WeightSet)
from .db import utcnow
from .settings import settings


class SlottingError(Exception):
    pass


def active_weights(s: Session) -> WeightSet:
    ws = s.scalar(select(WeightSet).where(WeightSet.active).order_by(WeightSet.id.desc()))
    assert ws is not None
    return ws


def _item(a: Article) -> scoring.Item:
    return scoring.Item(a.length_m, a.width_m, a.height_m, a.weight_kg, a.fragility, a.dwell_days)


def _slot(c: Case) -> scoring.Slot:
    return scoring.Slot(c.id, c.width_m, c.depth_m, c.height_m, c.volume_m3, c.max_kg, c.level_max_kg, c.level,
                        c.max_level, c.dock_dist_m)


def free_now():
    """Empty and not reserved."""
    now = utcnow()
    return (Case.state == FREE) & Case.enabled & or_(Case.reserved_until.is_(None), Case.reserved_until < now)


def available_now():
    """Can take one more unit: empty, or a shared case (room checked in scoring.room_ok). Not reserved."""
    now = utcnow()
    return (Case.enabled & or_(Case.reserved_until.is_(None), Case.reserved_until < now)
            & or_(Case.state == FREE, Case.storage_type.in_(settings.shared_storage_types)))


def policy_for(max_dock_dist_m: float) -> scoring.Policy:
    return scoring.Policy(overhang_m=settings.overhang_m, short_dwell_days=settings.short_dwell_days,
                          max_dock_dist_m=max_dock_dist_m or 1.0, fill_factor=settings.fill_factor,
                          max_skus_per_case=settings.max_skus_per_case)


def case_contents(s: Session) -> dict[str, tuple[float, float, set[str]]]:
    """case id -> (m3, kg, skus) of the units in it (placed or reserved). Aggregated in SQL."""
    live = Article.state.in_([RESERVED, PLACED]) & Article.case_id.is_not(None)
    out: dict[str, tuple[float, float, set[str]]] = {
        cid: (m3 or 0.0, kg or 0.0, set())
        for cid, m3, kg in s.execute(select(Article.case_id, func.sum(Article.length_m * Article.width_m *
                                                                       Article.height_m), func.sum(Article.weight_kg))
                                     .where(live).group_by(Article.case_id))}
    for cid, sku in s.execute(select(Article.case_id, Article.sku).where(live).distinct()):
        out[cid][2].add(sku)
    return out


def case_state(c: Case, contents: dict) -> scoring.CaseState:
    m3, kg, skus = contents.get(c.id, (0.0, 0.0, set()))
    return scoring.CaseState(_slot(c), c.storage_type, c.shelf_id, c.storage_type in settings.shared_storage_types,
                             m3, kg, set(skus), ref=c)


def level_loads(s: Session) -> dict[tuple[str, int], float]:
    """kg already on (or reserved for) each shelf level."""
    q = (select(Case.shelf_id, Case.level, func.sum(Article.weight_kg))
         .join(Article, Article.case_id == Case.id)
         .where(Article.state.in_([RESERVED, PLACED]))
         .group_by(Case.shelf_id, Case.level))
    return {(sh, lv): kg or 0.0 for sh, lv, kg in s.execute(q)}


def candidates(s: Session, art: Article, storage_types: list[str] | None = None,
               buildings: list[int] | None = None, limit: int | None = None):
    """-> [(case, components, total)] ranked, hard constraints applied."""
    if None in (art.length_m, art.width_m, art.height_m, art.weight_kg):
        raise SlottingError("dimensions unknown: resolve them first")
    if storage_types is None:
        prof = s.get(PackagingProfile, art.line.packaging)
        storage_types = prof.storage_types if prof else []
    q = select(Case).where(available_now(), Case.storage_type.in_(storage_types))
    if buildings is None:
        buildings = settings.slot_buildings
    if buildings:
        q = q.where(Case.building.in_(buildings))
    from .shipping import is_blocked_by_pick
    blocked = is_blocked_by_pick(s)
    cases = [c for c in s.scalars(q) if c.id not in blocked]
    if not cases:
        return []
    contents = case_contents(s)
    states = [case_state(c, contents) for c in cases]
    ws = active_weights(s)
    weights = {"size_fit": ws.size_fit, "dwell": ws.dwell, "fragility": ws.fragility, "weight": ws.weight}
    ranked = scoring.rank(_item(art), art.sku, states, level_loads(s), weights,
                          policy_for(max(c.dock_dist_m for c in cases)))
    return [(cs.ref, comps, tot) for cs, comps, tot in (ranked[:limit] if limit else ranked)]


def expire_reservations(s: Session) -> int:
    now = utcnow()
    n = 0
    for a in s.scalars(select(Article).join(Case, Article.case_id == Case.id)
                       .where(Article.state == RESERVED, Case.reserved_until < now)):
        a.state, a.slot, a.case_id = PENDING, None, None
        n += 1
    s.execute(update(Case).where(Case.reserved_until < now).values(reserved_until=None, reserved_for=None))
    if n:
        emit(s, "reservations.expired", count=n)
    return n


def slot_article(s: Session, art: Article):
    """Reserve the best case for the article, or mark it EXTRA. Idempotent while the reservation is alive."""
    now = utcnow()
    if art.state == RESERVED and art.case and art.case.reserved_until and art.case.reserved_until > now \
            and art.case.reserved_for == art.id:
        return art.case, None, []
    if art.state not in (PENDING, RESERVED):
        raise SlottingError(f"article is {art.state}, cannot slot")
    expire_reservations(s)
    ranked = candidates(s, art)
    ws = active_weights(s)
    until = now + timedelta(minutes=settings.reservation_min)
    for rank, (c, comps, tot) in enumerate(ranked[:50]):
        res = s.execute(update(Case).where(Case.id == c.id, available_now())
                        .values(reserved_until=until, reserved_for=art.id))
        if res.rowcount != 1:                 # taken by a concurrent operator, try the next one
            continue
        s.refresh(c)
        art.state, art.slot, art.case_id = RESERVED, c.id, c.id
        for i, (alt, acomps, atot) in enumerate(ranked[rank:rank + 3]):
            s.add(SlottingScore(article_id=art.id, case_id=alt.id, rank=i + 1, total=atot, components=acomps,
                                weight_set_id=ws.id))
        emit(s, "article.reserved", article_id=art.id, case_id=c.id, score=tot)
        alts = [(alt.id, atot, acomps) for alt, acomps, atot in ranked[rank + 1:rank + 4]]
        return c, (tot, comps), alts
    art.state, art.slot, art.case_id = PENDING, EXTRA, None
    emit(s, "article.extra", article_id=art.id, sku=art.sku)
    return None, None, []


def confirm_placed(s: Session, art: Article) -> Case:
    if art.state == PLACED:
        return art.case
    if art.state != RESERVED or art.case is None:
        raise SlottingError(f"article is {art.state}, nothing reserved")
    c = art.case
    shared = c.storage_type in settings.shared_storage_types
    if (c.state != FREE and not shared) or \
            (c.reserved_for not in (None, art.id) and c.reserved_until and c.reserved_until > utcnow()):
        raise SlottingError(f"case {c.id} is no longer available, slot again")
    c.state, c.reserved_until, c.reserved_for = OCCUPIED, None, None
    art.state, art.placed_at = PLACED, utcnow()
    emit(s, "article.placed", article_id=art.id, case_id=c.id)
    return c


def free_case(s: Session, art: Article, reason: str) -> None:
    """Give the article's case back (placed, staged or reserved)."""
    c = art.case
    if c is None:
        return
    was = c.state
    if c.reserved_for == art.id:
        c.reserved_until, c.reserved_for = None, None
    art.case_id = None
    s.flush()
    others = s.scalar(select(func.count()).where(Article.case_id == c.id,
                                                 Article.state.in_([RESERVED, PLACED, STAGED])))
    if others:                                    # shared case: other units stay
        emit(s, "case.unit_removed", case_id=c.id, article_id=art.id, reason=reason, remaining=others)
        return
    c.state, c.reserved_until, c.reserved_for = FREE, None, None
    if was == OCCUPIED:
        emit(s, "shelf.freed", case_id=c.id, article_id=art.id, reason=reason)


def cancel_article(s: Session, art: Article, reason: str) -> None:
    if art.state in (CANCELLED,):
        return
    free_case(s, art, reason)
    art.state, art.slot = CANCELLED, None
    emit(s, "article.cancelled", article_id=art.id, reason=reason)


def is_active(a: Article) -> bool:
    return a.state in (PENDING, RESERVED, PLACED, STAGED)
