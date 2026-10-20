"""Goods dims (mm/kg, per handling unit) -> handling-unit dims incl. packaging (m/kg)."""
from dataclasses import dataclass

from .models import PackagingProfile


@dataclass(frozen=True)
class UnitDims:
    length_m: float
    width_m: float
    height_m: float
    weight_kg: float


def apply_packaging(p: PackagingProfile, length_mm: float, width_mm: float, height_mm: float,
                    weight_kg: float) -> UnitDims:
    long, short = max(length_mm, width_mm) / 1000, min(length_mm, width_mm) / 1000
    side = 2 * p.add_each_side_m
    return UnitDims(
        length_m=round(max(long + side, p.base_length_m), 3),     # goods may overhang the pallet base
        width_m=round(max(short + side, p.base_width_m), 3),
        height_m=round(height_mm / 1000 + p.add_height_m, 3),
        weight_kg=round(weight_kg + p.add_weight_kg, 2),
    )
