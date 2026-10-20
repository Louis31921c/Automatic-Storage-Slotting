"""Synthetic sample files (PRD Q9: "make it up"). Deterministic for a given --date.

  .venv/bin/python data/samples/generate_samples.py [--date 2026-10-19] [--out data/samples]

Writes 4 Excel files shaped like what humans will drop in the inbox folders:
  supplier_purchase_order_<date>.xlsx   PO lines, goods dims per handling unit (excl. pallet), some blanks
  delivery_note_<date>.xlsx             goods received (~60% of lines, some partial) + missing dims (DDT style)
  customer_purchase_order_<date>.xlsx   customer lines, ship dates (some date-only), truck refs
  truck_planning_<date>.xlsx            inbound + outbound trucks, docks, ETA/ETD (local time)
Messy on purpose: comma decimals, padded strings, a bad qty, a blank line, mixed-case packaging.
"""
import argparse
import random
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

SUPPLIERS = ["Ricambi Torino Srl", "Meccanica Valsusa SpA", "Carrozzeria Parts Italia", "Lamiere Piemonte Srl",
             "Fari & Specchi Srl"]
CATALOG = [  # sku, description, packaging, (l, w, h) mm per unit, kg, fragile
    ("BRK-DSC-2041", "Brake discs, front, set x10", "pallet", (1150, 780, 900), 410, ""),
    ("BRK-PAD-0882", "Brake pads, carton x40", "pallet", (1200, 800, 1100), 520, ""),
    ("BMP-FR-1307", "Front bumper, primed", "pallet", (1900, 800, 700), 95, "Y"),
    ("HDL-L-5512", "Headlamp LED, left", "parcel", (620, 420, 380), 7.5, "Y"),
    ("HDL-R-5513", "Headlamp LED, right", "parcel", (620, 420, 380), 7.5, "Y"),
    ("MRR-L-0761", "Door mirror, left, heated", "parcel", (450, 300, 260), 3.2, "very"),
    ("DOR-RR-2210", "Rear door shell", "pallet", (1350, 1000, 1250), 160, ""),
    ("RAD-CL-4410", "Radiator, cooling, x6", "pallet", (1200, 800, 1300), 140, ""),
    ("FLT-OIL-0091", "Oil filters, box x50", "parcel", (600, 400, 400), 18, ""),
    ("CLP-TRM-0007", "Trim clips, bag x100", "small", (300, 200, 120), 0.8, ""),
    ("BLT-M8-0120", "Bolts M8x120, box x200", "small", (250, 180, 100), 4.0, ""),
    ("WSH-PMP-3301", "Washer pump", "small", (220, 160, 140), 0.6, ""),
    ("GLS-WS-9001", "Windscreen, laminated", "pallet", (1600, 1100, 900), 260, "very"),
    ("ENG-MNT-6620", "Engine mounts x12", "pallet", (1200, 800, 650), 300, ""),
    ("SHK-ABS-1150", "Shock absorbers x16", "pallet", (1200, 800, 1000), 380, ""),
]


def num(v: float, comma: bool) -> str | float:
    return f"{v:.1f}".replace(".", ",") if comma else v


def build(day: date, rng: random.Random) -> dict[str, pd.DataFrame]:
    po_rows, dn_rows = [], []
    for p in range(3):
        po = f"PO-{day:%Y}-{day.timetuple().tm_yday:03d}{p + 1}"
        supplier = SUPPLIERS[p % len(SUPPLIERS)]
        for k, (sku, desc, pack, (l, w, h), kg, frag) in enumerate(rng.sample(CATALOG, 6)):
            line = (k + 1) * 10
            qty = rng.randint(1, 4) if pack == "pallet" else rng.randint(1, 6)
            missing = rng.random() < 0.25                      # dims only on the delivery note
            comma = rng.random() < 0.3
            po_rows.append({
                "PO Number": po, "Line": line, "Supplier": f" {supplier} ", "SKU": sku, "Description": desc,
                "Qty": qty, "Packaging": pack.upper() if rng.random() < 0.2 else pack,
                "Length (mm)": None if missing else l, "Width (mm)": None if missing else w,
                "Height (mm)": None if missing else num(h, comma), "Weight (kg)": None if missing else num(kg, comma),
                "Fragile": frag, "Expected Date": (day + timedelta(days=p)).strftime("%d/%m/%Y"),
            })
            # ~60% of lines already delivered (some partially); the rest stays "ordered"
            if missing or rng.random() < 0.55:
                got = qty - 1 if qty > 1 and rng.random() < 0.3 else qty
                dn_rows.append({"DDT Number": f"DDT-{p + 1:03d}", "PO Number": po, "Line": line, "Delivered Qty": got,
                                "Length mm": l if missing else None, "Width mm": w if missing else None,
                                "Height mm": h if missing else None, "Weight kg": kg if missing else None,
                                "Fragile": (frag or "no") if missing else None,
                                "Delivery Date": day.strftime("%d/%m/%Y")})
    po_rows.append({k: None for k in po_rows[0]})                               # blank line
    bad = dict(po_rows[0], Line=999, Qty="two")                                 # rejected -> dead letter
    po_rows.append(bad)

    trucks = []
    for i in range(4):
        dep = datetime.combine(day, datetime.min.time()) + timedelta(hours=10 + 6 * i)
        trucks.append({"Truck Ref": f"T-{day:%m%d}-{i + 1}", "Carrier": rng.choice(["Bartolini", "TNT", "SDA",
                                                                                     "Arcese"]),
                       "Direction": "out", "Dock": f"Dock_EastHall_{i % 6 + 1}",
                       "Arrival": dep - timedelta(hours=2), "Departure": dep})
    trucks.append({"Truck Ref": f"T-{day:%m%d}-IN1", "Carrier": "Arcese", "Direction": "in", "Dock": "Dock_B1_1",
                   "Arrival": datetime.combine(day, datetime.min.time()) + timedelta(hours=7), "Departure": None})

    skus = sorted({r["SKU"] for r in po_rows if r["SKU"]})
    orders = []
    for o in range(6):
        for li in range(rng.randint(1, 3)):
            when = day + timedelta(days=o // 2)
            ship = when.strftime("%d/%m/%Y") if o % 3 == 2 else datetime.combine(when, datetime.min.time()) + \
                timedelta(hours=9 + o)
            orders.append({"Order Number": f"C-{day:%y%m%d}-{o + 1:02d}", "Line": li + 1,
                           "Customer": rng.choice(["Autofficina Rossi", "Carrozzeria Bianchi", "Garage Verdi",
                                                   "Ricambi Est"]),
                           "SKU": rng.choice(skus), "Qty": 1, "Ship Date": ship,
                           "Truck": trucks[o % 4]["Truck Ref"] if o % 2 == 0 else None})
    return {
        f"supplier_purchase_order_{day:%Y%m%d}.xlsx": pd.DataFrame(po_rows),
        f"delivery_note_{day:%Y%m%d}.xlsx": pd.DataFrame(dn_rows),
        f"customer_purchase_order_{day:%Y%m%d}.xlsx": pd.DataFrame(orders),
        f"truck_planning_{day:%Y%m%d}.xlsx": pd.DataFrame(trucks),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", type=date.fromisoformat, default=date.today())
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    for name, df in build(a.date, random.Random(a.date.toordinal())).items():
        df.to_excel(a.out / name, index=False)
        print(a.out / name, len(df), "rows")
