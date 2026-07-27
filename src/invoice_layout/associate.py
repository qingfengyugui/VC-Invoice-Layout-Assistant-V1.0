"""Deterministic, explainable association of invoices and support documents."""

from __future__ import annotations

import hashlib
import math
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from .classify import category_rank
from .models import AssociationGroup, DocumentType, Observation, WarningItem

WEIGHTS = {
    "order_exact": 0.55,
    "amount_exact": 0.35,
    "vendor": 0.15,
    "date_overlap": 0.15,
    "traveler": 0.10,
    "route": 0.10,
    "amount_conflict": -0.45,
    "date_conflict": -0.30,
    "traveler_conflict": -0.25,
}
COMPATIBILITY_BASE = 0.20
HIGH_CONFIDENCE = 0.85
MIN_MATCH = 0.65
AMBIGUITY_DELTA = 0.08
MIN_OBSERVATION_CONFIDENCE = 0.65

_SUPPORT_TO_PRIMARY = {
    DocumentType.LODGING_STATEMENT: DocumentType.LODGING_INVOICE,
    DocumentType.TAXI_ITINERARY: DocumentType.TAXI_INVOICE,
}
_EVIDENCE_ORDER = ("order_exact", "amount_exact", "vendor", "date_overlap", "traveler", "route")
_CONFLICT_ORDER = ("amount_conflict", "date_conflict", "traveler_conflict")


@dataclass(frozen=True)
class _Candidate:
    primary_id: str
    support_id: str
    score: float
    evidence: tuple[str, ...]
    conflicts: tuple[str, ...]
    confidence_limited: bool


def _normalized(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )
    return normalized or None


def _service_date_range(observation: Observation) -> tuple[date, date] | None:
    service_dates = [
        value
        for value in (observation.service_start, observation.service_end)
        if value is not None
    ]
    if not service_dates:
        return None
    return min(service_dates), max(service_dates)


def _score(
    primary: Observation,
    support: Observation,
) -> tuple[float, tuple[str, ...], tuple[str, ...], bool]:
    evidence: list[str] = []
    conflicts: list[str] = []

    if (
        primary.order_number is not None
        and support.order_number is not None
        and primary.order_number.strip() == support.order_number.strip()
    ):
        evidence.append("order_exact")

    if primary.amount is not None and support.amount is not None:
        if primary.amount == support.amount:
            evidence.append("amount_exact")
        else:
            conflicts.append("amount_conflict")

    primary_vendor = _normalized(primary.vendor)
    support_vendor = _normalized(support.vendor)
    if primary_vendor is not None and support_vendor is not None and primary_vendor == support_vendor:
        evidence.append("vendor")

    primary_dates = _service_date_range(primary)
    support_dates = _service_date_range(support)
    if primary_dates is not None and support_dates is not None:
        if primary_dates[0] <= support_dates[1] and support_dates[0] <= primary_dates[1]:
            evidence.append("date_overlap")
        else:
            conflicts.append("date_conflict")
    elif primary.issue_date is not None and support.issue_date is not None:
        if primary.issue_date == support.issue_date:
            evidence.append("date_overlap")
        else:
            conflicts.append("date_conflict")

    primary_traveler = _normalized(primary.traveler)
    support_traveler = _normalized(support.traveler)
    if primary_traveler is not None and support_traveler is not None:
        if primary_traveler == support_traveler:
            evidence.append("traveler")
        else:
            conflicts.append("traveler_conflict")

    primary_route = _normalized(primary.route)
    support_route = _normalized(support.route)
    if primary_route is not None and support_route is not None and primary_route == support_route:
        evidence.append("route")

    raw_score = COMPATIBILITY_BASE
    raw_score += sum(WEIGHTS[item] for item in evidence)
    raw_score += sum(WEIGHTS[item] for item in conflicts)
    confidence_limited = min(primary.confidence, support.confidence) < MIN_OBSERVATION_CONFIDENCE
    score = 0.0 if confidence_limited else round(max(0.0, min(1.0, raw_score)), 4)
    ordered_evidence = tuple(item for item in _EVIDENCE_ORDER if item in evidence)
    ordered_conflicts = tuple(item for item in _CONFLICT_ORDER if item in conflicts)
    return score, ordered_evidence, ordered_conflicts, confidence_limited


