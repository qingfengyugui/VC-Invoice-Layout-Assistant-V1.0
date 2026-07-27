from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pypdf import PdfWriter
from reportlab.lib.pagesizes import A4

from invoice_layout.models import PipelineResult

PRIVATE_WINDOWS_ROOT = "C:" + chr(92) + "private"


def _synthetic_pdf(path: Path, *, size: tuple[float, float] = A4) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=size[0], height=size[1])
    with path.open("wb") as stream:
        writer.write(stream)


def _private_case(tmp_path: Path) -> tuple[dict[str, object], dict[str, object], Path]:
    source_hash = "a" * 64
    page_key = f"{source_hash}:0"
    sendable = tmp_path / "sendable.pdf"
    _synthetic_pdf(sendable)
    pipeline_report: dict[str, object] = {
        "sources": [{
            "source_id": "private-source-id",
            "sha256": source_hash,
            "path": f"{PRIVATE_WINDOWS_ROOT}\\SENTINEL-FILENAME-9988.pdf",
        }],
        "pages": [{
            "page_id": "private-page-id",
            "source_id": "private-source-id",
            "page_index": 0,
            "included_in_ticket_pages": True,
        }],
        "observations": {"private-page-id": {"text": "SENTINEL OCR 9988", "amount": "12345.67"}},
        "associations": [{"primary_page_ids": ["private-page-id"], "support_page_ids": []}],
        "placements": [{
            "page_asset_id": "private-page-id", "output_page_index": 0,
            "x_mm": 10.0, "y_mm": 20.0,
        }],
        "warnings": [{"code": "synthetic_warning", "source_page_ids": ["private-page-id"]}],
    }
    expected: dict[str, object] = {
        "page_order": [page_key],
        "association_groups": [[page_key]],
        "included_page_keys": [page_key],
        "warning_codes": ["synthetic_warning"],
        "manual_findings": {
            "critical_content_crop_page_keys": [],
            "visible_normal_page_annotation_page_indexes": [],
        },
    }
    return pipeline_report, expected, sendable


def test_gray_report_uses_hashes_not_private_paths_or_financial_text(tmp_path: Path) -> None:
    from scripts.gray_test import evaluate_gray_case

    pipeline_report, expected, sendable = _private_case(tmp_path)
    report = evaluate_gray_case(pipeline_report, expected, sendable)
    serialized = json.dumps(report, ensure_ascii=False)

    for forbidden in (
        PRIVATE_WINDOWS_ROOT,
        "SENTINEL-FILENAME-9988.pdf",
        "SENTINEL OCR 9988",
        "12345.67",
    ):
        assert forbidden not in serialized
    assert report["critical_content_crop_count"] == 0
    assert report["normal_page_annotation_count"] == 0
    assert report["release_gate_passed"] is True


def test_evaluator_fails_closed_for_unknown_or_non_hash_expected_identifiers(tmp_path: Path) -> None:
    from scripts.gray_test import evaluate_gray_case

    pipeline_report, expected, sendable = _private_case(tmp_path)
    invalid = {**expected, "unexpected": True}
    with pytest.raises(ValueError, match="expected"):
        evaluate_gray_case(pipeline_report, invalid, sendable)

    invalid_identifier = {**expected, "page_order": ["private-page-id"]}
    with pytest.raises(ValueError, match="SHA-256"):
        evaluate_gray_case(pipeline_report, invalid_identifier, sendable)


