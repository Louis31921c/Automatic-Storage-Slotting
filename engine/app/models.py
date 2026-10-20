"""DB schema. DB is the canonical state; Excel is input only."""
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, utcnow

# case.state
FREE, OCCUPIED = "FREE", "OCCUPIED"
# article.state
PENDING, RESERVED, PLACED, STAGED, SHIPPED, CANCELLED = "PENDING", "RESERVED", "PLACED", "STAGED", "SHIPPED", "CANCELLED"
EXTRA = "EXTRA"


class Case(Base):
    """One pallet position (or bin / floor spot / SHIP slot). IDs come from the FreeCAD macro, never hand-made."""
    __tablename__ = "cases"
    id: Mapped[str] = mapped_column(String(24), primary_key=True)
    shelf_id: Mapped[str] = mapped_column(String(16), index=True)
    building: Mapped[int] = mapped_column(Integer, index=True)
    rack: Mapped[str] = mapped_column(String(40))
    storage_type: Mapped[str] = mapped_column(String(10), index=True)   # pallet | parcel | bin | floor | ship
    aisle: Mapped[int | None] = mapped_column(Integer)
    shelf: Mapped[int | None] = mapped_column(Integer)
    level: Mapped[int] = mapped_column(Integer)
    pos: Mapped[int] = mapped_column(Integer)
    width_m: Mapped[float] = mapped_column(Float)
    depth_m: Mapped[float] = mapped_column(Float)
    height_m: Mapped[float] = mapped_column(Float)
    volume_m3: Mapped[float] = mapped_column(Float)
    max_kg: Mapped[float] = mapped_column(Float)
    level_max_kg: Mapped[float] = mapped_column(Float)
    z_m: Mapped[float] = mapped_column(Float, default=0.0)
    rect: Mapped[list[float] | None] = mapped_column(JSON)
    dock_dist_m: Mapped[float] = mapped_column(Float, default=0.0)
    max_level: Mapped[int] = mapped_column(Integer, default=1)          # highest level of this rack
    state: Mapped[str] = mapped_column(String(10), default=FREE, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)        # admin override / removed from model
    reserved_until: Mapped[datetime | None] = mapped_column(DateTime)
    reserved_for: Mapped[int | None] = mapped_column(Integer)


class PackagingProfile(Base):
    __tablename__ = "packaging_profiles"
    name: Mapped[str] = mapped_column(String(16), primary_key=True)     # pallet | parcel | small
    base_length_m: Mapped[float] = mapped_column(Float)                 # minimum footprint (pallet base)
    base_width_m: Mapped[float] = mapped_column(Float)
    add_height_m: Mapped[float] = mapped_column(Float)                  # pallet base / carton allowance
    add_each_side_m: Mapped[float] = mapped_column(Float)
    add_weight_kg: Mapped[float] = mapped_column(Float)
    storage_types: Mapped[list[str]] = mapped_column(JSON)              # where this packaging may go


class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    po_number: Mapped[str] = mapped_column(String(40), primary_key=True)
    supplier: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    lines: Mapped[list["PoLine"]] = relationship(back_populates="po")


