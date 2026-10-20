"""Dimension lookup chain (PRD 4.2): PO -> delivery note -> supplier site -> web scrape -> manual.

PO and delivery-note dims land on the PoLine during ingest (dims_source tells which).
Supplier-site and scrape adapters are registered per supplier and are off by default (ToS).
"""
from collections.abc import Callable

from sqlalchemy.orm import Session

from .contract import ManualDimensions
from .events import emit
from .models import PENDING, Article, PackagingProfile, PoLine
from .packaging import apply_packaging

# supplier name -> callable(sku) -> (length_mm, width_mm, height_mm, weight_kg, fragility|None) | None
SupplierLookup = Callable[[str], tuple[float, float, float, float, int | None] | None]
SUPPLIER_SITE: dict[str, SupplierLookup] = {}
WEB_SCRAPE: dict[str, SupplierLookup] = {}


def _line_has_dims(line: PoLine) -> bool:
    return None not in (line.length_mm, line.width_mm, line.height_mm, line.weight_kg)


def _set_line_dims(line: PoLine, dims: tuple, source: str) -> None:
    line.length_mm, line.width_mm, line.height_mm, line.weight_kg = dims[:4]
    if len(dims) > 4 and dims[4] is not None:
        line.fragility = dims[4]
    line.dims_source = source


def apply_to_units(s: Session, line: PoLine) -> None:
    """Copy line dims (+packaging) to its units that are not placed yet."""
    if not _line_has_dims(line):
        return
    prof = s.get(PackagingProfile, line.packaging)
    assert prof is not None, f"unknown packaging {line.packaging}"
    u = apply_packaging(prof, line.length_mm, line.width_mm, line.height_mm, line.weight_kg)
    for a in line.units:
        if a.state == PENDING:
            a.length_m, a.width_m, a.height_m, a.weight_kg = u.length_m, u.width_m, u.height_m, u.weight_kg
            a.dims_source = line.dims_source
            a.fragility = line.fragility


def resolve(s: Session, art: Article, manual: ManualDimensions | None = None) -> str | None:
    """Run the chain for the article's PO line. Returns the source used, or None (manual entry needed)."""
    line = art.line
    if manual is not None:
        _set_line_dims(line, (manual.length_mm, manual.width_mm, manual.height_mm, manual.weight_kg,
                              manual.fragility), "manual")
    elif not _line_has_dims(line):
        supplier = line.po.supplier
        for source, registry in (("supplier_site", SUPPLIER_SITE), ("web_scrape", WEB_SCRAPE)):
            fn = registry.get(supplier)
            dims = fn(line.sku) if fn else None
            if dims:
                _set_line_dims(line, dims, source)
                break
    if not _line_has_dims(line):
        return None
    apply_to_units(s, line)
    emit(s, "article.dimensions", article_id=art.id, source=line.dims_source)
    return line.dims_source
