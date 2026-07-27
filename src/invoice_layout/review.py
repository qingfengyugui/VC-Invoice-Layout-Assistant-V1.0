"""Build private audit reports and annotation-free PDF output variants."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path, PurePath

from pydantic import BaseModel
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor  # type: ignore[import-untyped]
from reportlab.lib.pagesizes import A4  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from .models import PipelineResult, WarningItem

_TITLE = "打印前核对页"
_BANNER = "不属于报销附件，请勿寄送"
_REPORT_REFERENCE = "完整详情请查看机器报告 report.json"
_UNKNOWN_SOURCE = "未知来源"
_UNLOCATED_PAGE = "未定位"
_SOURCE_LABEL = "来源"
_OUTPUT_PAGE_LABEL = "输出页"
_PROBLEM_LABEL = "问题"
_ACTION_LABEL = "处理"
_CHECK_LABEL = "建议核对"
_FONT = "STSong-Light"
_SECRET_KEY = re.compile(
    r"(authorization|token|api.?key|password|cookie|client.?secret|credential)",
    re.IGNORECASE,
)
_PRC_ID = re.compile(r"(?<!\d)\d{17}[\dXx](?![\dXx])")
_BANK_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){15,18}\d(?!\d)")


def build_outputs(
    ticket_pdf: Path,
    warnings: Sequence[WarningItem],
    manifest: Mapping[str, object],
    output_dir: Path,
) -> PipelineResult:
    """Create printable, sendable, and machine-report outputs atomically.

    Ticket pages are copied as-is for the sendable variant. The printable
    variant always receives one final standalone review page.
    """
    ticket_pdf = Path(ticket_pdf)
    output_dir = Path(output_dir)
    destinations = {
        "printable": output_dir / "printable.pdf",
        "sendable": output_dir / "sendable.pdf",
        "report": output_dir / "report.json",
    }
    _validate_destinations(ticket_pdf, destinations.values())
    _validate_ticket_pdf(ticket_pdf)
    output_dir.mkdir(parents=True, exist_ok=True)

    staged: dict[str, Path] = {}
    try:
        for name, destination in destinations.items():
            staged[name] = _temporary_path(destination)
        warning_records = _warning_records(warnings, manifest)
        report = _build_report(manifest, warning_records)
        shutil.copyfile(ticket_pdf, staged["sendable"])
        _write_printable_with_review(
            ticket_pdf, staged["printable"], warning_records
        )
        _write_report(staged["report"], report)
        _commit_staged(staged, destinations)
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)

    return PipelineResult(
        printable_pdf=destinations["printable"],
        sendable_pdf=destinations["sendable"],
        report_json=destinations["report"],
        warnings=tuple(warnings),
    )


def _validate_ticket_pdf(ticket_pdf: Path) -> None:
    if not ticket_pdf.is_file():
        raise ValueError(f"ticket PDF does not exist: {ticket_pdf}")
    reader = PdfReader(ticket_pdf)
    if reader.is_encrypted:
        raise ValueError("ticket PDF must not be encrypted")


def _validate_destinations(ticket_pdf: Path, destinations: Iterable[Path]) -> None:
    for destination in destinations:
        if _paths_alias(ticket_pdf, destination):
            raise ValueError(f"output aliases ticket PDF: {destination.name}")


def _paths_alias(first: Path, second: Path) -> bool:
    try:
        if first.resolve(strict=False) == second.resolve(strict=False):
            return True
    except (OSError, RuntimeError):
        pass
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _temporary_path(destination: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    return Path(name)


def _commit_staged(
    staged: Mapping[str, Path],
    destinations: Mapping[str, Path],
) -> None:
    """Publish all outputs or restore the complete previous generation."""
    order = tuple(destinations)
    backups: dict[str, Path] = {}
    originals_moved: set[str] = set()
    installed: set[str] = set()
    try:
        for name in order:
            destination = destinations[name]
            if destination.exists():
                backup = _unused_backup_path(destination)
                backups[name] = backup
                os.replace(destination, backup)
                originals_moved.add(name)
        for name in order:
            os.replace(staged[name], destinations[name])
            installed.add(name)
    except BaseException as publish_error:
        rollback_errors: list[OSError] = []
        for name in reversed(order):
            destination = destinations[name]
            if name in installed:
                try:
                    destination.unlink(missing_ok=True)
                except OSError as error:
                    rollback_errors.append(error)
            if name in originals_moved:
                try:
                    os.replace(backups[name], destination)
                except OSError as error:
                    rollback_errors.append(error)
        if rollback_errors:
            details = "; ".join(str(error) for error in rollback_errors)
            raise RuntimeError(
                f"output publish failed and rollback was incomplete: {details}"
            ) from publish_error
        raise
    else:
        for backup in backups.values():
            backup.unlink(missing_ok=True)


def _unused_backup_path(destination: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".backup.tmp", dir=destination.parent
    )
    os.close(descriptor)
    backup = Path(name)
    backup.unlink()
    return backup


def _write_printable_with_review(
    ticket_pdf: Path,
    destination: Path,
    warning_records: Sequence[Mapping[str, object]],
) -> None:
    review_pdf = _render_review_page(warning_records)
    ticket_reader = PdfReader(ticket_pdf)
    review_reader = PdfReader(io.BytesIO(review_pdf))
    writer = PdfWriter()
    for page in ticket_reader.pages:
        writer.add_page(page)
    writer.add_page(review_reader.pages[0])
    with destination.open("wb") as output:
        writer.write(output)


def _render_review_page(
    warning_records: Sequence[Mapping[str, object]],
) -> bytes:
    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    stream = io.BytesIO()
    page = Canvas(
        stream,
        pagesize=A4,
        pageCompression=1,
        invariant=1,
    )
    width, height = A4
    margin = 36.0

    page.setFont(_FONT, 18)
    page.drawString(margin, height - 44, _TITLE)
    page.setFillColor(HexColor("#8B1E1E"))
    page.roundRect(margin, height - 88, width - margin * 2, 28, 4, fill=1, stroke=0)
    page.setFillColor(HexColor("#FFFFFF"))
    page.setFont(_FONT, 11)
    page.drawCentredString(width / 2, height - 78, _BANNER)
    page.setFillColor(HexColor("#222222"))

    y = height - 112
    bottom = 54.0
    rendered = 0
    for record in warning_records:
        source_items = record["sources"]
        if not isinstance(source_items, (list, tuple)):
            source_items = (source_items,)
        source = "、".join(str(item) for item in source_items) or _UNKNOWN_SOURCE
        output_page = record["output_page"]
        page_label = _UNLOCATED_PAGE if output_page is None else str(output_page)
        lines = [
            (
                f'{record["id"]}  {_SOURCE_LABEL}: {source}  '
                f'{_OUTPUT_PAGE_LABEL}: {page_label}'
            ),
            f'{_PROBLEM_LABEL}: {record["problem"]}',
            f'{_ACTION_LABEL}: {record["action"]}',
            f'{_CHECK_LABEL}: {record["suggested_check"]}',
        ]
        wrapped: list[str] = []
        for line, limit in zip(lines, (2, 2, 2, 2), strict=True):
            wrapped.extend(
                _truncated_lines(line, width - margin * 2, 7.2, limit)
            )
        row_height = len(wrapped) * 9.5 + 10
        if y - row_height < bottom + 18:
            break
        page.setFont(_FONT, 7.2)
        for line in wrapped:
            page.drawString(margin, y, line)
            y -= 9.5
        page.setStrokeColor(HexColor("#D8D8D8"))
        page.line(margin, y + 3, width - margin, y + 3)
        y -= 7
        rendered += 1

    if rendered < len(warning_records):
        omitted = len(warning_records) - rendered
        reference = f"另有 {omitted} 条提醒未在本页展开；{_REPORT_REFERENCE}"
    else:
        reference = _REPORT_REFERENCE
    page.setFillColor(HexColor("#555555"))
    page.setFont(_FONT, 8)
    page.drawString(margin, 36, reference)
    page.save()
    return stream.getvalue()


def _wrap_text(text: str, max_width: float, font_size: float) -> list[str]:
    if not text:
        return [""]
    result: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        if current and pdfmetrics.stringWidth(candidate, _FONT, font_size) > max_width:
            result.append(current)
            current = character
        else:
            current = candidate
    if current:
        result.append(current)
    return result


def _truncated_lines(
    text: str, max_width: float, font_size: float, max_lines: int
) -> list[str]:
    lines = _wrap_text(text, max_width, font_size)
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    ellipsis = "…"
    last = kept[-1]
    while last and pdfmetrics.stringWidth(last + ellipsis, _FONT, font_size) > max_width:
        last = last[:-1]
    kept[-1] = last + ellipsis
    return kept


def _warning_records(
    warnings: Sequence[WarningItem],
    manifest: Mapping[str, object],
) -> list[dict[str, object]]:
    page_sources_raw = manifest.get("page_sources", {})
    page_sources = (
        page_sources_raw if isinstance(page_sources_raw, Mapping) else {}
    )
    records: list[dict[str, object]] = []
    for warning in warnings:
        sources: list[str] = []
        for page_id in warning.source_page_ids:
            source_value = page_sources.get(page_id)
            if source_value is None:
                continue
            candidates = (
                source_value
                if isinstance(source_value, (list, tuple))
                else (source_value,)
            )
            for candidate in candidates:
                basename = _basename(str(candidate))
                if basename and basename not in sources:
                    sources.append(basename)
        canonical = json.dumps(
            warning.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        warning_id = "W-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[
            :10
        ].upper()
        records.append(
            {
                "id": warning_id,
                "code": warning.code,
                "sources": sources,
                "source_page_ids": list(warning.source_page_ids),
                "output_page": warning.output_page,
                "problem": _sanitize_string(warning.message, "message"),
                "action": _sanitize_string(warning.action, "action"),
                "suggested_check": _suggested_check(warning),
                "severity": warning.severity,
            }
        )
    return records


def _suggested_check(warning: WarningItem) -> str:
    if warning.output_page is None:
        return "确认票据是否遗漏，并对照原始文件核验"
    return "对照原始票据核验金额、日期、号码和关联关系"


def _build_report(
    manifest: Mapping[str, object],
    warning_records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    cleaned = _sanitize(manifest)
    report = dict(cleaned) if isinstance(cleaned, dict) else {"manifest": cleaned}
    report["warnings"] = _sanitize(warning_records)
    return report


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    encoded = (
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")
    with path.open("wb") as output:
        output.write(encoded)


def _sanitize(value: object, key_hint: str = "") -> object:
    if _SECRET_KEY.search(key_hint):
        return "[REDACTED]"
    if isinstance(value, type(os.environ)):
        return "[REDACTED ENVIRONMENT]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_string(value, key_hint)
    if isinstance(value, PurePath):
        return value.name
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return _sanitize(value.value, key_hint)
    if isinstance(value, BaseModel):
        return _sanitize(value.model_dump(mode="json"), key_hint)
    if isinstance(value, Mapping):
        cleaned: dict[str, object] = {}
        for raw_key in sorted(value, key=lambda item: str(item)):
            original_key = str(raw_key)
            key = _unique_mapping_key(
                cleaned, _sanitize_mapping_key(original_key)
            )
            cleaned[key] = _sanitize(value[raw_key], original_key)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, key_hint) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_sanitize(item, key_hint) for item in value]
        return sorted(items, key=_canonical_sort_key)
    return f"[UNSERIALIZABLE {type(value).__name__}]"


def _canonical_sort_key(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _sanitize_string(value: str, key_hint: str) -> str:
    if _SECRET_KEY.search(key_hint):
        return "[REDACTED]"
    sanitized = _PRC_ID.sub("[REDACTED PRC ID]", value)
    sanitized = _BANK_CARD.sub("[REDACTED BANK CARD]", sanitized)
    if _is_absolute_path(sanitized) or _is_path_field(key_hint):
        return _basename(sanitized)
    return sanitized


def _sanitize_mapping_key(value: str) -> str:
    if _SECRET_KEY.search(value):
        return "[REDACTED KEY]"
    sanitized = _PRC_ID.sub("[REDACTED PRC ID]", value)
    sanitized = _BANK_CARD.sub("[REDACTED BANK CARD]", sanitized)
    if _is_absolute_path(sanitized):
        return _basename(sanitized)
    return sanitized or "[EMPTY KEY]"


def _unique_mapping_key(existing: Mapping[str, object], base: str) -> str:
    if base not in existing:
        return base
    suffix = 2
    while f"{base}#{suffix}" in existing:
        suffix += 1
    return f"{base}#{suffix}"


def _is_absolute_path(value: str) -> bool:
    return (
        value.startswith(("/", "\\\\"))
        or bool(re.match(r"^[A-Za-z]:[\\/]", value))
    )


def _is_path_field(key_hint: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", key_hint.casefold())
    return normalized == "path" or normalized.endswith(("path", "filepath"))


def _basename(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] if normalized else ""