def _candidate_edges(pages: Mapping[str, Observation]) -> list[_Candidate]:
    primaries: dict[DocumentType, list[tuple[str, Observation]]] = defaultdict(list)
    supports: list[tuple[str, Observation]] = []
    for page_id, observation in sorted(pages.items()):
        if observation.document_type in _SUPPORT_TO_PRIMARY:
            supports.append((page_id, observation))
        else:
            primaries[observation.document_type].append((page_id, observation))

    candidates: list[_Candidate] = []
    for support_id, support in supports:
        compatible_type = _SUPPORT_TO_PRIMARY[support.document_type]
        for primary_id, primary in primaries[compatible_type]:
            score, evidence, conflicts, confidence_limited = _score(primary, support)
            candidates.append(
                _Candidate(
                    primary_id=primary_id,
                    support_id=support_id,
                    score=score,
                    evidence=evidence,
                    conflicts=conflicts,
                    confidence_limited=confidence_limited,
                )
            )
    return sorted(candidates, key=lambda item: (item.support_id, item.primary_id))


def _warning(code: str, support_id: str) -> WarningItem:
    messages = {
        "ambiguous_association": "A support document has multiple plausible associations.",
        "association_competition": "A support document could not be assigned one-to-one.",
        "low_confidence_association": "A possible document association lacks sufficient evidence.",
        "low_observation_confidence": "A possible association relies on a low-confidence observation.",
        "unmatched_support": "No compatible primary document was found for a support document.",
    }
    return WarningItem(
        code=code,
        source_page_ids=(support_id,),
        output_page=None,
        message=messages[code],
        action="Review the support document and its intended primary document.",
        severity="warning",
    )


def _ambiguous_supports(candidates: Sequence[_Candidate]) -> tuple[set[str], dict[str, str]]:
    by_support: dict[str, list[_Candidate]] = defaultdict(list)
    for candidate in candidates:
        by_support[candidate.support_id].append(candidate)

    rejected: set[str] = set()
    reasons: dict[str, str] = {}
    for support_id, choices in sorted(by_support.items()):
        ranked = sorted(choices, key=lambda item: (-item.score, item.primary_id))
        if ranked[0].score < MIN_MATCH:
            rejected.add(support_id)
            reasons[support_id] = (
                "low_observation_confidence"
                if all(candidate.confidence_limited for candidate in ranked)
                else "low_confidence_association"
            )
        elif len(ranked) > 1 and ranked[0].score - ranked[1].score < AMBIGUITY_DELTA:
            rejected.add(support_id)
            reasons[support_id] = "ambiguous_association"

    by_primary: dict[str, list[_Candidate]] = defaultdict(list)
    for candidate in candidates:
        if candidate.support_id not in rejected and candidate.score >= MIN_MATCH:
            by_primary[candidate.primary_id].append(candidate)
    for choices in by_primary.values():
        ranked = sorted(choices, key=lambda item: (-item.score, item.support_id))
        if len(ranked) < 2 or ranked[0].score - ranked[1].score >= AMBIGUITY_DELTA:
            continue
        best_score = ranked[0].score
        for candidate in ranked:
            if best_score - candidate.score >= AMBIGUITY_DELTA:
                break
            rejected.add(candidate.support_id)
            reasons[candidate.support_id] = "ambiguous_association"
    return rejected, reasons


