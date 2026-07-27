"""Credential-free command line interface for invoice layout."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Annotated, Literal, NoReturn, cast

import typer
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # type: ignore[import-untyped]

from .config import Settings
from .pipeline import PipelineError, prepare_batch, run_pipeline
from .runtime import java_executable, rar_extractor

app = typer.Typer(
    name="invoice-layout",
    help="Arrange original electronic ticket content onto printable A4 pages.",
    no_args_is_help=True,
    rich_markup_mode=None,
)


@app.command()
def prepare(
    inputs: Annotated[list[Path], typer.Argument(help="Files, archives, or directories")],
    request: Annotated[Path, typer.Option("--request", help="Observation request JSON")],
    work_dir: Annotated[
        Path, typer.Option("--work-dir", help="Private local work directory")
    ] = Path(".invoice-layout-work"),
) -> None:
    """Prepare immutable previews and a strict host observation schema."""
    try:
        result = prepare_batch(
            inputs,
            request,
            Settings(provider="host", work_dir=work_dir),
        )
    except Exception as error:  # noqa: BLE001 - CLI is the privacy boundary.
        _fail(error)
    typer.echo(f"request: {result.request_path}")
    typer.echo(f"warnings: {result.warning_count}")
    if result.warning_count:
        raise typer.Exit(2)


@app.command()
def process(
    inputs: Annotated[list[Path], typer.Argument(help="Files, archives, or directories")],
    output_dir: Annotated[Path, typer.Option("--output-dir", help="Output directory")],
    observations: Annotated[
        Path | None,
        typer.Option("--observations", help="Hash-bound host observation manifest"),
    ] = None,
    provider: Annotated[
        str, typer.Option("--provider", help="auto, host, or local")
    ] = "auto",
    work_dir: Annotated[
        Path, typer.Option("--work-dir", help="Private local work directory")
    ] = Path(".invoice-layout-work"),
) -> None:
    """Process inputs into printable, sendable, and private report outputs."""
    if provider not in {"auto", "host", "local"}:
        typer.echo("error: provider must be auto, host, or local", err=True)
        raise typer.Exit(1)
    if provider == "host" and observations is None:
        typer.echo("error: host provider requires an observations manifest", err=True)
        raise typer.Exit(1)
    try:
        settings = Settings(
            provider=cast(Literal["auto", "host", "local"], provider),
            host_manifest=observations,
            work_dir=work_dir,
        )
        result = run_pipeline(inputs, output_dir, settings)
    except Exception as error:  # noqa: BLE001 - CLI is the privacy boundary.
        _fail(error)
    typer.echo("completed")
    typer.echo(f"printable: {result.printable_pdf}")
    typer.echo(f"sendable: {result.sendable_pdf}")
    typer.echo(f"report: {result.report_json}")
    typer.echo(f"warnings: {len(result.warnings)}")
    if result.warnings:
        raise typer.Exit(2)


@app.command()
def doctor() -> None:
    """Report local runtime readiness without reading or printing credentials."""
    checks = _doctor_checks()
    for name, available, detail in checks:
        typer.echo(f"{'OK' if available else 'MISSING'} {name}: {detail}")
    optional_checks = {"tesseract"}
    if not all(available for name, available, _ in checks if name not in optional_checks):
        raise typer.Exit(2)


@app.command("mcp")
def serve_mcp() -> None:
    """Run the local MCP server from the same standalone executable."""
    from .mcp_server import main

    main()


def _doctor_checks() -> list[tuple[str, bool, str]]:
    python_ok = (3, 11) <= sys.version_info[:2] < (3, 14)
    pdfium = importlib.util.find_spec("pypdfium2") is not None
    java = java_executable()
    renderer = _ofd_renderer()
    tesseract = shutil.which("tesseract")
    extractor = rar_extractor()

    temp_ok = False
    try:
        with tempfile.TemporaryDirectory(prefix="invoice-layout-doctor-") as directory:
            probe = Path(directory) / "write-test"
            probe.write_bytes(b"TEST")
            temp_ok = probe.read_bytes() == b"TEST"
    except OSError:
        temp_ok = False

    font_ok = True
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    except Exception:  # noqa: BLE001 - ReportLab backends vary by platform.
        font_ok = False

    return [
        ("python", python_ok, f"{sys.version_info.major}.{sys.version_info.minor}"),
        ("pdfium", pdfium, "bundled" if pdfium else "not found"),
        ("java", bool(java), "available" if java else "not found"),
        (
            "ofd-renderer",
            renderer.is_file(),
            "available" if renderer.is_file() else "not found",
        ),
        (
            "tesseract",
            bool(tesseract),
            "available" if tesseract else "optional component not found",
        ),
        (
            "rar-extractor",
            extractor is not None,
            extractor.name if extractor is not None else "not found",
        ),
        ("temporary-directory", temp_ok, "writable" if temp_ok else "not writable"),
        ("cjk-font", font_ok, "available" if font_ok else "not found"),
    ]

def _ofd_renderer() -> Path:
    configured = os.getenv("INVOICE_LAYOUT_OFD_RENDERER")
    if configured:
        return Path(configured)
    packaged = Path(__file__).resolve().parent / "bin" / "ofd-renderer.jar"
    if packaged.is_file():
        return packaged
    return (
        Path(__file__).resolve().parents[2]
        / "tools"
        / "ofd-renderer"
        / "target"
        / "ofd-renderer.jar"
    )


def _fail(error: Exception) -> NoReturn:
    if isinstance(error, PipelineError):
        typer.echo(f"error: PipelineError: {error}", err=True)
    else:
        typer.echo(f"error: {type(error).__name__}: processing failed", err=True)
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
