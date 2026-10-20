"""Demo data: fill the warehouse to a target occupancy (default 70%) with realistic stock history.

  .venv/bin/python data/samples/seed_full_warehouse.py [--fill 0.70] [--seed 42]

What it creates (all synthetic, deterministic for a given --seed):
  - supplier POs + delivery notes received over the last ~150 days, one unit per case
  - cases chosen with the engine's own matrix (scoring.hard_ok / components / rank_key, active weights),
    same hard constraints and tie-break as live slotting; leftovers that fit nowhere become EXTRA
  - ~100 PO lines still on order (expected in the next 3 weeks)
  - customer orders + truck planning for the next 14 days, written as Excel files into data/inbox/customer/
    so the watcher ingests them and the engine allocates (LIFO) and applies the 24h rule itself

Stock is written straight to the DB (source_file "seed:demo"): slotting 13k units one by one through the API
would take far too long. The engine must be running for the customer files to be picked up.
Reset everything: stop the engine, delete data/warehouse.db*, restart.
"""
import argparse
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "engine"))

from sqlalchemy import select  # noqa: E402

from app import scoring  # noqa: E402
from app.db import init_db, session_scope, utcnow  # noqa: E402
from app.models import (EXTRA, FREE, OCCUPIED, PENDING, PLACED, Article, Case, DeliveryLine,  # noqa: E402
                        PackagingProfile, PoLine, PurchaseOrder)
from app.packaging import apply_packaging  # noqa: E402
from app.settings import settings  # noqa: E402
from app.slotting import active_weights, case_contents, case_state, level_loads, policy_for  # noqa: E402

TZ = ZoneInfo("Europe/Rome")
SUPPLIERS = ["Ricambi Torino Srl", "Meccanica Valsusa SpA", "Carrozzeria Parts Italia", "Lamiere Piemonte Srl",
             "Fari & Specchi Srl", "Freni Canavese Srl", "Filtri Monviso SpA", "Elettrauto Pinerolo Srl",
             "Gomme & Cerchi Asti", "Vetri Auto Novara", "Sospensioni Biella Srl", "Bulloneria Cuneese"]
CUSTOMERS = ["Autofficina Rossi", "Carrozzeria Bianchi", "Garage Verdi", "Ricambi Est", "Officina Gallo",
             "Carrozzeria Conti", "Autoservice Ferrero", "Moto & Auto Rinaldi", "Officina Bruno", "Elettrauto Greco",
             "Carrozzeria Marino", "Auto Colombo", "Officina Ricci", "Gommista Barbieri", "Autoriparazioni Costa"]

# category: (prefix, names, packaging, length mm, width mm, height mm, kg, fragile share)
CATEGORIES = [
    # small items arrive as inner cartons (one carton = one unit), several cartons share a bin
    ("BLT", ["Bolts M6 carton 10x200", "Bolts M8 carton 10x100", "Nuts M10 carton 10x200", "Washers carton"],
     "small", (280, 420), (200, 320), (150, 300), (6, 15), 0.0),
    ("CLP", ["Trim clips carton 20x100", "Bumper clips carton 20x50", "Cable ties carton 10x500"], "small",
     (300, 450), (200, 330), (150, 280), (2, 6), 0.0),
    ("FUS", ["Fuse assortment carton x12", "Relay set carton x10", "Bulb H7 carton 10x10", "Bulb W5W carton 10x50"],
     "small", (260, 400), (200, 300), (150, 260), (1.5, 5), 0.3),
    ("SNS", ["ABS sensors carton x10", "Lambda probes carton x6", "Parking sensors carton x12",
             "Temperature sensors carton x20"], "small", (280, 420), (200, 320), (150, 300), (1.5, 6), 0.4),
    ("GSK", ["Head gaskets carton x10", "Gasket sets carton x8", "O-ring kits carton x20"], "small",
     (300, 450), (220, 340), (120, 250), (2, 8), 0.0),
    ("HDL", ["Headlamp LED left", "Headlamp LED right", "Headlamp halogen", "Fog lamp"], "parcel",
     (380, 700), (280, 480), (220, 420), (2.5, 9), 0.9),
    ("MRR", ["Door mirror left", "Door mirror right", "Mirror glass"], "parcel",
     (260, 480), (180, 330), (150, 300), (0.6, 3.5), 0.8),
    ("FLT", ["Oil filters box x20", "Air filters box x10", "Cabin filters box x20", "Fuel filters box x12"],
     "parcel", (400, 650), (300, 450), (250, 450), (4, 18), 0.0),
    ("PMP", ["Water pump", "Washer pump kit", "Fuel pump"], "parcel", (250, 500), (200, 380), (180, 380),
     (1.5, 7), 0.1),
    ("BRK", ["Brake pads set x4", "Brake caliper", "Brake disc pair"], "parcel", (300, 600), (250, 450),
     (120, 330), (4, 22), 0.0),
    ("SHK", ["Shock absorber", "Spring pair", "Strut mount kit"], "parcel", (500, 900), (180, 320), (150, 260),
     (3, 12), 0.0),
    ("TLL", ["Tail lamp left", "Tail lamp right", "Indicator set"], "parcel", (300, 560), (200, 360), (150, 330),
     (1, 4), 0.85),
    ("BMP", ["Front bumper primed", "Rear bumper primed"], "pallet", (1600, 1950), (700, 800), (500, 900),
     (60, 140), 0.5),
    ("DOR", ["Front door shell", "Rear door shell", "Tailgate shell"], "pallet", (1100, 1350), (800, 1000),
     (800, 1350), (120, 260), 0.3),
    ("RAD", ["Radiators x6", "Condensers x8", "Intercoolers x6"], "pallet", (1150, 1200), (780, 800),
     (500, 1300), (90, 220), 0.2),
    ("DSC", ["Brake discs x20", "Brake drums x16"], "pallet", (1100, 1200), (750, 800), (400, 850),
     (300, 580), 0.0),
    ("ENG", ["Engine mounts x24", "Gearbox mounts x24", "Control arms x30"], "pallet", (1150, 1200), (760, 800),
     (450, 900), (200, 520), 0.0),
    ("GLS", ["Windscreens x4", "Rear windows x6"], "pallet", (1450, 1650), (950, 1100), (600, 900),
     (150, 300), 1.0),
    ("OIL", ["Engine oil 5W30 x48", "Coolant x60", "Washer fluid x80"], "pallet", (1200, 1200), (800, 800),
     (700, 1100), (380, 600), 0.0),
    ("TYR", ["Tyres 205/55 R16 x16", "Tyres 225/45 R17 x14", "Steel rims x20"], "pallet", (1200, 1200),
     (800, 800), (800, 1400), (150, 300), 0.0),
]
UNITS_PER_LINE = {"small": (2, 24), "parcel": (2, 16), "pallet": (1, 6)}


