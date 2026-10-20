"""Read every data table out of an input file and recognise what it is.

xlsx / xlsm / csv: one table per sheet. pdf: tables found by pdfplumber, multi-page tables are joined
(a page table whose first row is not a known header continues the previous table).
A single workbook may hold several datasets (e.g. POs + orders + stock): each table is detected on its own.
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

OWN_EXPORT_TITLE = "Warehouse status"
SUPPORTED = {".xlsx", ".xlsm", ".csv", ".pdf"}

# detected kind -> what the user calls it
DATASET = {"purchase_order": "ordered", "delivery_note": "ordered", "stock": "in_stock",
           "customer_order": "shipments", "truck_planning": "shipments"}
# processing order inside one file: POs before their delivery notes, stock, then orders before trucks
ORDER = ["purchase_order", "delivery_note", "stock", "customer_order", "truck_planning"]


@dataclass
class Table:
    name: str                      # sheet name, or "page 3 table 1"
    df: pd.DataFrame               # normalised headers
    kind: str | None
    raw_headers: list[str]


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Column titles -> engine fields (headers.resolve: synonyms, languages, typos, units); values of
    columns with a unit in their title are converted (cm -> mm, g -> kg, ...)."""
    from .headers import resolve
    from .ingest import parse_num
    fields = resolve([str(c) for c in df.columns])
    df = df.copy()
    df.columns = [f for f, _ in fields]
    for col, (f, scale) in zip(df.columns, fields):
        if scale != 1.0:
            def conv(v, k=scale):
                try:
                    n = parse_num(v)
                except (TypeError, ValueError):
                    return v                    # left as is: the row validator reports it
                return None if n is None else round(n * k, 6)
            df[col] = df[col].map(conv)
    return df.dropna(how="all")


def detect(cols: set[str]) -> str | None:
    if "dn_number" in cols and "po_number" in cols:
        return "delivery_note"
    if "case_id" in cols and "sku" in cols and "qty_units" in cols:
        return "stock"
    if "order_number" in cols and "sku" in cols:
        return "customer_order"
    if "truck_ref" in cols and ({"departure_at", "arrival_at"} & cols):
        return "truck_planning"
    if "po_number" in cols and "sku" in cols:
        return "purchase_order"
    return None


def _table(name: str, df: pd.DataFrame) -> Table:
    raw = [str(c) for c in df.columns]
    df = _normalise(df)
    return Table(name, df, detect(set(df.columns)), raw)


def is_own_export(path: Path) -> bool:
    """Our status spreadsheet must never be fed back in (feedback loop)."""
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        return False
    from openpyxl import load_workbook
    try:
        wb = load_workbook(path, read_only=True)
        return (wb.properties.title or "").lower().endswith("warehouse status")   # older files too
    except Exception:
        return False


def _pdf_tables(path: Path) -> list[Table]:
    import pdfplumber
    from .headers import resolve
    out: list[Table] = []
    current: list[list[Any]] | None = None
    header: list[str] | None = None
    page_no = name = None

    def flush():
        if header and current:
            out.append(_table(name, pd.DataFrame(current, columns=header)))

    with pdfplumber.open(path) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            for t_no, rows in enumerate(page.extract_tables(), start=1):
                rows = [[(c.strip() if isinstance(c, str) else c) or None for c in r] for r in rows if any(r)]
                if not rows:
                    continue
                first = [str(c or "") for c in rows[0]]
                numeric = any(re.fullmatch(r"[-+]?\d+([.,]\d+)?", c.strip()) for c in first if c)
                is_header = not numeric and detect({f for f, _ in resolve(first)}) is not None   # data rows have numbers
                if is_header or header is None or len(first) != len(header):
                    flush()
                    header, current, name = first, rows[1:], f"page {page_no} table {t_no}"
                else:                           # continuation of a table from the previous page
                    current.extend(rows)
    flush()
    return out


def read_tables(path: Path) -> list[Table]:
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"unsupported file type {suffix} (use xlsx, xlsm, csv or pdf)")
    if suffix == ".csv":
        return [_table(path.stem, pd.read_csv(path, dtype=object, sep=None, engine="python"))]
    if suffix == ".pdf":
        return _pdf_tables(path)
    sheets = pd.read_excel(path, sheet_name=None, dtype=object, engine="openpyxl")
    return [_table(name, df) for name, df in sheets.items() if not df.empty]


def summary(path: Path) -> dict[str, Any]:
    """What is in this file, without ingesting it (used by the input window)."""
    if is_own_export(path):
        return {"path": str(path), "own_export": True, "tables": [], "datasets": [], "combined": False}
    tables = read_tables(path)
    datasets = sorted({DATASET[t.kind] for t in tables if t.kind})
    return {
        "path": str(path), "own_export": False,
        "tables": [{"name": t.name, "kind": t.kind, "dataset": DATASET.get(t.kind), "rows": len(t.df),
                    "headers": t.raw_headers[:12]} for t in tables],
        "datasets": datasets,
        "combined": len(datasets) > 1,
    }
