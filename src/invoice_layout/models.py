from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class DocumentType(StrEnum):
    FLIGHT_ITINERARY = "flight_itinerary"
    RAIL_TICKET = "rail_ticket"
    LODGING_INVOICE = "lodging_invoice"
    LODGING_STATEMENT = "lodging_statement"
    TAXI_INVOICE = "taxi_invoice"
    TAXI_ITINERARY = "taxi_itinerary"
    PHYSICAL_TICKET_PHOTO = "physical_ticket_photo"
    OTHER_TRANSPORT = "other_transport"
    UNKNOWN = "unknown"


class CropBox(FrozenModel):
    x0: float = Field(ge=0, le=1)
    y0: float = Field(ge=0, le=1)
    x1: float = Field(ge=0, le=1)
    y1: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def ordered(self) -> CropBox:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("crop coordinates must be ordered")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def width(self) -> float:
        return round(self.x1 - self.x0, 10)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def height(self) -> float:
        return round(self.y1 - self.y0, 10)


class SourceFile(FrozenModel):
    id: str
    path: Path
    sha256: str
    media_type: str
    archive_member: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()


class PageAsset(FrozenModel):
    id: str
    source_id: str
    source_path: Path
    page_index: int
    page_pdf: Path
    preview_png: Path
    width_pt: float
    height_pt: float
    pixel_width: int
    pixel_height: int


class Observation(FrozenModel):
    document_type: DocumentType = DocumentType.UNKNOWN
    amount: Decimal | None = None
    issue_date: date | None = None
    service_start: date | None = None
    service_end: date | None = None
    invoice_number: str | None = None
    order_number: str | None = None
    vendor: str | None = None
    traveler: str | None = None
    route: str | None = None
    text: str = ""
    text_boxes: tuple[CropBox, ...] = ()
    qr_boxes: tuple[CropBox, ...] = ()
    confidence: float = Field(default=0, ge=0, le=1)
    evidence: tuple[str, ...] = ()


class AssociationGroup(FrozenModel):
    id: str
    primary_page_ids: tuple[str, ...]
    support_page_ids: tuple[str, ...]
    score: float = Field(ge=0, le=1)
    evidence: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


class Placement(FrozenModel):
    page_asset_id: str
    crop: CropBox
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    output_page_index: int


class WarningItem(FrozenModel):
    code: str
    source_page_ids: tuple[str, ...]
    output_page: int | None
    message: str
    action: str
    severity: str


class PipelineResult(FrozenModel):
    printable_pdf: Path
    sendable_pdf: Path
    report_json: Path
    warnings: tuple[WarningItem, ...] = ()
