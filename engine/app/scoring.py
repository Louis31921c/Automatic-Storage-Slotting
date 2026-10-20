"""Matrix scoring (docs/matrix_weights.md). Pure functions: no DB access."""
import re
from bisect import bisect_left
from functools import lru_cache
from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Item:
    length_m: float
    width_m: float
    height_m: float
    weight_kg: float
    fragility: int
    dwell_days: float


@dataclass(frozen=True)
class Slot:
    id: str
    width_m: float
    depth_m: float
    height_m: float
    volume_m3: float
    max_kg: float
    level_max_kg: float
    level: int
    max_level: int
    dock_dist_m: float


@dataclass(frozen=True)
class Policy:
    overhang_m: float = 0.05
    short_dwell_days: float = 7.0
    max_dock_dist_m: float = 1.0
    fill_factor: float = 0.8            # shared cases: usable share of the volume (boxes never pack perfectly)
    max_skus_per_case: int = 3
    same_class_m: float = 0.05          # heights within this of the lowest fitting one = "right size"


@dataclass
class CaseState:
    """A case and what it already holds. shared=True: bins/shelving may take several units."""
    slot: Slot
    storage_type: str
    shelf_id: str
    shared: bool
    used_m3: float = 0.0
    used_kg: float = 0.0
    skus: set[str] = field(default_factory=set)
    ref: object = None                  # caller's object (ORM Case)

    @property
    def empty(self) -> bool:
        return not self.skus and self.used_m3 == 0.0


def item_volume(item: Item) -> float:
    return item.length_m * item.width_m * item.height_m


def r4(x: float) -> float:
    """Same rounding as the numpy path (np.round), so both rankings agree exactly."""
    return float(np.round(x, 4))


def fits(item: Item, slot: Slot, overhang_m: float) -> bool:
    if item.height_m > slot.height_m:
        return False
    w, d = slot.width_m + 2 * overhang_m, slot.depth_m + 2 * overhang_m
    a, b = item.length_m, item.width_m
    return (a <= w and b <= d) or (b <= w and a <= d)


def hard_ok(item: Item, slot: Slot, level_load_kg: float, policy: Policy) -> bool:
    return (fits(item, slot, policy.overhang_m)
            and item.weight_kg <= slot.max_kg
            and level_load_kg + item.weight_kg <= slot.level_max_kg)


def room_ok(item: Item, sku: str, cs: CaseState, policy: Policy) -> bool:
    """Shared case: volume (x fill factor), weight and SKU-count limits. Single cases must be empty."""
    if not cs.shared:
        return cs.empty
    if sku not in cs.skus and len(cs.skus) >= policy.max_skus_per_case:
        return False
    return (cs.used_m3 + item_volume(item) <= cs.slot.volume_m3 * policy.fill_factor
            and cs.used_kg + item.weight_kg <= cs.slot.max_kg)


def _level_norm(slot: Slot) -> float:
    return (slot.level - 1) / (slot.max_level - 1) if slot.max_level > 1 else 0.0


def size_fit(item: Item, slot: Slot, keep: float = 1.0, space_fit: float | None = None) -> float:
    """0.4 space + 0.3 height + 0.3 keep.
    space: footprint used (single cases) or volume used after adding the unit (shared cases).
    height: unit height / case height (a low pallet in a tall slot wastes the height above it).
    keep: 1 for the lowest fitting height class; for taller ones, how common that height still is
          (rare tall slots score low so they stay free for the goods that need them)."""
    if space_fit is None:
        area = slot.width_m * slot.depth_m
        space_fit = min(item.length_m * item.width_m / area, 1.0) if area > 0 else 0.0
    height_fit = min(item.height_m / slot.height_m, 1.0) if slot.height_m > 0 else 0.0
    return r4(0.4 * space_fit + 0.3 * height_fit + 0.3 * keep)


def components(item: Item, slot: Slot, policy: Policy, keep: float = 1.0,
               space_fit: float | None = None) -> dict[str, float]:
    ln = _level_norm(slot)
    dn = min(slot.dock_dist_m / policy.max_dock_dist_m, 1.0) if policy.max_dock_dist_m > 0 else 0.0
    access = 1.0 - (0.5 * dn + 0.5 * ln)
    return {
        "size_fit": size_fit(item, slot, keep, space_fit),
        "dwell": r4(access if item.dwell_days <= policy.short_dwell_days else 1.0 - access),
        "fragility": 1.0 if item.fragility <= 0 else r4(1.0 - ln),
        "weight": r4(1.0 - ln * min(item.weight_kg / slot.level_max_kg, 1.0)) if slot.level_max_kg else 0.0,
    }


