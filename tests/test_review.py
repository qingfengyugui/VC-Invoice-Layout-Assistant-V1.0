from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from invoice_layout import review
from invoice_layout.models import WarningItem
from invoice_layout.review import build_outputs

PRIVATE_WINDOWS_ROOT = "C:" + chr(92) + "private"
PRIVATE_POSIX_ROOT = chr(47) + "private"


def _ticket_pdf(path: Path, pages: int = 2) -> Path:
    pdf = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    for index in range(pages):
        pdf.setFillColorRGB(0.1 * (index + 1), 0.2, 0.3)
        pdf.rect(72, 600, 180, 80, fill=1, stroke=0)
        pdf.drawString(72, 570, f"TICKET-{index + 1}-TEST")
        pdf.showPage()
    pdf.save()
    return path


def _warning(index: int = 1) -> WarningItem:
    return WarningItem(
        code=f"low_confidence_{index}",
        source_page_ids=(f"page-{index}",),
        output_page=index,
        message=f"识别置信度较低 {index}",
        action="保持原票面，未裁切",
        severity="warning",
    )


def _manifest() -> dict[str, object]:
    return {
        "provider": "host",
        "page_sources": {"page-1": f"{PRIVATE_WINDOWS_ROOT}\\出租车发票-TEST.pdf"},
        "sources": [
            {
                "sha256": "a" * 64,
                "path": f"{PRIVATE_WINDOWS_ROOT}\\出租车发票-TEST.pdf",
                "page_index": 0,
            }
        ],
        "placements": [{"page_asset_id": "page-1", "crop": [0, 0, 1, 1]}],
        "verification": {"passed": True},
    }


def _content_bytes(path: Path) -> list[bytes]:
    return [page.get_contents().get_data() for page in PdfReader(path).pages]


def test_review_ui_copy_is_correct_unicode_chinese() -> None:
    assert review._TITLE == "打印前核对页"
    assert review._BANNER == "不属于报销附件，请勿寄送"
    assert review._REPORT_REFERENCE == "完整详情请查看机器报告 report.json"
    assert review._UNKNOWN_SOURCE == "未知来源"
    assert review._UNLOCATED_PAGE == "未定位"
    assert review._SOURCE_LABEL == "来源"
    assert review._OUTPUT_PAGE_LABEL == "输出页"
    assert review._PROBLEM_LABEL == "问题"
    assert review._ACTION_LABEL == "处理"
    assert review._CHECK_LABEL == "建议核对"


def test_warning_page_is_last_and_sendable_excludes_it(tmp_path: Path) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    result = build_outputs(ticket, [_warning()], _manifest(), tmp_path / "out")

    printable = PdfReader(result.printable_pdf)
    sendable = PdfReader(result.sendable_pdf)
    assert len(printable.pages) == len(sendable.pages) + 1
    review_text = printable.pages[-1].extract_text()
    assert "打印前核对页" in review_text
    assert "不属于报销附件，请勿寄送" in review_text
    assert "打印前核对页" not in "".join(
        page.extract_text() for page in sendable.pages
    )
    assert _content_bytes(result.printable_pdf)[:-1] == _content_bytes(
        result.sendable_pdf
    )


def test_no_warning_still_creates_standalone_review_page(tmp_path: Path) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    result = build_outputs(ticket, [], _manifest(), tmp_path / "out")

    printable = PdfReader(result.printable_pdf)
    sendable = PdfReader(result.sendable_pdf)
    assert len(printable.pages) == len(sendable.pages) + 1 == 3
    assert review._TITLE in printable.pages[-1].extract_text()
    assert _content_bytes(result.printable_pdf)[:-1] == _content_bytes(
        result.sendable_pdf
    )
    assert result.sendable_pdf.read_bytes() == ticket.read_bytes()


