from invoice_layout.classify import category_rank
from invoice_layout.models import DocumentType


def test_category_order_covers_every_document_type() -> None:
    ordered = sorted(DocumentType, key=category_rank)

    assert ordered == [
        DocumentType.FLIGHT_ITINERARY,
        DocumentType.RAIL_TICKET,
        DocumentType.LODGING_INVOICE,
        DocumentType.LODGING_STATEMENT,
        DocumentType.TAXI_INVOICE,
        DocumentType.TAXI_ITINERARY,
        DocumentType.OTHER_TRANSPORT,
        DocumentType.PHYSICAL_TICKET_PHOTO,
        DocumentType.UNKNOWN,
    ]