def test_evaluator_counts_omissions_duplicates_review_pages_and_manual_failures(tmp_path: Path) -> None:
    from scripts.gray_test import evaluate_gray_case

    pipeline_report, expected, sendable = _private_case(tmp_path)
    source_hash = "a" * 64
    second_key = f"{source_hash}:1"
    pipeline_report["placements"] = [
        *pipeline_report["placements"],
        {"page_asset_id": "private-page-id", "output_page_index": 0, "x_mm": 11.0, "y_mm": 20.0},
    ]
    expected["included_page_keys"] = [f"{source_hash}:0", second_key]
    expected["manual_findings"] = {
        "critical_content_crop_page_keys": [f"{source_hash}:0"],
        "visible_normal_page_annotation_page_indexes": [0],
    }

    report = evaluate_gray_case(pipeline_report, expected, sendable)

    assert report["omission_count"] == 1
    assert report["unexpected_duplicate_count"] == 1
    assert report["sendable_review_page_count"] == 0
    assert report["critical_content_crop_count"] == 1
    assert report["visible_normal_page_annotation_count"] == 1
    assert report["release_gate_passed"] is False


def test_evaluator_detects_non_a4_sendable_pages(tmp_path: Path) -> None:
    from scripts.gray_test import evaluate_gray_case

    pipeline_report, expected, _ = _private_case(tmp_path)
    non_a4 = tmp_path / "non-a4.pdf"
    _synthetic_pdf(non_a4, size=(500, 500))

    report = evaluate_gray_case(pipeline_report, expected, non_a4)

    assert report["a4_verification_pass_rate"] == 0.0
    assert report["release_gate_passed"] is False


def test_evaluator_counts_pdf_annotations_without_exposing_contents(tmp_path: Path) -> None:
    from scripts.gray_test import evaluate_gray_case

    pipeline_report, expected, _ = _private_case(tmp_path)
    annotated = tmp_path / "annotated.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=A4[0], height=A4[1])
    writer.add_annotation(0, {
        "/Type": "/Annot", "/Subtype": "/Text", "/Rect": [0, 0, 10, 10],
        "/Contents": "SYNTHETIC-ANNOTATION-SECRET",
    })
    with annotated.open("wb") as stream:
        writer.write(stream)

    report = evaluate_gray_case(pipeline_report, expected, annotated)

    assert report["normal_page_annotation_count"] == 1
    assert "SYNTHETIC-ANNOTATION-SECRET" not in json.dumps(report)
    assert report["release_gate_passed"] is False


def test_cli_writes_atomic_aggregate_report_and_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import gray_test

    pipeline_report, expected, sendable = _private_case(tmp_path)
    private_input = tmp_path / "private-input-SENTINEL.pdf"
    private_input.write_bytes(b"SYNTHETIC-NOT-FINANCIAL")
    expected_path = tmp_path / "expected.json"
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    output = tmp_path / "gray-output"

    def fake_run_pipeline(*_args: object, **_kwargs: object) -> PipelineResult:
        output.mkdir(exist_ok=True)
        report_path = output / "report.json"
        report_path.write_text(json.dumps(pipeline_report), encoding="utf-8")
        printable = output / "printable.pdf"
        _synthetic_pdf(printable)
        destination = output / "sendable.pdf"
        destination.write_bytes(sendable.read_bytes())
        return PipelineResult(printable_pdf=printable, sendable_pdf=destination, report_json=report_path)

    monkeypatch.setattr(gray_test, "run_pipeline", fake_run_pipeline)
    assert gray_test.main([
        str(private_input), "--output-dir", str(output), "--expected", str(expected_path), "--provider", "local",
    ]) == 0
    gray_report = output / "gray-report.json"
    assert gray_report.is_file()
    assert not list(output.glob(".gray-report.*.tmp"))
    serialized = gray_report.read_text(encoding="utf-8")
    assert "private-input-SENTINEL.pdf" not in serialized
    assert "SENTINEL OCR 9988" not in serialized

    expected["manual_findings"] = {
        "critical_content_crop_page_keys": ["a" * 64 + ":0"],
        "visible_normal_page_annotation_page_indexes": [],
    }
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    assert gray_test.main([
        str(private_input), "--output-dir", str(output), "--expected", str(expected_path), "--provider", "local",
    ]) == 2