def total(comps: dict[str, float], weights: dict[str, float]) -> float:
    wsum = sum(weights.values()) or 1.0
    acc = 0.0                       # plain left-to-right sum (builtin sum() is compensated since 3.12,
    for k in weights:               # numpy is not: keep both paths bit-identical)
        acc += weights[k] * comps[k]
    return r4(acc / wsum)


def natural_key(case_id: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.findall(r"\d+|\D+", case_id))


def rank_key(score: float, slot: Slot) -> tuple:
    """Higher score first; tie-break: lowest level, nearest dock, ID ascending (natural)."""
    return (-score, slot.level, slot.dock_dist_m, natural_key(slot.id))


def keep_score(slot_h: float, lowest_fit_h: float, heights_sorted: list[float], policy: Policy) -> float:
    """1.0 if the case is in the lowest height class that fits; else the share of available cases at least
    this tall (tall + rare -> close to 0)."""
    if slot_h <= lowest_fit_h + policy.same_class_m or not heights_sorted:
        return 1.0
    return r4((len(heights_sorted) - bisect_left(heights_sorted, slot_h - 1e-9)) / len(heights_sorted))


def rank_reference(item: Item, sku: str, states: list[CaseState], loads: dict, weights: dict[str, float],
         policy: Policy) -> list[tuple[CaseState, dict[str, float], float]]:
    """Plain-Python version of rank(); kept as the reference the numpy version is tested against."""
    ok = [cs for cs in states
          if room_ok(item, sku, cs, policy)
          and hard_ok(item, cs.slot, loads.get((cs.shelf_id, cs.slot.level), 0.0), policy)]
    if not ok:
        return []
    heights = sorted(cs.slot.height_m for cs in states)       # availability of each height class
    lowest = min(cs.slot.height_m for cs in ok)
    vol = item_volume(item)
    out = []
    for cs in ok:
        space = None
        if cs.shared:
            usable = cs.slot.volume_m3 * policy.fill_factor
            space = min((cs.used_m3 + vol) / usable, 1.0) if usable > 0 else 0.0
            if sku in cs.skus:                                  # same SKU together: easier picking
                space = min(space + 0.1, 1.0)
        comps = components(item, cs.slot, policy, keep_score(cs.slot.height_m, lowest, heights, policy), space)
        out.append((cs, comps, total(comps, weights)))
    out.sort(key=lambda t: rank_key(t[2], t[0].slot))
    return out


@lru_cache(maxsize=None)
def _order_cols(case_id: str) -> tuple[int, int, int, int, int]:
    """Numeric natural-sort key: A{aisle}S{shelf}L{level}P{pos} < FLOOR{n} < SHIP{n}."""
    m = re.match(r"^A(\d+)S(\d+)L(\d+)P(\d+)$", case_id)
    if m:
        return (0, *(int(g) for g in m.groups()))
    m = re.match(r"^([A-Z]+)(\d+)$", case_id)
    return ((1 if m.group(1) < "SHIP" else 2), int(m.group(2)), 0, 0, 0) if m else (3, 0, 0, 0, 0)


class Pool:
    """Vectorised view of candidate cases for fast ranking (static columns + mutable fill)."""

    def __init__(self, states: list[CaseState]):
        self.states = states
        sl = [cs.slot for cs in states]
        f = lambda attr: np.array([getattr(x, attr) for x in sl], dtype=float)  # noqa: E731
        self.w, self.d, self.h, self.vol = f("width_m"), f("depth_m"), f("height_m"), f("volume_m3")
        self.max_kg, self.level_max = f("max_kg"), f("level_max_kg")
        self.level, self.max_level, self.dock = f("level"), f("max_level"), f("dock_dist_m")
        self.shared = np.array([cs.shared for cs in states], dtype=bool)
        self.used_m3 = np.array([cs.used_m3 for cs in states], dtype=float)
        self.used_kg = np.array([cs.used_kg for cs in states], dtype=float)
        self.n_skus = np.array([len(cs.skus) for cs in states], dtype=float)
        self.alive = np.ones(len(states), dtype=bool)
        order = np.array([_order_cols(x.id) for x in sl], dtype=float).reshape(-1, 5)
        self.order = [order[:, k] for k in range(5)]
        self.level_keys = [(cs.shelf_id, cs.slot.level) for cs in states]
        self.heights_sorted = np.sort(self.h)
        self.index = {id(cs): i for i, cs in enumerate(states)}

    def add_unit(self, cs: CaseState, item: Item, sku: str) -> None:
        i = self.index[id(cs)]
        v = item_volume(item)
        cs.used_m3 += v
        cs.used_kg += item.weight_kg
        cs.skus.add(sku)
        self.used_m3[i] += v
        self.used_kg[i] += item.weight_kg
        self.n_skus[i] = len(cs.skus)
        if not cs.shared:
            self.alive[i] = False


