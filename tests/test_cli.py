from __future__ import annotations

from pathlib import Path

from PIL import Image
from typer.testing import CliRunner

from invoice_layout.cli import app
from invoice_layout.models import PipelineResult, WarningItem
from invoice_layout.pipeline import PrepareResult

runner = CliRunner()


def _warning() -> WarningItem:
    return WarningItem(
        code="review_test",
        source_page_ids=("page-test",),
        output_page=None,
        message="review synthetic TEST material",
        action="review",
        severity="warning",
    )


def test_prepare_prints_request_and_warning_count(
    tmp_path: Path, monkeypatch
) -> None:
    from invoice_layout import cli

    request = tmp_path / "request.json"
    monkeypatch.setattr(
        cli,
        "prepare_batch",
        lambda *_args, **_kwargs: PrepareResult(request, 1),
    )

    result = runner.invoke(
        app,
        ["prepare", str(tmp_path / "input-TEST.pdf"), "--request", str(request)],
    )

    assert result.exit_code == 2
    assert str(request) in result.stdout
    assert "warnings: 1" in result.stdout
    assert "API_KEY" not in result.stdout


def test_process_clean_and_warning_exit_codes(
    tmp_path: Path, monkeypatch
) -> None:
    from invoice_layout import cli

    output = tmp_path / "out"

    def result(warnings: tuple[WarningItem, ...]) -> PipelineResult:
        return PipelineResult(
            printable_pdf=output / "printable.pdf",
            sendable_pdf=output / "sendable.pdf",
            report_json=output / "report.json",
            warnings=warnings,
        )

    monkeypatch.setattr(cli, "run_pipeline", lambda *_args, **_kwargs: result(()))
    clean = runner.invoke(
        app,
        [
            "process",
            str(tmp_path / "input-TEST.pdf"),
            "--output-dir",
            str(output),
        ],
    )
    assert clean.exit_code == 0
    assert "sendable.pdf" in clean.stdout
    assert "completed" in clean.stdout.casefold()

    monkeypatch.setattr(
        cli, "run_pipeline", lambda *_args, **_kwargs: result((_warning(),))
    )
    warned = runner.invoke(
        app,
        [
            "process",
            str(tmp_path / "input-TEST.pdf"),
            "--output-dir",
            str(output),
        ],
    )
    assert warned.exit_code == 2
    assert "warnings: 1" in warned.stdout


def test_process_failure_is_concise_and_has_no_traceback(
    tmp_path: Path, monkeypatch
) -> None:
    from invoice_layout import cli

    def fail(*_args, **_kwargs):
        raise ValueError("private extracted financial text")

    monkeypatch.setattr(cli, "run_pipeline", fail)
    result = runner.invoke(
        app,
        [
            "process",
            str(tmp_path / "input-TEST.pdf"),
            "--output-dir",
            str(tmp_path / "out"),
        ],
    )

    assert result.exit_code == 1
    assert "ValueError" in result.stderr
    assert "private extracted financial text" not in result.stderr
    assert "Traceback" not in result.stderr


def test_provider_host_without_observations_is_clear_cli_failure(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "process",
            str(tmp_path / "input-TEST.pdf"),
            "--output-dir",
            str(tmp_path / "out"),
            "--provider",
            "host",
        ],
    )

    assert result.exit_code == 1
    assert "manifest" in result.stderr.casefold()


def test_local_image_input_fails_closed_without_partial_outputs(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.png"
    Image.new("RGB", (320, 160), "white").save(source)
    output = tmp_path / "out"

    result = runner.invoke(
        app,
        [
            "process",
            str(source),
            "--output-dir",
            str(output),
            "--work-dir",
            str(tmp_path / "work"),
            "--provider",
            "local",
        ],
    )

    assert result.exit_code == 1
    assert "host observations" in result.stderr
    assert not (output / "printable.pdf").exists()
    assert not (output / "sendable.pdf").exists()
    assert not (output / "report.json").exists()


def test_doctor_reports_status_and_exit_codes(monkeypatch) -> None:
    from invoice_layout import cli

    monkeypatch.setattr(
        cli,
        "_doctor_checks",
        lambda: [("python", True, "compatible"), ("tesseract", False, "optional missing")],
    )
    optional_missing = runner.invoke(app, ["doctor"])
    assert optional_missing.exit_code == 0
    assert "python" in optional_missing.stdout
    assert "tesseract" in optional_missing.stdout

    monkeypatch.setattr(
        cli,
        "_doctor_checks",
        lambda: [("python", True, "compatible"), ("java", False, "not found")],
    )
    required_missing = runner.invoke(app, ["doctor"])
    assert required_missing.exit_code == 2

    monkeypatch.setattr(
        cli,
        "_doctor_checks",
        lambda: [("python", True, "compatible"), ("poppler", True, "available")],
    )
    clean = runner.invoke(app, ["doctor"])
    assert clean.exit_code == 0
    assert "API_KEY" not in clean.stdout


def test_doctor_reports_detected_rar_extractor(monkeypatch) -> None:
    from invoice_layout import cli

    monkeypatch.setattr(cli, "rar_extractor", lambda: Path("/tools/7z"))

    assert ("rar-extractor", True, "7z") in cli._doctor_checks()


def test_single_binary_exposes_mcp_server_command(monkeypatch) -> None:
    from invoice_layout import mcp_server

    called: list[bool] = []
    monkeypatch.setattr(mcp_server, "main", lambda: called.append(True))

    result = runner.invoke(app, ["mcp"])

    assert result.exit_code == 0
    assert called == [True]
