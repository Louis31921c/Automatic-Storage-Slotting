"""Where an input file goes, from what the engine detected in it (pure logic, tested)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INBOX = ROOT / "data" / "inbox"
SLOTS = ("ordered", "in_stock", "shipments")
LABEL = {"ordered": "Ordered", "in_stock": "In stock", "shipments": "Shipments"}
FOLDER = {"ordered": "supplier", "in_stock": "stock", "shipments": "customer"}
KIND_LABEL = {"purchase_order": "purchase order", "delivery_note": "delivery note", "stock": "stock list",
              "customer_order": "customer order", "truck_planning": "truck planning"}


def target_folder(datasets: list[str]) -> Path:
    """One dataset -> its own inbox folder; several -> inbox/mixed (engine splits it per sheet)."""
    if len(datasets) == 1:
        return INBOX / FOLDER[datasets[0]]
    return INBOX / "mixed"


def describe(info: dict) -> tuple[str, str]:
    """(text, tone) for the line under a slot. tone: ok | warn | err."""
    if info.get("own_export"):
        return "This is the status spreadsheet (an output). Choose an input file.", "err"
    known = [t for t in info["tables"] if t["kind"]]
    if not known:
        heads = info["tables"][0]["headers"][:5] if info["tables"] else []
        return f"No recognised table (headers: {', '.join(map(str, heads)) or 'none'}).", "err"
    parts = [f"{KIND_LABEL[t['kind']]} ({t['rows']} rows)" for t in known]
    skipped = len(info["tables"]) - len(known)
    text = ", ".join(parts) + (f"; {skipped} other sheet(s) ignored" if skipped else "")
    if info["combined"]:
        return "One database for " + " + ".join(LABEL[d] for d in SLOTS if d in info["datasets"]) + ": " + text, "ok"
    return text, "ok"


def assign(slot: str, info: dict) -> dict[str, str]:
    """Which slots this file fills. A file holding several datasets fills all of them; a file put in the
    wrong slot moves to the right one."""
    return {d: info["path"] for d in info["datasets"]} if info["datasets"] else {slot: info["path"]}


def import_plan(slots: dict[str, dict]) -> list[tuple[str, Path]]:
    """Distinct files to copy and their inbox folder: [(source path, folder)]."""
    seen, plan = set(), []
    for info in slots.values():
        if not info or info.get("own_export") or not info["datasets"] or info["path"] in seen:
            continue
        seen.add(info["path"])
        plan.append((info["path"], target_folder(info["datasets"])))
    return plan
