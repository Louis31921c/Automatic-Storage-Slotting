from datetime import timedelta

import pandas as pd
from sqlalchemy import update

from app.db import session_scope, utcnow
from app.models import PoLine
from conftest import units

PO_COLS = ["PO Number", "Line", "Supplier", "SKU", "Description", "Qty", "Packaging", "Length (mm)", "Width (mm)",
           "Height (mm)", "Weight (kg)", "Fragile", "Expected Date"]


def po_rows():
    return [
        ["PO-7", 10, "Bosch", "BX-1", "Brake discs", 2, "pallet", 1200, 800, 900, 400, "", "20/10/2026"],
        ["PO-7", 20, "Bosch", "BX-2", "Headlamps", 1, "Pallet ", "1100", "750", "1000,5", "210,5", "Y", "21/10/2026"],
        ["PO-7", 30, "Bosch", "BX-3", "Mirror", 1, "parcel", None, None, None, None, None, None],
        ["PO-7", 40, "Bosch", "BX-4", "Bad row", "two", "pallet", 1, 1, 1, 1, "", ""],
    ]


def write(path, rows, cols=PO_COLS):
    pd.DataFrame(rows, columns=cols).to_excel(path, index=False)


def ingest(client, path, event="modified", dest=None):
    r = client.post("/ingest/file", json={"event": event, "path": str(path), "dest_path": str(dest) if dest else None})
    assert r.status_code == 200, r.text
    return r.json()


def test_messy_excel_ingest(client, tmp_path):
    f = tmp_path / "po.xlsx"
    write(f, po_rows())
    r = ingest(client, f)
    assert r["kind"] == "purchase_order" and r["inserted"] == 3 and r["rejected"] == 1
    u = units(client, "PO-7-20")[0]
    assert u["fragility"] == 1 and u["height_m"] == round(1.0005 + 0.144, 3) and u["weight_kg"] == 235.5
    assert len(units(client, "PO-7-10")) == 2
    dl = client.get("/admin/dead_letter").json()
    assert dl[0]["row_number"] == 5


def test_same_file_twice_is_skipped(client, tmp_path):
    f = tmp_path / "po.xlsx"
    write(f, po_rows())
    ingest(client, f)
    assert ingest(client, f)["skipped_file"] is True
    assert len(units(client, "PO-7-10")) == 2


def test_row_order_does_not_matter_and_edit_updates(client, tmp_path):
    f = tmp_path / "po.xlsx"
    write(f, po_rows())
    ingest(client, f)
    rows = list(reversed(po_rows()))
    rows[-1][5] = 3                                    # PO-7/10 qty 2 -> 3
    write(f, rows)
    r = ingest(client, f)
    assert r["updated"] == 1 and r["unchanged"] == 2 and r["inserted"] == 0
    assert len(units(client, "PO-7-10")) == 3


def test_row_deleted_releases_placed_shelf(client, tmp_path):
    f = tmp_path / "po.xlsx"
    write(f, po_rows())
    ingest(client, f)
    a = units(client, "PO-7-20")[0]
    client.post("/slot", json={"article_id": a["id"]})
    client.post(f"/articles/{a['id']}/placed")
    write(f, [r for r in po_rows() if r[1] != 20])
    r = ingest(client, f)
    assert r["cancelled"] == 1
    assert units(client, "PO-7-20")[0]["state"] == "CANCELLED"
    assert "OCCUPIED" not in client.get("/admin/stats").json()["cases"]


def test_delivery_note_fills_missing_dims(client, tmp_path):
    f = tmp_path / "po.xlsx"
    write(f, po_rows())
    ingest(client, f)
    dn = tmp_path / "dn.xlsx"
    write(dn, [["DN-1", "PO-7", 30, 1, 500, 400, 300, 8, "no"]],
          ["DDT Number", "PO Number", "Line", "Delivered Qty", "Length mm", "Width mm", "Height mm", "Weight kg",
           "Fragile"])
    r = ingest(client, dn)
    assert r["kind"] == "delivery_note" and r["inserted"] == 1
    u = units(client, "PO-7-30")[0]
    assert u["dims_source"] == "delivery_note" and u["length_m"] == 0.52
    assert u["state"] == "PLACED"                                          # received -> case booked


def test_rename_keeps_rows_delete_cancels_after_grace(client, tmp_path):
    f, g = tmp_path / "po.xlsx", tmp_path / "po_renamed.xlsx"
    write(f, po_rows())
    ingest(client, f)
    f.rename(g)
    ingest(client, f, event="moved", dest=g)
    assert units(client, "PO-7-10")[0]["state"] == "PENDING"
    ingest(client, g, event="deleted")
    client.post("/admin/tick")
    assert units(client, "PO-7-10")[0]["state"] == "PENDING"            # still within the rename grace
    with session_scope() as s:
        s.execute(update(PoLine).values(missing_since=utcnow() - timedelta(hours=1)))
    client.post("/admin/tick")
    assert units(client, "PO-7-10")[0]["state"] == "CANCELLED"


def test_unknown_headers_rejected(client, tmp_path):
    f = tmp_path / "x.xlsx"
    write(f, [[1, 2]], ["foo", "bar"])
    assert ingest(client, f)["kind"] == "unknown"