def rank(item: Item, sku: str, states: "list[CaseState] | Pool", loads: dict, weights: dict[str, float],
         policy: Policy, limit: int | None = 200) -> list[tuple[CaseState, dict[str, float], float]]:
    """Hard constraints, then matrix score, then tie-break. Shared by live slotting and the demo seed.
    Same formulas as rank_reference(), vectorised; component dicts are built only for the returned top `limit`."""
    P = states if isinstance(states, Pool) else Pool(states)
    if not len(P.states):
        return []
    o = policy.overhang_m
    a, b = item.length_m, item.width_m
    w, d = P.w + 2 * o, P.d + 2 * o
    load = np.array([loads.get(k, 0.0) for k in P.level_keys], dtype=float)
    has_sku = np.array([sku in cs.skus for cs in P.states], dtype=bool)
    vol = item_volume(item)
    empty = (P.used_m3 == 0) & (P.n_skus == 0)
    room = np.where(P.shared,
                    (has_sku | (P.n_skus < policy.max_skus_per_case))
                    & (P.used_m3 + vol <= P.vol * policy.fill_factor)
                    & (P.used_kg + item.weight_kg <= P.max_kg),
                    empty)
    ok = (P.alive & room & (item.height_m <= P.h) & (((a <= w) & (b <= d)) | ((b <= w) & (a <= d)))
          & (item.weight_kg <= P.max_kg) & (load + item.weight_kg <= P.level_max))
    idx = np.nonzero(ok)[0]
    if not len(idx):
        return []
    h = P.h[idx]
    # keep: lowest fitting height class = 1, else share of available cases at least this tall
    heights = np.sort(P.h[P.alive]) if not P.alive.all() else P.heights_sorted
    keep = np.where(h <= h.min() + policy.same_class_m, 1.0,
                    np.round((len(heights) - np.searchsorted(heights, h - 1e-9, "left")) / len(heights), 4))
    area = P.w[idx] * P.d[idx]
    footprint = np.minimum(np.divide(a * b, area, out=np.zeros_like(area), where=area > 0), 1.0)
    usable = P.vol[idx] * policy.fill_factor
    fill = np.minimum(np.divide(P.used_m3[idx] + vol, usable, out=np.zeros_like(usable), where=usable > 0), 1.0)
    fill = np.where(has_sku[idx], np.minimum(fill + 0.1, 1.0), fill)
    space = np.where(P.shared[idx], fill, footprint)
    height_fit = np.minimum(np.divide(item.height_m, h, out=np.zeros_like(h), where=h > 0), 1.0)
    c_size = np.round(0.4 * space + 0.3 * height_fit + 0.3 * keep, 4)
    ml = P.max_level[idx]
    ln = np.where(ml > 1, (P.level[idx] - 1) / np.maximum(ml - 1, 1), 0.0)
    dn = np.minimum(P.dock[idx] / policy.max_dock_dist_m, 1.0) if policy.max_dock_dist_m > 0 else np.zeros(len(idx))
    access = 1.0 - (0.5 * dn + 0.5 * ln)
    c_dwell = np.round(access if item.dwell_days <= policy.short_dwell_days else 1.0 - access, 4)
    c_frag = np.ones(len(idx)) if item.fragility <= 0 else np.round(1.0 - ln, 4)
    lm = P.level_max[idx]
    c_weight = np.where(lm > 0, np.round(1.0 - ln * np.minimum(item.weight_kg / np.where(lm > 0, lm, 1), 1.0), 4), 0.0)
    cols = {"size_fit": c_size, "dwell": c_dwell, "fragility": c_frag, "weight": c_weight}
    wsum = sum(weights.values()) or 1.0
    tot = np.round(sum(weights[k] * cols[k] for k in weights) / wsum, 4)
    keys = [P.order[k][idx] for k in (4, 3, 2, 1, 0)] + [P.dock[idx], P.level[idx], -tot]
    order = np.lexsort(keys)
    if limit:
        order = order[:limit]
    return [(P.states[idx[j]], {k: float(cols[k][j]) for k in cols}, float(tot[j])) for j in order]