class PoLine(Base):
    __tablename__ = "po_lines"
    __table_args__ = (UniqueConstraint("po_number", "line_ref"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_number: Mapped[str] = mapped_column(ForeignKey("purchase_orders.po_number"), index=True)
    line_ref: Mapped[str] = mapped_column(String(20))
    sku: Mapped[str] = mapped_column(String(60), index=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    qty_units: Mapped[int] = mapped_column(Integer)
    packaging: Mapped[str] = mapped_column(String(16))
    # goods dims per handling unit as given by the source (mm / kg); None = unknown
    length_mm: Mapped[float | None] = mapped_column(Float)
    width_mm: Mapped[float | None] = mapped_column(Float)
    height_mm: Mapped[float | None] = mapped_column(Float)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    dims_source: Mapped[str | None] = mapped_column(String(20))         # purchase_order | delivery_note | ...
    fragility: Mapped[int] = mapped_column(Integer, default=0)
    expected_date: Mapped[datetime | None] = mapped_column(DateTime)
    source_file: Mapped[str | None] = mapped_column(String(300))
    row_hash: Mapped[str | None] = mapped_column(String(64))
    missing_since: Mapped[datetime | None] = mapped_column(DateTime)    # source file gone (rename grace)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    units: Mapped[list["Article"]] = relationship(back_populates="line", order_by="Article.unit_index")


class Article(Base):
    """One handling unit (pallet / parcel / small item) of a PO line = one slot."""
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("po_line_id", "unit_index"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    po_line_id: Mapped[int] = mapped_column(ForeignKey("po_lines.id"), index=True)
    unit_index: Mapped[int] = mapped_column(Integer)
    sku: Mapped[str] = mapped_column(String(60), index=True)
    # handling-unit dims incl. packaging (m / kg)
    length_m: Mapped[float | None] = mapped_column(Float)
    width_m: Mapped[float | None] = mapped_column(Float)
    height_m: Mapped[float | None] = mapped_column(Float)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    dims_source: Mapped[str | None] = mapped_column(String(20))
    fragility: Mapped[int] = mapped_column(Integer, default=0)
    dwell_days: Mapped[float] = mapped_column(Float, default=14.0)
    state: Mapped[str] = mapped_column(String(10), default=PENDING, index=True)
    slot: Mapped[str | None] = mapped_column(String(24))                # case id | EXTRA
    case_id: Mapped[str | None] = mapped_column(ForeignKey("cases.id"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)  # LIFO key; = delivered_at once delivered
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)     # physically arrived (delivery note / scan)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime)
    ship_at: Mapped[datetime | None] = mapped_column(DateTime)
    order_line_id: Mapped[int | None] = mapped_column(ForeignKey("order_lines.id"), index=True)
    staged_from: Mapped[str | None] = mapped_column(String(24))          # rack case it must be picked from
    picked_at: Mapped[datetime | None] = mapped_column(DateTime)         # physically moved to SHIP area
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime)
    line: Mapped[PoLine] = relationship(back_populates="units")
    order_line: Mapped["OrderLine | None"] = relationship()
    case: Mapped[Case | None] = relationship()

    @property
    def code(self) -> str:
        return f"U{self.id}"


class DeliveryLine(Base):
    """Delivery-note line (DDT): units of a PO line physically received."""
    __tablename__ = "delivery_lines"
    __table_args__ = (UniqueConstraint("dn_number", "po_number", "line_ref"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dn_number: Mapped[str] = mapped_column(String(40))
    po_number: Mapped[str] = mapped_column(String(40), index=True)
    line_ref: Mapped[str] = mapped_column(String(20))
    qty_units: Mapped[int | None] = mapped_column(Integer)              # None = whole line
    delivery_date: Mapped[datetime | None] = mapped_column(DateTime)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # goods dims per unit as printed on the DDT (applied only if the PO had none)
    length_mm: Mapped[float | None] = mapped_column(Float)
    width_mm: Mapped[float | None] = mapped_column(Float)
    height_mm: Mapped[float | None] = mapped_column(Float)
    weight_kg: Mapped[float | None] = mapped_column(Float)
    fragility: Mapped[int | None] = mapped_column(Integer)
    source_file: Mapped[str | None] = mapped_column(String(300))
    row_hash: Mapped[str | None] = mapped_column(String(64))
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


class OrderLine(Base):
    """Customer purchase order line."""
    __tablename__ = "order_lines"
    __table_args__ = (UniqueConstraint("order_number", "line_ref"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_number: Mapped[str] = mapped_column(String(40), index=True)
    line_ref: Mapped[str] = mapped_column(String(20))
    customer: Mapped[str] = mapped_column(String(120), default="")
    sku: Mapped[str] = mapped_column(String(60), index=True)
    qty_units: Mapped[int] = mapped_column(Integer)
    ship_at: Mapped[datetime | None] = mapped_column(DateTime)
    truck_ref: Mapped[str | None] = mapped_column(String(40))
    source_file: Mapped[str | None] = mapped_column(String(300))
    row_hash: Mapped[str | None] = mapped_column(String(64))
    missing_since: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)
    fulfilled: Mapped[bool] = mapped_column(Boolean, default=False)


class Truck(Base):
    __tablename__ = "trucks"
    truck_ref: Mapped[str] = mapped_column(String(40), primary_key=True)
    carrier: Mapped[str] = mapped_column(String(120), default="")
    direction: Mapped[str] = mapped_column(String(8), default="out")    # in | out
    dock: Mapped[str | None] = mapped_column(String(40))
    arrival_at: Mapped[datetime | None] = mapped_column(DateTime)
    departure_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_file: Mapped[str | None] = mapped_column(String(300))
    row_hash: Mapped[str | None] = mapped_column(String(64))
    missing_since: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


class WeightSet(Base):
    __tablename__ = "weight_sets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    size_fit: Mapped[float] = mapped_column(Float)
    dwell: Mapped[float] = mapped_column(Float)
    fragility: Mapped[float] = mapped_column(Float)
    weight: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SlottingScore(Base):
    __tablename__ = "slotting_scores"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(24))
    rank: Mapped[int] = mapped_column(Integer)
    total: Mapped[float] = mapped_column(Float)
    components: Mapped[dict[str, float]] = mapped_column(JSON)
    weight_set_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Event(Base):
    """Anonymous state-change log (PRD C5): feeds SSE replay and weight training."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    type: Mapped[str] = mapped_column(String(30), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class DeadLetter(Base):
    __tablename__ = "dead_letter"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source_file: Mapped[str] = mapped_column(String(300))
    row_number: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str] = mapped_column(String(1000))
    raw: Mapped[dict[str, Any]] = mapped_column(JSON)


class FileState(Base):
    """Last ingested hash per source file (skip unchanged files)."""
    __tablename__ = "file_state"
    path: Mapped[str] = mapped_column(String(300), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(20))
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Param(Base):
    """Runtime parameter overrides set from the input window (applied on top of WH_* settings)."""
    __tablename__ = "params"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