def _maximum_weight_matching(candidates: Sequence[_Candidate]) -> dict[str, _Candidate]:
    support_ids = sorted({candidate.support_id for candidate in candidates})
    primary_ids = sorted({candidate.primary_id for candidate in candidates})
    if not support_ids or not primary_ids:
        return {}

    by_pair = {(candidate.support_id, candidate.primary_id): candidate for candidate in candidates}
    columns = [*primary_ids, *(f"\0dummy:{support_id}" for support_id in support_ids)]
    row_count = len(support_ids)
    column_count = len(columns)
    unavailable_cost = 1_000_000.0

    def cost(row: int, column: int) -> float:
        support_id = support_ids[row - 1]
        primary_id = columns[column - 1]
        if primary_id.startswith("\0dummy:"):
            return 0.0
        candidate = by_pair.get((support_id, primary_id))
        return -candidate.score if candidate is not None else unavailable_cost

    row_potential = [0.0] * (row_count + 1)
    column_potential = [0.0] * (column_count + 1)
    assigned_row = [0] * (column_count + 1)
    predecessor = [0] * (column_count + 1)

    for row in range(1, row_count + 1):
        assigned_row[0] = row
        column_zero = 0
        minimum = [math.inf] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[column_zero] = True
            current_row = assigned_row[column_zero]
            delta = math.inf
            next_column = 0
            for column in range(1, column_count + 1):
                if used[column]:
                    continue
                reduced_cost = (
                    cost(current_row, column) - row_potential[current_row] - column_potential[column]
                )
                if reduced_cost < minimum[column]:
                    minimum[column] = reduced_cost
                    predecessor[column] = column_zero
                if minimum[column] < delta:
                    delta = minimum[column]
                    next_column = column
            for column in range(column_count + 1):
                if used[column]:
                    row_potential[assigned_row[column]] += delta
                    column_potential[column] -= delta
                else:
                    minimum[column] -= delta
            column_zero = next_column
            if assigned_row[column_zero] == 0:
                break
        while True:
            previous_column = predecessor[column_zero]
            assigned_row[column_zero] = assigned_row[previous_column]
            column_zero = previous_column
            if column_zero == 0:
                break

    matching: dict[str, _Candidate] = {}
    for column, row in enumerate(assigned_row[1:], start=1):
        if row == 0:
            continue
        support_id = support_ids[row - 1]
        primary_id = columns[column - 1]
        candidate = by_pair.get((support_id, primary_id))
        if candidate is not None:
            matching[support_id] = candidate
    return matching


def _group_id(page_ids: Sequence[str]) -> str:
    membership = "\x1f".join(sorted(page_ids)).encode("utf-8")
    return f"group-{hashlib.sha256(membership).hexdigest()[:16]}"


def _group_sort_key(
    group: AssociationGroup,
    pages: Mapping[str, Observation],
) -> tuple[int, tuple[str, ...]]:
    representative = (group.primary_page_ids or group.support_page_ids)[0]
    return category_rank(pages[representative].document_type), tuple(
        sorted((*group.primary_page_ids, *group.support_page_ids))
    )


def associate(
    pages: Mapping[str, Observation],
) -> tuple[list[AssociationGroup], list[WarningItem]]:
    """Associate compatible primary/support pages without forcing uncertain matches."""
    candidates = _candidate_edges(pages)
    rejected, rejection_reasons = _ambiguous_supports(candidates)
    eligible = [
        candidate
        for candidate in candidates
        if candidate.support_id not in rejected and candidate.score >= MIN_MATCH
    ]
    matching = _maximum_weight_matching(eligible)

    groups: list[AssociationGroup] = []
    grouped_page_ids: set[str] = set()
    for support_id, candidate in sorted(matching.items()):
        members = (candidate.primary_id, support_id)
        groups.append(
            AssociationGroup(
                id=_group_id(members),
                primary_page_ids=(candidate.primary_id,),
                support_page_ids=(support_id,),
                score=candidate.score,
                evidence=candidate.evidence,
                conflicts=candidate.conflicts,
            )
        )
        grouped_page_ids.update(members)

    warnings: list[WarningItem] = []
    support_ids = {
        page_id
        for page_id, observation in pages.items()
        if observation.document_type in _SUPPORT_TO_PRIMARY
    }
    candidate_support_ids = {candidate.support_id for candidate in candidates}
    for support_id in sorted(support_ids):
        if support_id in matching:
            continue
        if support_id in rejection_reasons:
            code = rejection_reasons[support_id]
        elif support_id not in candidate_support_ids:
            code = "unmatched_support"
        else:
            code = "association_competition"
        warnings.append(_warning(code, support_id))

    for page_id, observation in sorted(pages.items()):
        if page_id in grouped_page_ids:
            continue
        is_support = observation.document_type in _SUPPORT_TO_PRIMARY
        groups.append(
            AssociationGroup(
                id=_group_id((page_id,)),
                primary_page_ids=() if is_support else (page_id,),
                support_page_ids=(page_id,) if is_support else (),
                score=0.0,
            )
        )

    groups.sort(key=lambda group: _group_sort_key(group, pages))
    warnings.sort(key=lambda warning: (warning.source_page_ids, warning.code))
    return groups, warnings
