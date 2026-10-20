from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PackagingProfile, WeightSet

# PRD 4.2 / Q11: EUR pallet 1200x800x144, ~25 kg; parcels: 20 mm carton allowance; small items go in bins
PACKAGING = [
    PackagingProfile(name="pallet", base_length_m=1.2, base_width_m=0.8, add_height_m=0.144, add_each_side_m=0.0,
                     add_weight_kg=25.0, storage_types=["pallet"]),
    PackagingProfile(name="parcel", base_length_m=0.0, base_width_m=0.0, add_height_m=0.02, add_each_side_m=0.01,
                     add_weight_kg=0.5, storage_types=["parcel", "floor"]),
    PackagingProfile(name="small", base_length_m=0.0, base_width_m=0.0, add_height_m=0.0, add_each_side_m=0.0,
                     add_weight_kg=0.2, storage_types=["bin"]),
]


def seed_defaults(s: Session) -> None:
    for p in PACKAGING:
        if s.get(PackagingProfile, p.name) is None:
            s.add(PackagingProfile(**{c.name: getattr(p, c.name) for c in PackagingProfile.__table__.columns}))
    if s.scalar(select(WeightSet).where(WeightSet.active)) is None:
        s.add(WeightSet(size_fit=25, dwell=25, fragility=25, weight=25, active=True))
