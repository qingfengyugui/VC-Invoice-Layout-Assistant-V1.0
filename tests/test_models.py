from decimal import Decimal

from invoice_layout.models import CropBox, DocumentType, Observation


def test_crop_box_is_normalized_and_ordered() -> None:
    box = CropBox(x0=0.1, y0=0.2, x1=0.9, y1=0.8)
    assert box.width == 0.8
    assert box.height == 0.6


def test_observation_uses_decimal_amounts() -> None:
    item = Observation(document_type=DocumentType.RAIL_TICKET, amount=Decimal("84.00"))
    assert item.amount == Decimal("84.00")