def make_catalog(rng: random.Random) -> list[dict]:
    cat = []
    for prefix, names, pack, L, W, H, KG, frag in CATEGORIES:
        for i in range(40 if pack != "pallet" else 30):
            name = names[i % len(names)]
            cat.append({
                "sku": f"{prefix}-{1000 + i * 7 + rng.randint(0, 6):04d}", "description": name, "packaging": pack,
                "length_mm": rng.randint(*L), "width_mm": rng.randint(*W), "height_mm": rng.randint(*H),
                "weight_kg": round(rng.uniform(*KG), 1), "fragility": (2 if rng.random() < 0.3 else 1)
                if rng.random() < frag else 0, "supplier": rng.choice(SUPPLIERS)})
    seen, out = set(), []
    for c in cat:                                          # unique SKUs
        if c["sku"] not in seen:
            seen.add(c["sku"])
            out.append(c)
    return out


def local_to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=TZ).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def seed_stock(s, rng: random.Random, fill: float) -> dict:
    """Receive synthetic POs until `fill` of all cases are occupied. Cases come from the engine's planner
    (scoring.rank: same hard constraints, matrix and tie-break as live slotting, shared bins/shelving included)."""
    catalog = make_catalog(rng)
    by_pack = defaultdict(list)
    for c in catalog:
        by_pack[c["packaging"]].append(c)
    profiles = {p.name: p for p in s.scalars(select(PackagingProfile))}
    storage_of = {name: p.storage_types for name, p in profiles.items()}

    cases = list(s.scalars(select(Case).where(Case.enabled, Case.storage_type != "ship")))
    total = len(cases) + settings.ship_slots
    contents = case_contents(s)
    states = [case_state(c, contents) for c in cases
              if c.reserved_until is None and (c.state == FREE or c.storage_type in settings.shared_storage_types)]
    need = max(int(total * fill) - sum(1 for c in cases if c.state == OCCUPIED), 0)
    free_cap = defaultdict(int)
    for c in cases:
        if c.state == FREE:
            free_cap[c.storage_type] += 1
    pack_cap = {p: sum(free_cap[t] for t in storage_of[p]) for p in UNITS_PER_LINE}
    target = {p: int(need * pack_cap[p] / (sum(pack_cap.values()) or 1)) + 1 for p in UNITS_PER_LINE}
    pools = {p: scoring.Pool([cs for cs in states if cs.storage_type in storage_of[p]]) for p in UNITS_PER_LINE}
    policies = {p: policy_for(max((cs.slot.dock_dist_m for cs in pools[p].states), default=1.0))
                for p in UNITS_PER_LINE}
    ws = active_weights(s)
    weights = {"size_fit": ws.size_fit, "dwell": ws.dwell, "fragility": ws.fragility, "weight": ws.weight}
    loads = defaultdict(float, level_loads(s))

    now = utcnow()
    stats = defaultdict(int)
    opened = defaultdict(int)                             # newly occupied cases per packaging
    stuck = defaultdict(int)                              # lines in a row that placed nothing
    po_n = s.scalar(select(PurchaseOrder.po_number).where(PurchaseOrder.po_number.like("PO-2026-S%"))
                    .order_by(PurchaseOrder.po_number.desc()))
    po_n = int(po_n[-4:]) if po_n else 0

    def open_packs():
        return [p for p in UNITS_PER_LINE if opened[p] < target[p] and stuck[p] < 30]

    # POs received between 150 days ago and today (recent weeks busier), until the target occupancy is reached
    while open_packs():
        po_n += 1
        age_days = min(rng.expovariate(1 / 35), 150)
        delivered = now - timedelta(days=age_days, hours=rng.uniform(0, 8))
        if rng.random() < 0.012:                           # a few deliveries in the last 24 h: "fresh"
            delivered = now - timedelta(hours=rng.uniform(0.5, 20))
        po = PurchaseOrder(po_number=f"PO-2026-S{po_n:04d}", supplier=rng.choice(SUPPLIERS),
                           created_at=delivered - timedelta(days=10))
        s.add(po)
        for k in range(1, rng.randint(4, 14) + 1):
            packs = open_packs()
            if not packs:
                break
            pack = rng.choice(packs)
            item = rng.choice(by_pack[pack])
            qty = rng.randint(*UNITS_PER_LINE[pack])
            line = PoLine(po_number=po.po_number, line_ref=str(k * 10), sku=item["sku"],
                          description=item["description"], qty_units=qty, packaging=pack,
                          length_mm=item["length_mm"], width_mm=item["width_mm"], height_mm=item["height_mm"],
                          weight_kg=item["weight_kg"], dims_source="purchase_order", fragility=item["fragility"],
                          expected_date=delivered.replace(hour=0, minute=0, second=0, microsecond=0),
                          source_file="seed:demo", row_hash=f"seed-{po.po_number}-{k}")
            s.add(line)
            s.add(DeliveryLine(dn_number=f"DDT-S{po_n:04d}", po_number=po.po_number, line_ref=line.line_ref,
                               qty_units=qty, delivery_date=delivered, received_at=delivered,
                               source_file="seed:demo", row_hash=f"seed-dn-{po.po_number}-{k}"))
            u = apply_packaging(profiles[pack], item["length_mm"], item["width_mm"], item["height_mm"],
                                item["weight_kg"])
            it = scoring.Item(u.length_m, u.width_m, u.height_m, u.weight_kg, item["fragility"],
                              settings.default_dwell_days)
            pol = policies[pack]
            placed: list = []
            for cs, _, _ in scoring.rank(it, item["sku"], pools[pack], loads, weights, pol):
                while len(placed) < qty and scoring.room_ok(it, item["sku"], cs, pol) and \
                        loads[(cs.shelf_id, cs.slot.level)] + it.weight_kg <= cs.slot.level_max_kg:
                    if cs.empty:
                        opened[pack] += 1
                        cs.ref.state = OCCUPIED
                    pools[pack].add_unit(cs, it, item["sku"])
                    loads[(cs.shelf_id, cs.slot.level)] += it.weight_kg
                    placed.append(cs.ref)
                if len(placed) == qty:
                    break
            stuck[pack] = 0 if placed else stuck[pack] + 1
            for idx in range(qty):
                a = Article(unit_index=idx + 1, sku=item["sku"], length_m=u.length_m, width_m=u.width_m,
                            height_m=u.height_m, weight_kg=u.weight_kg, dims_source="purchase_order",
                            fragility=item["fragility"], dwell_days=settings.default_dwell_days,
                            received_at=delivered, delivered_at=delivered)
                if idx < len(placed):
                    c = placed[idx]
                    a.state, a.slot, a.case_id, a.placed_at = PLACED, c.id, c.id, delivered + timedelta(minutes=20)
                    stats[f"units_{c.storage_type}"] += 1
                else:
                    a.state, a.slot = PENDING, EXTRA
                    stats["EXTRA"] += 1
                line.units.append(a)
        s.flush()
    stats.update({f"cases_{p}": opened[p] for p in opened})

    # still on order: expected in the next 3 weeks, no delivery note yet
    for _ in range(14):
        po_n += 1
        supplier = rng.choice(SUPPLIERS)
        po = PurchaseOrder(po_number=f"PO-2026-S{po_n:04d}", supplier=supplier, created_at=now - timedelta(days=3))
        s.add(po)
        expected = now + timedelta(days=rng.randint(1, 21))
        for k in range(1, rng.randint(4, 10)):
            item = rng.choice(catalog)
            pack = item["packaging"]
            qty = rng.randint(*UNITS_PER_LINE[pack])
            u = apply_packaging(profiles[pack], item["length_mm"], item["width_mm"], item["height_mm"],
                                item["weight_kg"])
            line = PoLine(po_number=po.po_number, line_ref=str(k * 10), sku=item["sku"],
                          description=item["description"], qty_units=qty, packaging=pack,
                          length_mm=item["length_mm"], width_mm=item["width_mm"], height_mm=item["height_mm"],
                          weight_kg=item["weight_kg"], dims_source="purchase_order", fragility=item["fragility"],
                          expected_date=expected.replace(hour=0, minute=0, second=0, microsecond=0),
                          source_file="seed:demo", row_hash=f"seed-{po.po_number}-{k}")
            s.add(line)
            for idx in range(qty):
                line.units.append(Article(unit_index=idx + 1, sku=item["sku"], length_m=u.length_m, width_m=u.width_m,
                                          height_m=u.height_m, weight_kg=u.weight_kg, dims_source="purchase_order",
                                          fragility=item["fragility"], dwell_days=settings.default_dwell_days,
                                          state=PENDING, received_at=now))
            stats["ordered_units"] += qty
    stats["target_total"] = int(total * fill)
    stats["total_cases"] = total
    return dict(stats)