def test_warning_row_has_stable_id_basename_page_problem_action_and_check(
    tmp_path: Path,
) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    first = build_outputs(ticket, [_warning()], _manifest(), tmp_path / "one")
    second = build_outputs(ticket, [_warning()], _manifest(), tmp_path / "two")
    first_text = PdfReader(first.printable_pdf).pages[-1].extract_text()
    second_text = PdfReader(second.printable_pdf).pages[-1].extract_text()

    assert "W-" in first_text
    assert "出租车发票-TEST.pdf" in first_text
    assert PRIVATE_WINDOWS_ROOT not in first_text
    assert "输出页: 1" in first_text
    assert "问题: 识别置信度较低 1" in first_text
    assert "处理: 保持原票面，未裁切" in first_text
    assert "建议核对:" in first_text
    assert first_text == second_text


def test_missing_source_display_mapping_never_falls_back_to_page_id_or_path(
    tmp_path: Path,
) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    private_page_id = f"{PRIVATE_WINDOWS_ROOT}\\opaque-page-id-TEST"
    warning = _warning().model_copy(
        update={"source_page_ids": (private_page_id,)}
    )
    manifest = _manifest()
    manifest.pop("page_sources")
    result = build_outputs(ticket, [warning], manifest, tmp_path / "out")
    review_text = PdfReader(result.printable_pdf).pages[-1].extract_text()

    assert "来源: 未知来源" in review_text
    assert "opaque-page-id-TEST" not in review_text
    assert PRIVATE_WINDOWS_ROOT not in review_text


def test_many_warnings_fit_exactly_one_a4_review_page(tmp_path: Path) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf", pages=1)
    warnings = [
        _warning(index).model_copy(
            update={"message": "需要人工核对" * 100, "action": "保持原样" * 100}
        )
        for index in range(1, 101)
    ]
    manifest = _manifest() | {
        "page_sources": {
            f"page-{index}": f"{PRIVATE_POSIX_ROOT}/source-{index}-TEST.pdf"
            for index in range(1, 101)
        }
    }
    result = build_outputs(ticket, warnings, manifest, tmp_path / "out")
    reader = PdfReader(result.printable_pdf)

    assert len(reader.pages) == 2
    assert tuple(float(value) for value in reader.pages[-1].mediabox[2:]) == pytest.approx(
        A4
    )
    assert "完整详情请查看机器报告" in reader.pages[-1].extract_text()


def test_report_recursively_redacts_secrets_ids_cards_and_paths(
    tmp_path: Path,
) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    private_home = "/" + "home" + "/user/private"
    manifest = _manifest() | {
        "Authorization": "Bearer secret-value",
        "nested": {
            "api_key": "sk-private",
            "password": "private",
            "cookie": "session=private",
            "tokenValue": "private",
            "traveler_id": "11010519491231002X",
            "bank_card": "6222021234567890123",
            "source_path": f"{private_home}/invoice.pdf",
        },
    }
    result = build_outputs(ticket, [_warning()], manifest, tmp_path / "out")
    report_text = result.report_json.read_text("utf-8")
    report = json.loads(report_text)

    assert "secret-value" not in report_text
    assert "sk-private" not in report_text
    assert "11010519491231002X" not in report_text
    assert "6222021234567890123" not in report_text
    assert private_home not in report_text
    assert PRIVATE_WINDOWS_ROOT not in report_text
    assert report["nested"]["source_path"] == "invoice.pdf"
    assert report["sources"][0]["path"] == "出租车发票-TEST.pdf"
    assert report["warnings"][0]["id"].startswith("W-")


def test_report_sanitizes_mapping_keys_without_truncating_business_slashes(
    tmp_path: Path,
) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    manifest = _manifest() | {
        "route": "北京/上海",
        f"{PRIVATE_POSIX_ROOT}/one/invoice.pdf": "first-key",
        f"{PRIVATE_POSIX_ROOT}/two/invoice.pdf": "second-key",
        "Authorization": "Bearer private",
        "token": "private",
        "11010519491231002X": "id-key",
        "6222021234567890123": "card-key",
        "relative_path": "private/invoice.pdf",
    }
    result = build_outputs(ticket, [_warning()], manifest, tmp_path / "out")
    report_text = result.report_json.read_text("utf-8")
    report = json.loads(report_text)

    assert report["route"] == "北京/上海"
    assert report["relative_path"] == "invoice.pdf"
    assert "Authorization" not in report_text
    assert '"token"' not in report_text
    assert "11010519491231002X" not in report_text
    assert "6222021234567890123" not in report_text
    assert f"{PRIVATE_POSIX_ROOT}/" not in report_text
    assert sorted(
        value
        for key, value in report.items()
        if key.startswith("invoice.pdf")
    ) == ["first-key", "second-key"]
    assert len(
        [key for key in report if key.startswith("[REDACTED KEY]")]
    ) == 2