def test_cli_rejects_expected_path_that_aliases_private_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import gray_test

    private_input = tmp_path / "private-input-SENTINEL.json"
    private_input.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gray_test, "run_pipeline", lambda *_args: pytest.fail("pipeline called"))

    assert gray_test.main([
        str(private_input), "--output-dir", str(tmp_path / "gray-output"), "--expected", str(private_input),
    ]) == 1


def test_gray_work_dir_is_sibling_and_real_pipeline_path_plan_reaches_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline
    from invoice_layout.config import Settings
    from scripts import gray_test

    private_input = tmp_path / "private-input-SENTINEL.pdf"
    private_input.write_bytes(b"SYNTHETIC-NOT-FINANCIAL")
    output = tmp_path / "gray-output"
    work_dir = gray_test._gray_work_dir(output)

    assert work_dir == tmp_path / ".gray-output.gray-test-work"
    assert work_dir.parent == output.parent
    assert work_dir != output

    def reached_after_path_plan(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic discovery reached")

    monkeypatch.setattr(pipeline, "discover_inputs", reached_after_path_plan)
    with pytest.raises(RuntimeError, match="synthetic discovery reached"):
        pipeline.run_pipeline(
            [private_input], output, Settings(provider="local", work_dir=work_dir)
        )


@pytest.mark.parametrize("extra", [
    ["--provider", "SENTINEL-PRIVATE-FILENAME.pdf"],
    ["--unknown-option", "SENTINEL-PRIVATE-FILENAME.pdf"],
])
def test_cli_parse_errors_do_not_echo_private_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    from scripts import gray_test

    private_input = tmp_path / "input.pdf"
    expected = tmp_path / "expected.json"
    private_input.write_bytes(b"SYNTHETIC")
    expected.write_text("{}", encoding="utf-8")

    code = gray_test.main([
        str(private_input), "--output-dir", str(tmp_path / "output"), "--expected", str(expected), *extra,
    ])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert "SENTINEL-PRIVATE-FILENAME.pdf" not in captured.err
    assert captured.err == "error: private gray-test processing failed\n"


def test_cli_rejects_hardlink_aliases_and_private_destination_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import gray_test

    private_input = tmp_path / "private-input.pdf"
    private_input.write_bytes(b"SYNTHETIC")
    expected = tmp_path / "expected.json"
    expected.write_text("{}", encoding="utf-8")
    hardlink = tmp_path / "expected-hardlink.json"
    os.link(private_input, hardlink)
    monkeypatch.setattr(gray_test, "run_pipeline", lambda *_args: pytest.fail("pipeline called"))

    assert gray_test.main([
        str(private_input), "--output-dir", str(tmp_path / "output"), "--expected", str(hardlink),
    ]) == 1

    output = tmp_path / "existing-output"
    output.mkdir()
    dangerous_expected = output / "expected.json"
    dangerous_expected.write_text("{}", encoding="utf-8")
    assert gray_test.main([
        str(private_input), "--output-dir", str(output), "--expected", str(dangerous_expected),
    ]) == 1


@pytest.mark.parametrize("location", ["input", "output", "work"])
def test_cli_rejects_observation_conflicts_without_calling_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    from scripts import gray_test

    private_input = tmp_path / "private-input.pdf"
    private_input.write_bytes(b"SYNTHETIC")
    expected = tmp_path / "expected.json"
    expected.write_text("{}", encoding="utf-8")
    output = tmp_path / "output"
    work_dir = tmp_path / ".output.gray-test-work"
    if location == "input":
        observation = tmp_path / "input-hardlink.json"
        os.link(private_input, observation)
    elif location == "output":
        output.mkdir()
        observation = output / "observations.json"
        observation.write_text("{}", encoding="utf-8")
    else:
        work_dir.mkdir()
        observation = work_dir / "observations.json"
        observation.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gray_test, "run_pipeline", lambda *_args: pytest.fail("pipeline called"))

    assert gray_test.main([
        str(private_input), "--output-dir", str(output), "--expected", str(expected),
        "--observations", str(observation), "--provider", "host",
    ]) == 1