def write_customer_files(s, rng: random.Random, out_dir: Path, tag: str) -> tuple[Path, Path]:
    """Orders for SKUs in stock over the next 14 days + the trucks that carry them (local Europe/Rome times)."""
    stock = defaultdict(int)
    for sku, in s.execute(select(Article.sku).where(Article.state == PLACED, Article.order_line_id.is_(None))):
        stock[sku] += 1
    skus = [k for k, v in stock.items() if v >= 2]
    today = datetime.now(TZ).replace(tzinfo=None, minute=0, second=0, microsecond=0)
    trucks, orders = [], []
    docks = [f"Dock_EastHall_{i}" for i in range(1, 7)] + [f"Dock_B1_{i}" for i in range(1, 5)]
    for d in range(1, 15):
        day = (today + timedelta(days=d)).replace(hour=0)
        if day.weekday() == 6:
            continue
        for t in range(3 if day.weekday() < 5 else 1):
            dep = day + timedelta(hours=9 + 4 * t)
            trucks.append({"Truck Ref": f"T-{dep:%m%d}-{t + 1}", "Carrier": rng.choice(["Bartolini", "TNT", "SDA", "Arcese",
                                                                                         "GLS", "DHL"]),
                           "Direction": "out", "Dock": rng.choice(docks), "Arrival": dep - timedelta(hours=2),
                           "Departure": dep})
    n = 0
    for d in range(1, 15):
        day = (today + timedelta(days=d)).replace(hour=0)
        day_trucks = [t for t in trucks if t["Departure"].date() == day.date()]
        if not day_trucks:
            continue
        for _ in range(6 if d == 1 else 22):              # first day light: SHIP area holds 40 units
            n += 1
            truck = rng.choice(day_trucks) if rng.random() < 0.7 else None
            ship = truck["Departure"] if truck else (day if rng.random() < 0.4 else day + timedelta(hours=rng.randint(8, 17)))
            customer = rng.choice(CUSTOMERS)                # one customer per order
            for li in range(1, rng.randint(2, 4)):
                sku = rng.choice(skus)
                orders.append({"Order Number": f"C-{tag}-{n:03d}", "Line": li, "Customer": customer,
                               "SKU": sku, "Qty": rng.randint(1, min(2, stock[sku])), "Ship Date": ship,
                               "Truck": truck["Truck Ref"] if truck else None})
    out_dir.mkdir(parents=True, exist_ok=True)
    tp, cp = out_dir / f"truck_planning_seed_{tag}.xlsx", out_dir / f"customer_purchase_order_seed_{tag}.xlsx"
    pd.DataFrame(trucks).to_excel(tp, index=False)
    pd.DataFrame(orders).to_excel(cp, index=False)
    return tp, cp


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fill", type=float, default=0.70)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--no-orders", action="store_true", help="skip customer/truck files (benchmarking)")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    init_db()
    with session_scope() as s:
        if s.scalar(select(Case.id).limit(1)) is None:     # fresh DB without the engine having run
            from app.storage_import import import_storage
            import_storage(s)
    with session_scope() as s:
        print("stock:", seed_stock(s, rng, a.fill))
    if a.no_orders:
        sys.exit(0)
    with session_scope() as s:
        tp, cp = write_customer_files(s, rng, ROOT / "data" / "inbox" / "customer", f"{datetime.now():%y%m%d}")
    print("customer files for the watcher:", tp.name, cp.name)