@pytest.mark.parametrize("output_name", ["sendable.pdf", "printable.pdf", "report.json"])
def test_rejects_destination_alias_and_preserves_ticket(
    tmp_path: Path, output_name: str
) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    before = ticket.read_bytes()
    os.link(ticket, output_dir / output_name)

    with pytest.raises(ValueError, match="aliases ticket PDF"):
        build_outputs(ticket, [_warning()], _manifest(), output_dir)

    assert ticket.read_bytes() == before


def test_atomic_failure_cleans_temporaries_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    before = ticket.read_bytes()
    output = tmp_path / "out"

    def fail_write(*args: object, **kwargs: object) -> object:
        raise OSError("synthetic write failure")

    monkeypatch.setattr("invoice_layout.review._write_report", fail_write)
    with pytest.raises(OSError, match="synthetic write failure"):
        build_outputs(ticket, [_warning()], _manifest(), output)

    assert ticket.read_bytes() == before
    assert not list(output.glob(".*.tmp"))
    assert not (output / "printable.pdf").exists()
    assert not (output / "sendable.pdf").exists()
    assert not (output / "report.json").exists()


def test_temporary_creation_failure_cleans_already_created_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    output = tmp_path / "out"
    original = review._temporary_path
    calls = 0

    def fail_second(destination: Path) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic temporary creation failure")
        return original(destination)

    monkeypatch.setattr(review, "_temporary_path", fail_second)
    with pytest.raises(OSError, match="temporary creation failure"):
        build_outputs(ticket, [_warning()], _manifest(), output)

    assert not list(output.glob(".*.tmp"))
    assert not (output / "printable.pdf").exists()
    assert not (output / "sendable.pdf").exists()
    assert not (output / "report.json").exists()


def test_warning_record_failure_cleans_all_staged_temporaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    output = tmp_path / "out"

    def fail_records(*args: object, **kwargs: object) -> object:
        raise ValueError("synthetic warning failure")

    monkeypatch.setattr(review, "_warning_records", fail_records)
    with pytest.raises(ValueError, match="synthetic warning failure"):
        build_outputs(ticket, [_warning()], _manifest(), output)

    assert not list(output.glob(".*.tmp"))
    assert not (output / "printable.pdf").exists()
    assert not (output / "sendable.pdf").exists()
    assert not (output / "report.json").exists()


@pytest.mark.parametrize("fail_at", range(1, 7))
def test_replace_failure_rolls_back_every_output_to_previous_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int
) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    before = ticket.read_bytes()
    output = tmp_path / "out"
    output.mkdir()
    old = {
        "sendable.pdf": b"old-sendable",
        "printable.pdf": b"old-printable",
        "report.json": b"old-report",
    }
    for name, content in old.items():
        (output / name).write_bytes(content)
    real_replace = os.replace
    calls = 0

    def fail_one_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError(f"synthetic replace failure {fail_at}")
        real_replace(source, destination)

    monkeypatch.setattr(review.os, "replace", fail_one_replace)
    with pytest.raises(OSError, match="synthetic replace failure"):
        build_outputs(ticket, [_warning()], _manifest(), output)

    assert ticket.read_bytes() == before
    for name, content in old.items():
        assert (output / name).read_bytes() == content
    assert not list(output.glob(".*.tmp"))


def test_repeated_outputs_are_deterministic(tmp_path: Path) -> None:
    ticket = _ticket_pdf(tmp_path / "tickets.pdf")
    first = build_outputs(ticket, [_warning()], _manifest(), tmp_path / "one")
    second = build_outputs(ticket, [_warning()], _manifest(), tmp_path / "two")

    assert first.printable_pdf.read_bytes() == second.printable_pdf.read_bytes()
    assert first.sendable_pdf.read_bytes() == second.sendable_pdf.read_bytes()
    assert first.report_json.read_bytes() == second.report_json.read_bytes()
