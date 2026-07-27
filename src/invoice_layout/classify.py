"""Stable company ordering for classified travel documents."""

from __future__ import annotations

from .models import DocumentType

_CATEGORY_ORDER = {
    DocumentType.FLIGHT_ITINERARY: 0,
    DocumentType.RAIL_TICKET: 1,
    DocumentType.LODGING_INVOICE: 2,
    DocumentType.LODGING_STATEMENT: 3,
    DocumentType.TAXI_INVOICE: 4,
    DocumentType.TAXI_ITINERARY: 5,
    DocumentType.OTHER_TRANSPORT: 6,
    DocumentType.PHYSICAL_TICKET_PHOTO: 7,
    DocumentType.UNKNOWN: 8,
}


def category_rank(document_type: DocumentType) -> int:
    """Return the company's total ordering rank for one document type."""
    return _CATEGORY_ORDER[document_type]
