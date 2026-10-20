from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.db import session_scope, utcnow
from app.models import Article
from conftest import ingest, pallet_line, units


def place(client, code):
    out = []
    for a in units(client, code):
        r = client.post("/slot", json={"article_id": a["id"]}).json()
        client.post(f"/articles/{a['id']}/placed")
        out.append((a["id"], r["slot"]))
    return out


def orders(client, tmp_path, rows, name="cpo.xlsx"):
    f = tmp_path / name
    pd.DataFrame(rows, columns=["Order Number", "Line", "Customer", "SKU", "Qty", "Ship Date", "Truck"]).to_excel(
        f, index=False)
    r = client.post("/ingest/file", json={"event": "modified", "path": str(f)})
    assert r.status_code == 200, r.text
    return r.json()


def local(dt_utc):
    return dt_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("Europe/Rome")).replace(tzinfo=None)


def test_24h_rule_stages_and_frees_case(client, tmp_path):
    ingest(client, [pallet_line(qty=1)])
    (aid, rack_case), = place(client, "PO-1-10")
    ship = local(utcnow() + timedelta(hours=10))
    orders(client, tmp_path, [["C-1", 1, "Garage Rossi", "SKU-A", 1, ship, None]])
    a = client.get(f"/articles/{aid}").json()
    assert a["state"] == "STAGED" and a["slot"].startswith("SHIP")
    stats = client.get("/admin/stats").json()
    assert stats["cases"]["OCCUPIED"] == 1                     # only the SHIP slot
    # the freed rack case is not offered until the unit is physically picked
    ingest(client, [pallet_line(line_ref="20", qty=1, sku="SKU-Z")])
    other = units(client, "PO-1-20")[0]
    free_ids = [c["id"] for c in client.get(f"/shelves/free?article_id={other['id']}&limit=100").json()]
    assert rack_case not in free_ids
    route = client.get("/pick/orders/C-1").json()
    assert route["to_pick"][0]["from_case"]["id"] == rack_case
    client.post(f"/articles/{aid}/picked")
    free_ids = [c["id"] for c in client.get(f"/shelves/free?article_id={other['id']}&limit=100").json()]
    assert rack_case in free_ids
    r = client.post(f"/articles/{aid}/loaded").json()
    assert r["state"] == "SHIPPED"
    assert "OCCUPIED" not in client.get("/admin/stats").json()["cases"]


def test_far_ship_time_stays_placed_and_no_ship_time(client, tmp_path):
    ingest(client, [pallet_line(qty=2)])
    place(client, "PO-1-10")
    orders(client, tmp_path, [["C-2", 1, "X", "SKU-A", 1, local(utcnow() + timedelta(days=5)), None],
                              ["C-3", 1, "Y", "SKU-A", 1, None, None]])
    assert sorted(u["state"] for u in units(client, "PO-1-10")) == ["PLACED", "PLACED"]


def test_lifo_allocates_newest(client, tmp_path):
    ingest(client, [pallet_line(qty=2)])
    (old_id, _), (new_id, _) = place(client, "PO-1-10")
    with session_scope() as s:
        s.get(Article, old_id).received_at = utcnow() - timedelta(days=3)
    orders(client, tmp_path, [["C-4", 1, "X", "SKU-A", 1, local(utcnow() + timedelta(hours=2)), None]])
    assert client.get(f"/articles/{new_id}").json()["state"] == "STAGED"
    assert client.get(f"/articles/{old_id}").json()["state"] == "PLACED"


def test_order_cancelled_after_staging_frees_ship_slot(client, tmp_path):
    ingest(client, [pallet_line(qty=1)])
    (aid, _), = place(client, "PO-1-10")
    orders(client, tmp_path, [["C-5", 1, "X", "SKU-A", 1, local(utcnow() + timedelta(hours=2)), None]])
    assert client.get(f"/articles/{aid}").json()["state"] == "STAGED"
    orders(client, tmp_path, [["C-6", 1, "X", "SKU-Q", 1, None, None]])     # C-5 row removed from file
    a = client.get(f"/articles/{aid}").json()
    assert a["state"] == "PENDING" and a["slot"] is None
    assert "OCCUPIED" not in client.get("/admin/stats").json()["cases"]


def test_truck_departure_overrides_ship_date(client, tmp_path):
    ingest(client, [pallet_line(qty=1)])
    (aid, _), = place(client, "PO-1-10")
    orders(client, tmp_path, [["C-7", 1, "X", "SKU-A", 1, local(utcnow() + timedelta(days=5)), "T-1"]])
    assert client.get(f"/articles/{aid}").json()["state"] == "PLACED"
    f = tmp_path / "trucks.xlsx"
    pd.DataFrame([["T-1", "DHL", "out", "Dock_EastHall_2", local(utcnow() + timedelta(hours=3)),
                   local(utcnow() + timedelta(hours=4))]],
                 columns=["Truck Ref", "Carrier", "Direction", "Dock", "Arrival", "Departure"]).to_excel(f, index=False)
    client.post("/ingest/file", json={"event": "modified", "path": str(f)})
    assert client.get(f"/articles/{aid}").json()["state"] == "STAGED"
    t = client.get("/trucks").json()
    assert t[0]["truck_ref"] == "T-1" and len(t[0]["manifest"]) == 1


def test_date_only_ship_defaults_to_8am_local(client, tmp_path):
    ingest(client, [pallet_line(qty=1)])
    (aid, _), = place(client, "PO-1-10")
    day = datetime(2027, 1, 15)
    orders(client, tmp_path, [["C-8", 1, "X", "SKU-A", 1, day, None]])
    ship = client.get(f"/articles/{aid}").json()["ship_at"]
    assert ship.startswith("2027-01-15T07:00:00")             # 08:00 CET = 07:00 UTC
