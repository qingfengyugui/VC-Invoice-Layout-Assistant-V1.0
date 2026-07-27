from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from invoice_layout.associate import associate
from invoice_layout.models import DocumentType, Observation


def _observation(
    document_type: DocumentType,
    *,
    amount: str | None = None,
    order_number: str | None = None,
    vendor: str | None = None,
    issue: date | None = None,
    start: date | None = None,
    end: date | None = None,
    traveler: str | None = None,
    route: str | None = None,
    confidence: float = 0.9,
) -> Observation:
    return Observation(
        document_type=document_type,
        amount=Decimal(amount) if amount is not None else None,
        order_number=order_number,
        vendor=vendor,
        issue_date=issue,
        service_start=start,
        service_end=end,
        traveler=traveler,
        route=route,
        confidence=confidence,
    )


@pytest.mark.parametrize(
    ("primary_type", "support_type"),
    [
        (DocumentType.TAXI_INVOICE, DocumentType.TAXI_ITINERARY),
        (DocumentType.LODGING_INVOICE, DocumentType.LODGING_STATEMENT),
    ],
)
def test_compatible_invoice_and_support_form_high_confidence_group(
    primary_type: DocumentType,
    support_type: DocumentType,
) -> None:
    pages = {
        "invoice-page": _observation(
            primary_type,
            amount="198.20",
            vendor="Example Mobility, Ltd.",
            start=date(2026, 6, 9),
            end=date(2026, 6, 12),
        ),
        "support-page": _observation(
            support_type,
            amount="198.20",
            vendor=" example mobility ltd ",
            start=date(2026, 6, 9),
            end=date(2026, 6, 12),
        ),
    }

    groups, warnings = associate(pages)

    assert len(groups) == 1
    assert groups[0].primary_page_ids == ("invoice-page",)
    assert groups[0].support_page_ids == ("support-page",)
    assert groups[0].score >= 0.85
    assert groups[0].evidence == ("amount_exact", "vendor", "date_overlap")
    assert groups[0].conflicts == ()
    assert warnings == []


def test_exact_order_number_is_strong_explainable_evidence() -> None:
    pages = {
        "invoice": _observation(DocumentType.TAXI_INVOICE, order_number="ORDER-EXACT-7"),
        "itinerary": _observation(DocumentType.TAXI_ITINERARY, order_number="ORDER-EXACT-7"),
    }

    groups, warnings = associate(pages)

    assert len(groups) == 1
    assert groups[0].score >= 0.65
    assert groups[0].evidence == ("order_exact",)
    assert warnings == []


def test_explicit_conflicts_are_penalties_and_prevent_a_forced_match() -> None:
    pages = {
        "invoice": _observation(
            DocumentType.LODGING_INVOICE,
            amount="440.00",
            vendor="Example Hotel",
            start=date(2026, 4, 1),
            end=date(2026, 4, 2),
            traveler="Person Alpha",
        ),
        "statement": _observation(
            DocumentType.LODGING_STATEMENT,
            amount="880.00",
            vendor="Example Hotel",
            start=date(2026, 5, 1),
            end=date(2026, 5, 2),
            traveler="Person Beta",
        ),
    }

    groups, warnings = associate(pages)

    assert len(groups) == 2
    assert all(group.score == 0 for group in groups)
    assert any(warning.code == "low_confidence_association" for warning in warnings)


def test_incompatible_categories_never_match() -> None:
    pages = {
        "lodging-invoice": _observation(
            DocumentType.LODGING_INVOICE,
            amount="120.00",
            order_number="SAME-ORDER",
        ),
        "taxi-itinerary": _observation(
            DocumentType.TAXI_ITINERARY,
            amount="120.00",
            order_number="SAME-ORDER",
        ),
    }

    groups, warnings = associate(pages)

    assert len(groups) == 2
    assert all(not group.primary_page_ids or not group.support_page_ids for group in groups)
    assert [warning.code for warning in warnings] == ["unmatched_support"]


def test_competing_support_documents_are_not_forced() -> None:
    common = {
        "amount": "36.00",
        "order_number": "ORDER-COMPETE",
        "vendor": "Example Taxi",
        "start": date(2026, 7, 3),
        "end": date(2026, 7, 3),
    }
    pages = {
        "invoice": _observation(DocumentType.TAXI_INVOICE, **common),
        "support-a": _observation(DocumentType.TAXI_ITINERARY, **common),
        "support-b": _observation(DocumentType.TAXI_ITINERARY, **common),
    }

    groups, warnings = associate(pages)

    assert len(groups) == 3
    assert all(not group.primary_page_ids or not group.support_page_ids for group in groups)
    assert [warning.code for warning in warnings] == [
        "ambiguous_association",
        "ambiguous_association",
    ]


