"""Input files: combined workbooks, PDF, stock snapshots, own-export guard, inspect, parameters."""
import pandas as pd
from openpyxl import load_workbook

from app.settings import settings
from conftest import ingest, pallet_line, units
from pdfmini import table_pdf

PO = ["PO Number", "Line", "Supplier", "SKU", "Description", "Qty", "Packaging", "Length mm", "Width mm",
      "Height mm", "Weight kg"]
DN = ["DDT Number", "PO Number", "Line", "Delivered Qty"]
CPO = ["Order Number", "Line", "Customer", "SKU", "Qty", "Ship Date", "Truck"]
STOCK = ["Case #", "SKU", "Description", "Qty", "Packaging", "Length mm", "Width mm", "Height mm", "Weight kg"]


def post(client, path, event="modified"):
    r = client.post("/ingest/file", json={"event": event, "path": str(path)})
    assert r.status_code == 200, r.text
    return r.json()


def combined_workbook(path):
    with pd.ExcelWriter(path) as xw:
        pd.DataFrame([["PO-9", 10, "ACME", "SKU-A", "Brake pads", 2, "pallet", 1200, 800, 900, 300]],
                     columns=PO).to_excel(xw, sheet_name="Purchase orders", index=False)
        pd.DataFrame([["DDT-1", "PO-9", 10, 2]], columns=DN).to_excel(xw, sheet_name="DDT", index=False)
        pd.DataFrame([["A2S1L1P1", "FLT-1", "Oil filters", 3, "parcel", 300, 200, 150, 4]],
                     columns=STOCK).to_excel(xw, sheet_name="Stock", index=False)
        pd.DataFrame([["C-9", 1, "Garage", "SKU-A", 1, None, None]], columns=CPO).to_excel(xw, sheet_name="Orders",
                                                                                         index=False)
        pd.DataFrame([["just notes"]], columns=["Notes"]).to_excel(xw, sheet_name="Notes", index=False)


def test_inspect_detects_all_three_datasets(client, tmp_path):
    f = tmp_path / "all.xlsm"                       # xlsm read like xlsx
    combined_workbook(tmp_path / "all.xlsx")
    (tmp_path / "all.xlsx").rename(f)
    r = client.post("/ingest/inspect", json={"path": str(f)}).json()
    assert r["datasets"] == ["in_stock", "ordered", "shipments"] and r["combined"] is True
    kinds = {t["name"]: t["kind"] for t in r["tables"]}
    assert kinds == {"Purchase orders": "purchase_order", "DDT": "delivery_note", "Stock": "stock",
                     "Orders": "customer_order", "Notes": None}


def test_combined_workbook_ingests_every_sheet(client, tmp_path):
    f = tmp_path / "all.xlsx"
    combined_workbook(f)
    r = post(client, f)
    assert r["kind"] == "purchase_order+delivery_note+stock+customer_order"
    assert [u["state"] for u in units(client, "PO-9-10")] == ["PLACED", "PLACED"]   # PO before DDT
    stock = units(client, "FLT-1")
    assert len(stock) == 3 and {u["slot"] for u in stock} == {"A2S1L1P1"}            # booked in the given case


def test_stock_snapshot_is_idempotent_and_releases(client, tmp_path):
    f = tmp_path / "stock.xlsx"
    pd.DataFrame([["A2S1L1P1", "FLT-1", "Oil filters", 3, "parcel", 300, 200, 150, 4],
                  [None, "MRR-1", "Mirror", 2, "parcel", 400, 300, 250, 3]], columns=STOCK).to_excel(f, index=False)
    r = post(client, f)
    assert r["inserted"] == 2
    assert all(u["state"] == "PLACED" for u in units(client, "MRR-1"))                 # engine chose the case
    pd.DataFrame([["A2S1L1P1", "FLT-1", "Oil filters", 3, "parcel", 300, 200, 150, 4],
                  [None, "MRR-1", "Mirror", 2, "parcel", 400, 300, 250, 3]], columns=STOCK).to_excel(f, index=False)
    r = post(client, f)
    assert (r["skipped_file"] or r["unchanged"] == 2) and r["inserted"] == 0
    pd.DataFrame([["A2S1L1P1", "FLT-1", "Oil filters", 1, "parcel", 300, 200, 150, 4]],
                 columns=STOCK).to_excel(f, index=False)
    r = post(client, f)
    assert r["cancelled"] == 1 and r["updated"] == 1
    assert sorted(u["state"] for u in units(client, "FLT-1")) == ["CANCELLED", "CANCELLED", "PLACED"]
    assert all(u["state"] == "CANCELLED" for u in units(client, "MRR-1"))


