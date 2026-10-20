"""Import data/storage_spaces.json (FreeCAD macro output) into the cases table.

IDs are never regenerated here: existing cases keep their state, new IDs are added,
IDs missing from the file are disabled (not deleted) so history stays consistent.
"""
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .events import emit
from .models import Case
from .settings import settings

SHIP_W, SHIP_D, SHIP_H = 1.3, 1.3, 2.5


@lru_cache(maxsize=1)
def load_storage(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or settings.storage_json).read_text())


def _dock_dist(rect: list[float] | None, docks: list[dict[str, Any]]) -> float:
    if not rect or not docks:
        return 0.0
    cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
    return round(min(math.hypot(cx - d["x"], cy - d["y"]) for d in docks), 2)


def import_storage(s: Session, path: Path | None = None) -> dict[str, int]:
    load_storage.cache_clear()
    data = load_storage(path)
    max_level: dict[str, int] = {}
    for c in data["cases"]:
        max_level[c["rack"]] = max(max_level.get(c["rack"], 1), c["level"])

    rows = list(data["cases"])
    for n in range(1, settings.ship_slots + 1):           # shipping area: logical only (PRD Q21)
        rows.append({"id": f"SHIP{n}", "shelf_id": f"SHIP{n}", "building": 2, "rack": "SHIP", "storage_type": "ship",
                     "aisle": None, "shelf": None, "level": 1, "pos": 1, "width_m": SHIP_W, "depth_m": SHIP_D,
                     "height_m": SHIP_H, "volume_m3": round(SHIP_W * SHIP_D * SHIP_H, 3), "max_kg": 1250,
                     "level_max_kg": 1250, "z_m": 0.0, "rect": None})

    existing = {c.id: c for c in s.scalars(select(Case))}
    seen, added, updated = set(), 0, 0
    fields = ["shelf_id", "building", "rack", "storage_type", "aisle", "shelf", "level", "pos", "width_m", "depth_m",
              "height_m", "volume_m3", "max_kg", "level_max_kg", "z_m", "rect"]
    for r in rows:
        seen.add(r["id"])
        vals = {f: r[f] for f in fields}
        vals["dock_dist_m"] = _dock_dist(r["rect"], data["docks"])
        vals["max_level"] = max_level.get(r["rack"], 1)
        c = existing.get(r["id"])
        if c is None:
            s.add(Case(id=r["id"], **vals))
            added += 1
        else:
            if any(getattr(c, k) != v for k, v in vals.items()) or not c.enabled:
                for k, v in vals.items():
                    setattr(c, k, v)
                c.enabled = True
                updated += 1
    disabled = 0
    for cid, c in existing.items():
        if cid not in seen and c.enabled:
            c.enabled = False
            disabled += 1
    result = {"added": added, "updated": updated, "disabled": disabled, "total": len(seen)}
    emit(s, "storage.imported", **result)
    return result
