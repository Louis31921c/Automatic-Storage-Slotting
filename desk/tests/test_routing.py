import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routing import INBOX, assign, describe, import_plan, target_folder  # noqa: E402

PO = {"name": "PO", "kind": "purchase_order", "rows": 3, "headers": []}
CPO = {"name": "Orders", "kind": "customer_order", "rows": 2, "headers": []}
NOTES = {"name": "Notes", "kind": None, "rows": 1, "headers": ["Notes"]}


def info(path, tables, own=False):
    ds = sorted({{"purchase_order": "ordered", "customer_order": "shipments"}[t["kind"]] for t in tables if t["kind"]})
    return {"path": path, "own_export": own, "tables": tables, "datasets": ds, "combined": len(ds) > 1}


def test_single_and_combined_folders():
    assert target_folder(["ordered"]) == INBOX / "supplier"
    assert target_folder(["shipments"]) == INBOX / "customer"
    assert target_folder(["in_stock"]) == INBOX / "stock"
    assert target_folder(["ordered", "shipments"]) == INBOX / "mixed"


def test_combined_file_fills_all_its_slots_and_is_copied_once():
    i = info("/x/all.xlsx", [PO, CPO, NOTES])
    assert assign("ordered", i) == {"ordered": "/x/all.xlsx", "shipments": "/x/all.xlsx"}
    text, tone = describe(i)
    assert tone == "ok" and text.startswith("One database for Ordered + Shipments") and "1 other sheet" in text
    assert import_plan({"ordered": i, "shipments": i}) == [("/x/all.xlsx", INBOX / "mixed")]


def test_wrong_slot_moves_to_right_one():
    assert assign("ordered", info("/x/cpo.xlsx", [CPO])) == {"shipments": "/x/cpo.xlsx"}


def test_errors():
    assert describe(info("/x/out.xlsx", [], own=True))[1] == "err"
    assert describe(info("/x/n.xlsx", [NOTES]))[1] == "err"
    assert import_plan({"ordered": info("/x/n.xlsx", [NOTES])}) == []
