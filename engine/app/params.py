"""Quick parameters editable at runtime (input window). Whitelisted, validated, persisted in `params`."""
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import utcnow
from .events import emit
from .models import Param
from .settings import Settings, settings

# key: (type, min, max, label, help) -- help = grey text under the parameter in the window ("" = none)
SPEC: dict[str, tuple[str, float | None, float | None, str, str]] = {
    "auto_place_on_receipt": ("bool", None, None, "Assign a case on delivery note",
                              "Off = cases are assigned at the tablet drop-off scan instead"),
    "slot_buildings": ("buildings", None, None, "Buildings in use", ""),
    "pre_ship_release_h": ("float", 0, 96, "Out for shipping (h)", "Unit leaves its case xx hours before shipping"),
    "default_dwell_days": ("float", 1, 365, "Default dwell time (days)", ""),
    "fresh_hours": ("float", 1, 168, "New add-on", "Hours a delivery stays marked as new"),
}
_DEFAULTS = Settings()


def _check(key: str, value: Any) -> Any:
    kind, lo, hi, label, _ = SPEC[key]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{label}: true/false expected")
        return value
    if kind == "buildings":
        if not isinstance(value, list) or not value or any(b not in (1, 2, 3, 4) for b in value):
            raise ValueError(f"{label}: choose at least one of B1-B4")
        return sorted(set(value))
    v = int(value) if kind == "int" else float(value)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ValueError(f"{label}: must be between {lo} and {hi}")
    return v


def load(s: Session) -> None:
    """Apply stored overrides to the live settings (startup)."""
    for p in s.scalars(select(Param)):
        if p.key in SPEC:
            try:
                setattr(settings, p.key, _check(p.key, p.value))
            except (ValueError, TypeError):
                pass                              # stale/invalid stored value: keep the default


def current() -> list[dict[str, Any]]:
    return [{"key": k, "label": lab, "help": hlp, "type": kind, "min": lo, "max": hi,
             "value": getattr(settings, k), "default": getattr(_DEFAULTS, k)}
            for k, (kind, lo, hi, lab, hlp) in SPEC.items()]


def update(s: Session, values: dict[str, Any]) -> list[dict[str, Any]]:
    unknown = set(values) - set(SPEC)
    if unknown:
        raise ValueError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    checked = {k: _check(k, v) for k, v in values.items()}       # all or nothing
    for k, v in checked.items():
        p = s.get(Param, k)
        if p is None:
            s.add(Param(key=k, value=v, updated_at=utcnow()))
        else:
            p.value, p.updated_at = v, utcnow()
        setattr(settings, k, v)
    if checked:
        emit(s, "params.changed", **checked)
    return current()