def test_low_score_candidate_remains_separate_with_actionable_warning() -> None:
    pages = {
        "invoice": _observation(DocumentType.TAXI_INVOICE, vendor="Example Taxi"),
        "support": _observation(DocumentType.TAXI_ITINERARY, vendor="Example Taxi"),
    }

    groups, warnings = associate(pages)

    assert len(groups) == 2
    assert [warning.code for warning in warnings] == ["low_confidence_association"]
    assert warnings[0].source_page_ids == ("support",)
    assert warnings[0].action


def test_association_is_invariant_to_input_mapping_order() -> None:
    items = [
        (
            "taxi-support",
            _observation(
                DocumentType.TAXI_ITINERARY,
                amount="31.00",
                order_number="TAXI-31",
                vendor="Example Taxi",
            ),
        ),
        ("rail", _observation(DocumentType.RAIL_TICKET)),
        (
            "taxi-invoice",
            _observation(
                DocumentType.TAXI_INVOICE,
                amount="31.00",
                order_number="TAXI-31",
                vendor="example taxi",
            ),
        ),
        ("unknown", _observation(DocumentType.UNKNOWN)),
    ]

    first = associate(dict(items))
    second = associate(dict(reversed(items)))

    assert first == second


def test_every_page_appears_in_exactly_one_group() -> None:
    pages = {
        "flight": _observation(DocumentType.FLIGHT_ITINERARY),
        "rail": _observation(DocumentType.RAIL_TICKET),
        "lodging-invoice": _observation(
            DocumentType.LODGING_INVOICE,
            order_number="LODGE-1",
        ),
        "lodging-statement": _observation(
            DocumentType.LODGING_STATEMENT,
            order_number="LODGE-1",
        ),
        "taxi-invoice": _observation(DocumentType.TAXI_INVOICE, vendor="Only Vendor"),
        "taxi-itinerary": _observation(DocumentType.TAXI_ITINERARY, vendor="Only Vendor"),
        "physical": _observation(DocumentType.PHYSICAL_TICKET_PHOTO),
        "other": _observation(DocumentType.OTHER_TRANSPORT),
        "unknown": _observation(DocumentType.UNKNOWN),
    }

    groups, _ = associate(pages)
    grouped_page_ids = [
        page_id
        for group in groups
        for page_id in (*group.primary_page_ids, *group.support_page_ids)
    ]

    assert sorted(grouped_page_ids) == sorted(pages)
    assert len(grouped_page_ids) == len(set(grouped_page_ids))


def test_warning_messages_do_not_leak_extracted_financial_details() -> None:
    secret_amount = "98765.43"
    secret_vendor = "PRIVATE-VENDOR-MARKER"
    pages = {
        "invoice": _observation(
            DocumentType.TAXI_INVOICE,
            amount=secret_amount,
            vendor=secret_vendor,
        ),
        "support": _observation(
            DocumentType.TAXI_ITINERARY,
            amount="1.00",
            vendor=secret_vendor,
        ),
    }

    _, warnings = associate(pages)
    messages = " ".join(warning.message for warning in warnings)

    assert secret_amount not in messages
    assert secret_vendor not in messages


def test_low_confidence_observations_do_not_auto_match_on_exact_order() -> None:
    pages = {
        "invoice": _observation(
            DocumentType.TAXI_INVOICE,
            order_number="ORDER-LOW-CONFIDENCE",
            confidence=0.2,
        ),
        "support": _observation(
            DocumentType.TAXI_ITINERARY,
            order_number="ORDER-LOW-CONFIDENCE",
            confidence=0.2,
        ),
    }

    groups, warnings = associate(pages)

    assert len(groups) == 2
    assert all(not group.primary_page_ids or not group.support_page_ids for group in groups)
    assert [warning.code for warning in warnings] == ["low_observation_confidence"]


def test_issue_date_is_not_compared_with_service_range() -> None:
    pages = {
        "invoice": _observation(
            DocumentType.LODGING_INVOICE,
            amount="560.00",
            vendor="Example Hotel",
            issue=date(2026, 6, 20),
        ),
        "statement": _observation(
            DocumentType.LODGING_STATEMENT,
            amount="560.00",
            vendor="Example Hotel",
            start=date(2026, 6, 9),
            end=date(2026, 6, 12),
        ),
    }

    groups, warnings = associate(pages)

    assert len(groups) == 1
    assert groups[0].score == 0.7
    assert groups[0].evidence == ("amount_exact", "vendor")
    assert groups[0].conflicts == ()
    assert warnings == []
