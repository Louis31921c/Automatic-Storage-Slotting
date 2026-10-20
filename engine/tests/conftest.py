import json

import pytest
from fastapi.testclient import TestClient

from app.settings import settings


def _case(cid, aisle, shelf, level, pos, st, w, d, h, z, x, building=2, rack=None, max_level=2):
    return {"id": cid, "shelf_id": f"A{aisle}S{shelf}" if aisle else cid, "aisle": aisle, "shelf": shelf,
            "level": level, "pos": pos, "building": building, "rack": rack or f"R{aisle}", "storage_type": st,
            "width_m": w, "depth_m": d, "height_m": h, "volume_m3": round(w * d * h, 3), "max_kg": 1250,
            "level_max_kg": 1250, "z_m": z, "rect": [x, 0.0, x + w, d]}


def storage_fixture():
    cases = []
    # pallet rack A1: 2 bays x 2 levels x 2 positions (B2-like)
    for shelf in (1, 2):
        for level, (z, h) in ((1, (0.0, 1.68)), (2, (1.8, 1.6))):
            for pos in (1, 2):
                x = (shelf - 1) * 2.7 + (pos - 1) * 1.35
                cases.append(_case(f"A1S{shelf}L{level}P{pos}", 1, shelf, level, pos, "pallet", 1.337, 1.1, h, z, x))
    # parcel shelving A2: 1 bay x 3 levels
    for level in (1, 2, 3):
        cases.append(_case(f"A2S1L{level}P1", 2, 1, level, 1, "parcel", 1.0, 1.2, 0.45, (level - 1) * 0.5, 20.0,
                           building=1))
    cases.append(_case("A3S1L1P1", 3, 1, 1, 1, "bin", 1.0, 1.2, 0.4, 4.5, 30.0, building=1))
    cases.append(_case("FLOOR1", None, None, 1, 1, "floor", 2.0, 1.2, 3.0, 0.0, 40.0, building=3,
                       rack="B3_crate_storage"))
    racks = [{"aisle": 1, "rack": "R1", "building": 2, "storage_type": "pallet", "rect": [0, 0, 5.4, 1.1],
              "cases": 16, "axis": "X", "bays": 2, "levels": 2, "nominal_bay_m": 2.7},
             {"aisle": 2, "rack": "R2", "building": 1, "storage_type": "parcel", "rect": [20, 0, 21, 1.2],
              "cases": 3, "axis": "X", "bays": 1, "levels": 3, "nominal_bay_m": 1.0},
             {"aisle": 3, "rack": "R3", "building": 1, "storage_type": "bin", "rect": [30, 0, 31, 1.2],
              "cases": 1, "axis": "X", "bays": 1, "levels": 1, "nominal_bay_m": 1.0}]
    return {"contract_version": "0.2.0", "cases": cases, "racks": racks,
            "docks": [{"id": "Dock_B1_1", "x": 0.0, "y": -5.0}], "walls": [[-1, -10, 50, 10]]}


@pytest.fixture
def client(tmp_path, monkeypatch):
    p = tmp_path / "storage_spaces.json"
    p.write_text(json.dumps(storage_fixture()))
    monkeypatch.setattr(settings, "db_path", tmp_path / "test.db")
    monkeypatch.setattr(settings, "run_dir", tmp_path / "run")
    monkeypatch.setattr(settings, "storage_json", p)
    monkeypatch.setattr(settings, "ship_slots", 2)
    monkeypatch.setattr(settings, "export_path", tmp_path / "out" / "warehouse_status.xlsx")
    monkeypatch.setattr(settings, "export_debounce_s", 0.05)
    from app.params import SPEC                       # runtime params mutate settings: restore per test
    for k in SPEC:
        monkeypatch.setattr(settings, k, getattr(settings, k))
    from app.main import app
    with TestClient(app) as c:
        yield c


def pallet_line(line_ref="10", qty=1, sku="SKU-A", l=1200, w=800, h=1000, kg=300, **kw):
    return {"line_ref": line_ref, "sku": sku, "qty_units": qty, "packaging": "pallet", "length_mm": l,
            "width_mm": w, "height_mm": h, "weight_kg": kg, **kw}


def ingest(client, lines, po="PO-1", supplier="ACME"):
    r = client.post("/ingest/purchase_order", json={"po_number": po, "supplier": supplier, "lines": lines})
    assert r.status_code == 200, r.text
    return r.json()


def units(client, code):
    return client.get(f"/lookup/{code}").json()["articles"]
