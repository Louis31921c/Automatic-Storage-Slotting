"""Shared JSON contract between engine, watcher and UI. Bump on breaking changes."""
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

CONTRACT_VERSION = "0.2.0"

Packaging = Literal["pallet", "parcel", "small"]
StorageType = Literal["pallet", "parcel", "bin", "floor", "ship"]
ArticleState = Literal["PENDING", "RESERVED", "PLACED", "STAGED", "SHIPPED", "CANCELLED"]


class ComponentHealth(BaseModel):
    ok: bool
    detail: str = ""


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    contract_version: str = CONTRACT_VERSION
    db: ComponentHealth
    watcher: ComponentHealth


# ---- ingest
class PoLineIn(BaseModel):
    line_ref: str = Field(min_length=1, max_length=20)
    sku: str = Field(min_length=1, max_length=60)
    description: str = ""
    qty_units: int = Field(ge=1, le=10_000)
    packaging: Packaging = "pallet"
    length_mm: float | None = Field(default=None, gt=0, le=20_000)    # goods per unit, excl. pallet/carton
    width_mm: float | None = Field(default=None, gt=0, le=20_000)
    height_mm: float | None = Field(default=None, gt=0, le=20_000)
    weight_kg: float | None = Field(default=None, gt=0, le=5_000)
    fragility: int | None = Field(default=None, ge=0, le=2)
    expected_date: datetime | None = None


class PurchaseOrderIn(BaseModel):
    po_number: str = Field(min_length=1, max_length=40)
    supplier: str = ""
    lines: list[PoLineIn]


class IngestResult(BaseModel):
    kind: str
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    cancelled: int = 0
    rejected: int = 0
    skipped_file: bool = False
    tables: list[dict[str, Any]] = []        # every sheet / PDF table seen: name, detected kind, rows


class FileEvent(BaseModel):
    event: Literal["created", "modified", "deleted", "moved"]
    path: str
    dest_path: str | None = None


# ---- articles / cases
class CaseOut(BaseModel):
    id: str
    shelf_id: str
    building: int
    rack: str
    storage_type: StorageType
    level: int
    pos: int
    width_m: float
    depth_m: float
    height_m: float
    volume_m3: float
    max_kg: float
    state: str
    rect: list[float] | None
    dock_dist_m: float


class ArticleOut(BaseModel):
    id: int
    code: str
    po_number: str
    line_ref: str
    unit_index: int
    qty_units: int
    sku: str
    description: str
    packaging: Packaging
    length_m: float | None
    width_m: float | None
    height_m: float | None
    weight_kg: float | None
    dims_source: str | None
    fragility: int
    dwell_days: float
    state: ArticleState
    slot: str | None
    reserved_until: datetime | None
    placed_at: datetime | None
    ship_at: datetime | None


class ManualDimensions(BaseModel):
    """Goods dims per unit (excl. packaging), entered on the dropoff screen."""
    length_mm: float = Field(gt=0, le=20_000)
    width_mm: float = Field(gt=0, le=20_000)
    height_mm: float = Field(gt=0, le=20_000)
    weight_kg: float = Field(gt=0, le=5_000)
    fragility: int | None = Field(default=None, ge=0, le=2)


class DimensionsRequest(BaseModel):
    article_id: int
    manual: ManualDimensions | None = None


class DimensionsResult(BaseModel):
    article_id: int
    resolved: bool
    source: str | None
    needs_manual: bool
    article: ArticleOut


class SlotRequest(BaseModel):
    article_id: int


class Candidate(BaseModel):
    case_id: str
    total: float
    components: dict[str, float]


class SlotResult(BaseModel):
    article: ArticleOut
    slot: str                          # case id | "EXTRA"
    case: CaseOut | None
    reserved_until: datetime | None
    score: Candidate | None
    alternatives: list[Candidate]


class LocationOut(BaseModel):
    article: ArticleOut
    case: CaseOut | None


class LookupOut(BaseModel):
    code: str
    articles: list[ArticleOut]         # matching units, most actionable first


# ---- map
class MapRack(BaseModel):
    aisle: int
    rack: str
    building: int
    storage_type: str
    rect: list[float]
    cases: int
    occupied: int


class MapOut(BaseModel):
    bounds: list[float]
    racks: list[MapRack]
    floor: list[dict[str, Any]]
    docks: list[dict[str, Any]]
    walls: list[list[float]]


# ---- admin
class Weights(BaseModel):
    size_fit: float = Field(ge=0, le=100)
    dwell: float = Field(ge=0, le=100)
    fragility: float = Field(ge=0, le=100)
    weight: float = Field(ge=0, le=100)


class EventOut(BaseModel):
    id: int
    ts: datetime
    type: str
    data: dict[str, Any]