def test_stock_rejects_unknown_case_and_po_managed_lines(client, tmp_path):
    ingest(client, [pallet_line()], po="PO-1")
    f = tmp_path / "stock.xlsx"
    pd.DataFrame([["A99S9L9P9", "X", "Ghost", 1, "parcel", 1, 1, 1, 1]], columns=STOCK).to_excel(f, index=False)
    assert post(client, f)["rejected"] == 1
    g = tmp_path / "stock2.xlsx"
    pd.DataFrame([["A1S1L1P1", "PO-1", "10", "SKU-A", 1]], columns=["Case #", "PO", "Line", "SKU", "Qty"]).to_excel(
        g, index=False)
    assert post(client, g)["rejected"] == 1


def test_pdf_multipage_table(client, tmp_path):
    f = tmp_path / "orders.pdf"
    head = ["PO Number", "Line", "SKU", "Qty", "Packaging", "Length mm", "Width mm", "Height mm", "Weight kg"]
    page1 = [head] + [["PO-P", str(10 * i), f"SKU-{i}", "1", "parcel", "400", "300", "200", "5"] for i in range(1, 4)]
    page2 = [["PO-P", str(10 * i), f"SKU-{i}", "1", "parcel", "400", "300", "200", "5"] for i in range(4, 6)]
    table_pdf(f, [page1, page2], col_w=62)
    r = client.post("/ingest/inspect", json={"path": str(f)}).json()
    assert r["datasets"] == ["ordered"] and r["tables"][0]["rows"] == 5             # page 2 continues page 1
    assert post(client, f)["inserted"] == 5


def test_own_export_is_refused(client, tmp_path):
    ingest(client, [pallet_line()])
    path = tmp_path / "status.xlsx"
    r = client.get("/export.xlsx")
    path.write_bytes(r.content)
    assert load_workbook(path).properties.title == "Warehouse status"
    assert client.post("/ingest/inspect", json={"path": str(path)}).json()["own_export"] is True
    assert post(client, path)["kind"] == "rejected"


def test_unsupported_type(client, tmp_path):
    f = tmp_path / "old.xls"
    f.write_bytes(b"x")
    assert client.post("/ingest/inspect", json={"path": str(f)}).status_code == 422


def test_params_validate_apply_persist(client):
    ps = {p["key"]: p for p in client.get("/admin/params").json()}
    assert set(ps) == {"auto_place_on_receipt", "slot_buildings", "pre_ship_release_h", "default_dwell_days",
                       "fresh_hours"}
    assert ps["fresh_hours"]["label"] == "New add-on" and ps["slot_buildings"]["default"] == [1, 2, 3, 4]
    assert client.put("/admin/params", json={"default_dwell_days": 0}).status_code == 422
    assert client.put("/admin/params", json={"fill_factor": 0.7}).status_code == 422        # no longer editable
    assert client.put("/admin/params", json={"nope": 1}).status_code == 422
    assert client.put("/admin/params", json={"slot_buildings": []}).status_code == 422
    r = client.put("/admin/params", json={"default_dwell_days": 5, "slot_buildings": [2, 1, 2], "auto_place_on_receipt": False})
    assert r.status_code == 200 and settings.default_dwell_days == 5 and settings.slot_buildings == [1, 2]
    from app import params
    from app.db import session_scope
    settings.default_dwell_days = 14
    with session_scope() as s:
        params.load(s)
    assert settings.default_dwell_days == 5                                          # persisted
