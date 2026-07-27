"""Private gray-test release gate for invoice-layout output artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn, cast

from pypdf import PdfReader
from reportlab.lib.pagesizes import A4

from invoice_layout.config import Settings
from invoice_layout.pipeline import run_pipeline

_EXPECTED_KEYS = {
    "page_order",
    "association_groups",
    "included_page_keys",
    "warning_codes",
    "manual_findings",
}
_MANUAL_KEYS = {
    "critical_content_crop_page_keys",
    "visible_normal_page_annotation_page_indexes",
}
_PAGE_KEY = re.compile(r"^[0-9a-f]{64}:[0-9]+$")
_WARNING_CODE = re.compile(r"^[a-z][a-z0-9_]{0,80}$")
_A4_TOLERANCE_PT = 1.0


class _PrivateParseError(ValueError):
    """Signal an argparse failure without echoing private argument values."""


class _PrivateArgumentParser(argparse.ArgumentParser):
    """Suppress argparse usage/errors because paths may contain private data."""

    def error(self, _message: str) -> NoReturn:
        raise _PrivateParseError("invalid command")


def _invalid(message: str) -> NoReturn:
    """Raise one stable validation error for untrusted private inputs."""
    raise ValueError(message)


def _expected_case(raw: object) -> dict[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != _EXPECTED_KEYS:
        raise ValueError("expected schema has unknown or missing keys")
    page_order = _page_keys(raw["page_order"])
    included = _page_keys(raw["included_page_keys"])
    warnings = raw["warning_codes"]
    if not isinstance(warnings, list) or any(
        not isinstance(code, str) or not _WARNING_CODE.fullmatch(code) for code in warnings
    ):
        raise ValueError("expected warning codes are invalid")
    groups_raw = raw["association_groups"]
    if not isinstance(groups_raw, list):
        _invalid("expected association groups are invalid")
    groups: list[tuple[str, ...]] = []
    for group in groups_raw:
        keys = _page_keys(group)
        if not keys:
            raise ValueError("expected association group is empty")
        groups.append(tuple(sorted(keys)))
    manual = raw["manual_findings"]
    if not isinstance(manual, Mapping) or set(manual) != _MANUAL_KEYS:
        raise ValueError("expected manual findings are invalid")
    crops = _page_keys(manual["critical_content_crop_page_keys"])
    annotations = manual["visible_normal_page_annotation_page_indexes"]
    if not isinstance(annotations, list) or any(
        not isinstance(index, int) or isinstance(index, bool) or index < 0
        for index in annotations
    ):
        raise ValueError("expected visible annotation findings are invalid")
    return {
        "page_order": page_order,
        "association_groups": groups,
        "included_page_keys": included,
        "warning_codes": sorted(set(warnings)),
        "manual_findings": {
            "critical_content_crop_page_keys": crops,
            "visible_normal_page_annotation_page_indexes": sorted(set(annotations)),
        },
    }


def _page_keys(value: object) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(key, str) or not _PAGE_KEY.fullmatch(key) for key in value
    ):
        raise ValueError("expected page keys must use full SHA-256 identifiers")
    if len(value) != len(set(value)):
        raise ValueError("expected page keys must be unique")
    return list(value)


def _page_key_maps(report: Mapping[str, object]) -> tuple[dict[str, str], set[str]]:
    sources = report.get("sources")
    pages = report.get("pages")
    if not isinstance(sources, list) or not isinstance(pages, list):
        _invalid("pipeline report has no source/page inventory")
    source_hashes: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            _invalid("pipeline report source record is invalid")
        source_id = source.get("source_id")
        sha256 = source.get("sha256")
        if not isinstance(source_id, str) or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("pipeline report source hash is invalid")
        source_hashes[source_id] = sha256
    page_keys: dict[str, str] = {}
    included: set[str] = set()
    for page in pages:
        if not isinstance(page, Mapping):
            _invalid("pipeline report page record is invalid")
        page_id = page.get("page_id")
        source_id = page.get("source_id")
        index = page.get("page_index")
        if (
            not isinstance(page_id, str)
            or not isinstance(source_id, str)
            or not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or source_id not in source_hashes
        ):
            raise ValueError("pipeline report page identifier is invalid")
        page_keys[page_id] = f"{source_hashes[source_id]}:{index}"
        if page.get("included_in_ticket_pages") is True:
            included.add(page_keys[page_id])
    return page_keys, included


def _mapped_page_key(page_id: object, page_keys: Mapping[str, str]) -> str:
    if not isinstance(page_id, str) or page_id not in page_keys:
        raise ValueError("pipeline report references an unknown page")
    return page_keys[page_id]


def _placements(report: Mapping[str, object], page_keys: Mapping[str, str]) -> list[tuple[str, int, float, float]]:
    raw = report.get("placements")
    if not isinstance(raw, list):
        _invalid("pipeline report placements are invalid")
    placements: list[tuple[str, int, float, float]] = []
    for placement in raw:
        if not isinstance(placement, Mapping):
            _invalid("pipeline report placement is invalid")
        page_key = _mapped_page_key(placement.get("page_asset_id"), page_keys)
        output_index = placement.get("output_page_index")
        x = placement.get("x_mm")
        y = placement.get("y_mm")
        if (
            not isinstance(output_index, int)
            or isinstance(output_index, bool)
            or output_index < 0
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
        ):
            raise ValueError("pipeline report placement coordinates are invalid")
        placements.append((page_key, output_index, float(y), float(x)))
    return sorted(placements, key=lambda item: (item[1], item[2], item[3], item[0]))


def _association_groups(report: Mapping[str, object], page_keys: Mapping[str, str]) -> set[tuple[str, ...]]:
    raw = report.get("associations")
    if not isinstance(raw, list):
        _invalid("pipeline report associations are invalid")
    groups: set[tuple[str, ...]] = set()
    for group in raw:
        if not isinstance(group, Mapping):
            _invalid("pipeline report association is invalid")
        primary = group.get("primary_page_ids")
        support = group.get("support_page_ids")
        if not isinstance(primary, list) or not isinstance(support, list):
            _invalid("pipeline report association pages are invalid")
        members = tuple(sorted(_mapped_page_key(item, page_keys) for item in [*primary, *support]))
        if not members or len(members) != len(set(members)):
            raise ValueError("pipeline report association members are invalid")
        groups.add(members)
    return groups


def _warning_codes(report: Mapping[str, object]) -> set[str]:
    raw = report.get("warnings", [])
    if not isinstance(raw, list):
        _invalid("pipeline report warnings are invalid")
    result: set[str] = set()
    for warning in raw:
        if not isinstance(warning, Mapping) or not isinstance(warning.get("code"), str):
            _invalid("pipeline report warning code is invalid")
        code = cast(str, warning["code"])
        result.add(code if _WARNING_CODE.fullmatch(code) else _hashed_identifier(code))
    return result


def _hashed_identifier(value: str) -> str:
    return "H-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _pdf_metrics(sendable_pdf: Path, ticket_output_indexes: set[int]) -> tuple[float, int, int]:
    reader = PdfReader(sendable_pdf)
    a4_pages = 0
    annotation_count = 0
    for page in reader.pages:
        box = page.mediabox
        width = float(box.width)
        height = float(box.height)
        if abs(width - A4[0]) <= _A4_TOLERANCE_PT and abs(height - A4[1]) <= _A4_TOLERANCE_PT:
            a4_pages += 1
        annotations = page.get("/Annots")
        annotation_count += len(annotations) if annotations is not None else 0
    page_count = len(reader.pages)
    rate = a4_pages / page_count if page_count else 0.0
    return rate, max(0, page_count - len(ticket_output_indexes)), annotation_count


def evaluate_gray_case(
    pipeline_report: Mapping[str, object],
    expected: Mapping[str, object],
    sendable_pdf: Path,
) -> dict[str, object]:
    """Evaluate a private pipeline report without copying its sensitive fields."""
    normalized = _expected_case(expected)
    page_keys, actual_included = _page_key_maps(pipeline_report)
    placements = _placements(pipeline_report, page_keys)
    actual_order = [item[0] for item in placements]
    expected_order = cast(list[str], normalized["page_order"])
    expected_included = set(cast(list[str], normalized["included_page_keys"]))
    expected_groups = set(cast(list[tuple[str, ...]], normalized["association_groups"]))
    actual_groups = _association_groups(pipeline_report, page_keys)
    expected_warning_codes = set(cast(list[str], normalized["warning_codes"]))
    actual_warning_codes = _warning_codes(pipeline_report)
    expected_manual = cast(Mapping[str, object], normalized["manual_findings"])
    critical_crops = cast(list[str], expected_manual["critical_content_crop_page_keys"])
    visible_annotations = cast(list[int], expected_manual["visible_normal_page_annotation_page_indexes"])

    placed_keys = set(actual_order)
    duplicate_count = len(actual_order) - len(placed_keys)
    omitted = expected_included - placed_keys
    length = max(len(expected_order), len(actual_order))
    matching = sum(
        expected_key == actual_key
        for expected_key, actual_key in zip(expected_order, actual_order, strict=False)
    )
    page_order_accuracy = matching / length if length else 1.0
    association_accuracy = 1.0 if actual_groups == expected_groups else 0.0
    warning_recall = (
        len(expected_warning_codes & actual_warning_codes) / len(expected_warning_codes)
        if expected_warning_codes else 1.0
    )
    a4_rate, review_count, pdf_annotations = _pdf_metrics(
        Path(sendable_pdf), {item[1] for item in placements}
    )
    normal_annotation_count = pdf_annotations + len(visible_annotations)
    gate = (
        len(critical_crops) == 0
        and len(omitted) == 0
        and duplicate_count == 0
        and normal_annotation_count == 0
        and review_count == 0
        and a4_rate == 1.0
        and page_order_accuracy == 1.0
        and association_accuracy == 1.0
        and warning_recall == 1.0
        and actual_included == expected_included
    )
    return {
        "schema_version": 1,
        "release_gate_passed": gate,
        "expected_page_count": len(expected_included),
        "actual_included_page_count": len(actual_included),
        "actual_placement_count": len(actual_order),
        "page_order_accuracy": page_order_accuracy,
        "association_accuracy": association_accuracy,
        "omission_count": len(omitted),
        "unexpected_duplicate_count": duplicate_count,
        "critical_content_crop_count": len(critical_crops),
        "visible_normal_page_annotation_count": len(visible_annotations),
        "normal_page_annotation_count": normal_annotation_count,
        "a4_verification_pass_rate": a4_rate,
        "sendable_review_page_count": review_count,
        "review_warning_recall": warning_recall,
        "expected_warning_codes": sorted(expected_warning_codes),
        "actual_warning_codes": sorted(actual_warning_codes),
        "failed_page_keys": sorted(omitted),
    }


def _paths_alias(first: Path, second: Path) -> bool:
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _paths_overlap(first: Path, second: Path) -> bool:
    if _paths_alias(first, second):
        return True
    resolved_first = first.resolve(strict=False)
    resolved_second = second.resolve(strict=False)
    return (
        resolved_first == resolved_second
        or resolved_first in resolved_second.parents
        or resolved_second in resolved_first.parents
    )


def _gray_work_dir(output_dir: Path) -> Path:
    return output_dir.parent / f".{output_dir.name}.gray-test-work"


def _validate_cli_paths(
    inputs: Sequence[Path],
    output_dir: Path,
    expected: Path,
    observations: Path | None,
    work_dir: Path,
) -> None:
    if not inputs or any(not path.exists() or not (path.is_file() or path.is_dir()) for path in inputs):
        raise ValueError("invalid input")
    if not expected.is_file() or (output_dir.exists() and not output_dir.is_dir()):
        raise ValueError("invalid destination")
    if observations is not None and not observations.is_file():
        raise ValueError("invalid observations")
    planned = (output_dir, expected, work_dir)
    for index, first in enumerate(planned):
        if any(_paths_overlap(first, second) for second in planned[index + 1:]):
            raise ValueError("private destinations overlap")
    for input_path in inputs:
        if any(_paths_overlap(input_path, destination) for destination in planned):
            raise ValueError("private destinations overlap inputs")
        if observations is not None and _paths_overlap(input_path, observations):
            raise ValueError("observations overlap inputs")
    if observations is not None and any(
        _paths_overlap(observations, destination) for destination in planned
    ):
        raise ValueError("observations overlap private destinations")


def _load_expected(path: Path) -> dict[str, object]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid expected file") from error
    if not isinstance(parsed, dict):
        _invalid("invalid expected file")
    return parsed


def _write_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".gray-report.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = _PrivateArgumentParser(description="Run a private invoice gray-test release gate.")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--expected", required=True, type=Path)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--provider", choices=("auto", "host", "local"), default="auto")
    try:
        args = parser.parse_args(argv)
        inputs = [Path(item) for item in args.inputs]
        output_dir = Path(args.output_dir)
        expected_path = Path(args.expected)
        observations = Path(args.observations) if args.observations is not None else None
        work_dir = _gray_work_dir(output_dir)
        if args.provider == "host" and args.observations is None:
            raise ValueError("host observations required")
        _validate_cli_paths(inputs, output_dir, expected_path, observations, work_dir)
        expected = _load_expected(expected_path)
        result = run_pipeline(
            inputs,
            output_dir,
            Settings(
                provider=args.provider,
                host_manifest=observations,
                work_dir=work_dir,
            ),
        )
        pipeline_report = _load_expected(result.report_json)
        report = evaluate_gray_case(pipeline_report, expected, result.sendable_pdf)
        _write_atomic(output_dir / "gray-report.json", report)
    except (SystemExit, _PrivateParseError):
        print("error: private gray-test processing failed", file=os.sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - privacy boundary intentionally hides paths/data.
        print("error: private gray-test processing failed", file=os.sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["release_gate_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
