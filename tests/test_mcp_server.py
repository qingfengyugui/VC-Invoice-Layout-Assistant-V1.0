from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from invoice_layout.models import PipelineResult, WarningItem
from invoice_layout.pipeline import PrepareResult


def _warning() -> WarningItem:
    return WarningItem(
        code="adapter_test", source_page_ids=("page-test",), output_page=None,
        message="adapter test warning", action="review", severity="warning",
    )


def test_prepare_returns_only_private_safe_absolute_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import mcp_server

    input_file = tmp_path / "input-TEST.pdf"
    input_file.write_bytes(b"TEST")
    captured: dict[str, object] = {}
    request = tmp_path / "work" / "observation-request.json"
    request.parent.mkdir()
    request.write_text(
        json.dumps({
            "pages": [{"preview_path": str(tmp_path / "preview-TEST.png")}],
            "observation_schema": {"type": "object"},
        }),
        encoding="utf-8",
    )

    def fake_prepare(inputs: list[Path], request_path: Path, settings: object) -> PrepareResult:
        captured.update(inputs=inputs, request_path=request_path, settings=settings)
        return PrepareResult(request_path=request_path, warning_count=1)

    monkeypatch.setattr(mcp_server, "prepare_batch", fake_prepare)
    payload = mcp_server.prepare_invoice_batch([str(input_file)], str(tmp_path / "work"))

    assert set(payload) == {
        "observation_request", "previews", "preview_count", "observation_schema", "warning_count"
    }
    assert payload["observation_request"] == str(request.resolve())
    assert payload["previews"] == [str((tmp_path / "preview-TEST.png").resolve())]
    assert payload["preview_count"] == 1
    assert payload["observation_schema"] == {"type": "object"}
    assert payload["warning_count"] == 1
    assert captured["inputs"] == [input_file]
    assert captured["request_path"] == request
    assert captured["settings"].work_dir == tmp_path / "work" / "engine"


@pytest.mark.parametrize("inputs", ([], ["missing-TEST.pdf"]))
def test_prepare_rejects_invalid_inputs_before_pipeline(
    inputs: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import mcp_server

    monkeypatch.setattr(mcp_server, "prepare_batch", lambda *_args: pytest.fail("pipeline called"))
    with pytest.raises(ValueError):
        mcp_server.prepare_invoice_batch(inputs, str(tmp_path / "work"))


def test_layout_validates_provider_and_manifest_before_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import mcp_server

    source = tmp_path / "source-TEST.pdf"
    source.write_bytes(b"TEST")
    monkeypatch.setattr(mcp_server, "run_pipeline", lambda *_args: pytest.fail("pipeline called"))
    with pytest.raises(ValueError):
        mcp_server.layout_invoices([str(source)], str(tmp_path / "out"), provider="bad")
    with pytest.raises(ValueError):
        mcp_server.layout_invoices([str(source)], str(tmp_path / "out"), provider="host")
    with pytest.raises(ValueError):
        mcp_server.layout_invoices(
            [str(source)], str(tmp_path / "out"), observations_manifest=str(tmp_path / "missing.json")
        )


def test_layout_returns_only_absolute_artifact_paths_and_sibling_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import mcp_server

    source = tmp_path / "source-TEST.pdf"
    source.write_bytes(b"TEST")
    manifest = tmp_path / "observations-TEST.json"
    manifest.write_text("{}", encoding="utf-8")
    output = tmp_path / "out"
    captured: dict[str, object] = {}

    def fake_run(inputs: list[Path], output_dir: Path, settings: object) -> PipelineResult:
        captured.update(inputs=inputs, output_dir=output_dir, settings=settings)
        return PipelineResult(
            printable_pdf=output_dir / "printable.pdf",
            sendable_pdf=output_dir / "sendable.pdf",
            report_json=output_dir / "report.json",
            warnings=(_warning(),),
        )

    monkeypatch.setattr(mcp_server, "run_pipeline", fake_run)
    payload = mcp_server.layout_invoices(
        [str(source)], str(output), observations_manifest=str(manifest), provider="auto"
    )

    assert payload == {
        "printable_pdf": str((output / "printable.pdf").resolve()),
        "sendable_pdf": str((output / "sendable.pdf").resolve()),
        "report_json": str((output / "report.json").resolve()),
        "warning_count": 1,
    }
    assert captured["inputs"] == [source]
    assert captured["output_dir"] == output
    assert captured["settings"].host_manifest == manifest
    assert captured["settings"].work_dir == tmp_path / ".out-engine"


def test_mcp_main_uses_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    from invoice_layout import mcp_server

    called: dict[str, object] = {}
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kwargs: called.update(kwargs))
    mcp_server.main()
    assert called == {"transport": "stdio"}


def test_stdio_server_initializes_and_lists_public_tools() -> None:
    async def verify() -> set[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "invoice_layout.mcp_server"],
            env=environment,
        )
        async with (
            stdio_client(server) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            return {tool.name for tool in (await session.list_tools()).tools}

    assert asyncio.run(verify()) == {"prepare_invoice_batch", "layout_invoices"}
