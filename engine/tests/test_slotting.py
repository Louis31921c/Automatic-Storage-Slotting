from datetime import timedelta

from app.db import session_scope, utcnow
from app.models import Case
from conftest import ingest, pallet_line, units


def test_ingest_creates_units_with_packaging(client):
    r = ingest(client, [pallet_line(qty=2)])
    assert r["inserted"] == 1
    us = units(client, "PO-1-10")
    assert len(us) == 2
    u = us[0]
    assert (u["length_m"], u["width_m"], u["height_m"], u["weight_kg"]) == (1.2, 0.8, 1.144, 325.0)
    assert u["dims_source"] == "purchase_order" and u["state"] == "PENDING"


def test_reingest_same_po_is_idempotent(client):
    ingest(client, [pallet_line(qty=2)])
    r = ingest(client, [pallet_line(qty=2)])
    assert r["unchanged"] == 1 and r["inserted"] == 0
    assert len(units(client, "PO-1-10")) == 2


def test_slot_reserve_place_flow(client):
    ingest(client, [pallet_line(qty=2)])
    a1, a2 = units(client, "PO-1-10")
    r1 = client.post("/slot", json={"article_id": a1["id"]}).json()
    assert r1["slot"].startswith("A1S")
    assert r1["article"]["state"] == "RESERVED" and r1["reserved_until"]
    again = client.post("/slot", json={"article_id": a1["id"]}).json()
    assert again["slot"] == r1["slot"]                                         # idempotent while reserved
    r2 = client.post("/slot", json={"article_id": a2["id"]}).json()
    assert r2["slot"] != r1["slot"]                                            # never double-booked
    p = client.post(f"/articles/{a1['id']}/placed").json()
    assert p["case"]["state"] == "OCCUPIED" and p["article"]["state"] == "PLACED"
    loc = client.get(f"/articles/{a1['id']}/location").json()
    assert loc["case"]["id"] == r1["slot"]


def test_oversize_goes_extra(client):
    ingest(client, [pallet_line(h=2500)])
    a = units(client, "PO-1-10")[0]
    r = client.post("/slot", json={"article_id": a["id"]}).json()
    assert r["slot"] == "EXTRA" and r["case"] is None
    assert [x["id"] for x in client.get("/admin/extra").json()] == [a["id"]]


def test_level_weight_limit(client):
    ingest(client, [pallet_line(qty=4, kg=575)])                               # 600 kg per unit with pallet
    placed = []
    for a in units(client, "PO-1-10"):
        r = client.post("/slot", json={"article_id": a["id"]}).json()
        client.post(f"/articles/{a['id']}/placed")
        placed.append(r["case"])
    per_level: dict = {}
    for c in placed:
        per_level[(c["shelf_id"], c["level"])] = per_level.get((c["shelf_id"], c["level"]), 0) + 600
    assert max(per_level.values()) <= 1250


def test_missing_dims_then_manual(client):
    ingest(client, [{"line_ref": "20", "sku": "SKU-B", "qty_units": 1, "packaging": "parcel"}])
    a = units(client, "PO-1-20")[0]
    assert client.post("/slot", json={"article_id": a["id"]}).status_code == 409
    d = client.post("/resolve/dimensions", json={"article_id": a["id"]}).json()
    assert d["needs_manual"] is True
    d = client.post("/resolve/dimensions", json={"article_id": a["id"], "manual": {
        "length_mm": 600, "width_mm": 400, "height_mm": 300, "weight_kg": 12}}).json()
    assert d["resolved"] and d["source"] == "manual" and d["article"]["height_m"] == 0.32
    r = client.post("/slot", json={"article_id": a["id"]}).json()
    assert r["slot"] in ("A2S1L1P1", "A2S1L2P1", "A2S1L3P1", "FLOOR1")


def test_reservation_expires(client):
    ingest(client, [pallet_line()])
    a = units(client, "PO-1-10")[0]
    r = client.post("/slot", json={"article_id": a["id"]}).json()
    with session_scope() as s:
        s.get(Case, r["slot"]).reserved_until = utcnow() - timedelta(minutes=1)
    client.post("/admin/tick")
    assert client.get(f"/articles/{a['id']}").json()["state"] == "PENDING"


def test_qty_reduced_releases_shelf(client):
    ingest(client, [pallet_line(qty=2)])
    for a in units(client, "PO-1-10"):
        client.post("/slot", json={"article_id": a["id"]})
        client.post(f"/articles/{a['id']}/placed")
    r = ingest(client, [pallet_line(qty=1)])
    assert r["updated"] == 1
    states = sorted(u["state"] for u in units(client, "PO-1-10"))
    assert states == ["CANCELLED", "PLACED"]
    assert client.get("/admin/stats").json()["cases"]["OCCUPIED"] == 1


def test_weights_admin(client):
    assert client.put("/admin/weights", json={"size_fit": 40, "dwell": 20, "fragility": 20, "weight": 30}).status_code == 422
    assert client.put("/admin/weights", json={"size_fit": 40, "dwell": 20, "fragility": 20, "weight": 20}).status_code == 200
    assert client.get("/admin/weights").json()["size_fit"] == 40


def test_free_shelves_for_article(client):
    ingest(client, [pallet_line()])
    a = units(client, "PO-1-10")[0]
    free = client.get(f"/shelves/free?article_id={a['id']}").json()
    assert free and all(c["storage_type"] == "pallet" for c in free)


def test_heavy_pallet_goes_low(client):
    ingest(client, [pallet_line(kg=1075)])                                     # 1100 kg with pallet
    a = units(client, "PO-1-10")[0]
    assert client.post("/slot", json={"article_id": a["id"]}).json()["case"]["level"] == 1
