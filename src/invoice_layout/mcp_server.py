"""Credential-free stdio MCP adapter for the deterministic invoice pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, cast

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .pipeline import prepare_batch, run_pipeline

mcp = FastMCP("invoice-layout")
_PROVIDERS = frozenset({"auto", "host", "local"})


def _validated_inputs(inputs: list[str]) -> list[Path]:
    if not inputs:
        raise ValueError("at least one input path is required")
    paths = [Path(value).expanduser() for value in inputs]
    for path in paths:
        if not path.exists() or not (path.is_file() or path.is_dir()):
            raise ValueError("each input path must exist and be a file or directory")
    return paths


def _validated_manifest(path: str | None, provider: str) -> Path | None:
    if provider not in _PROVIDERS:
        raise ValueError("provider must be auto, host, or local")
    if provider == "host" and path is None:
        raise ValueError("host provider requires an observations manifest")
    if path is None:
        return None
    manifest = Path(path).expanduser()
    if not manifest.exists() or not manifest.is_file():
        raise ValueError("observations manifest must exist and be a file")
    return manifest


@mcp.tool()
def prepare_invoice_batch(inputs: list[str], work_dir: str) -> dict[str, object]:
    """Read user-provided local files and write private previews and request JSON to work_dir.

    This tool prepares host observations without extracting or returning financial text.
    """
    paths = _validated_inputs(inputs)
    root = Path(work_dir).expanduser()
    request_path = root / "observation-request.json"
    result = prepare_batch(
        paths,
        request_path,
        Settings(provider="host", work_dir=root / "engine"),
    )
    try:
        request = json.loads(result.request_path.read_text(encoding="utf-8"))
        pages = request["pages"]
        schema = request["observation_schema"]
        if not isinstance(pages, list) or not isinstance(schema, dict):
            raise TypeError
        previews = [str(Path(page["preview_path"]).resolve()) for page in pages]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError("observation request could not be read safely") from error
    return {
        "observation_request": str(result.request_path.resolve()),
        "previews": previews,
        "preview_count": len(previews),
        "observation_schema": schema,
        "warning_count": result.warning_count,
    }


@mcp.tool()
def layout_invoices(
    inputs: list[str],
    output_dir: str,
    observations_manifest: str | None = None,
    provider: str = "auto",
) -> dict[str, object]:
    """Read user-provided local files and write private previews and PDFs to output_dir.

    The returned paths identify private printable, sendable, and report artifacts.
    """
    paths = _validated_inputs(inputs)
    manifest = _validated_manifest(observations_manifest, provider)
    output = Path(output_dir).expanduser()
    engine = output.parent / f".{output.name}-engine"
    result = run_pipeline(
        paths,
        output,
        Settings(
            provider=cast(Literal["auto", "host", "local"], provider),
            host_manifest=manifest,
            work_dir=engine,
        ),
    )
    return {
        "printable_pdf": str(result.printable_pdf.resolve()),
        "sendable_pdf": str(result.sendable_pdf.resolve()),
        "report_json": str(result.report_json.resolve()),
        "warning_count": len(result.warnings),
    }


def main() -> None:
    """Run the MCP server over standard input/output."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
