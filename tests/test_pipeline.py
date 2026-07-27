from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from invoice_layout.config import Settings
from invoice_layout.models import (
    AssociationGroup,
    CropBox,
    DocumentType,
    Observation,
    PageAsset,
    PipelineResult,
    Placement,
    SourceFile,
    WarningItem,
)
from invoice_layout.pipeline import (
    PipelineError,
    batch_fingerprint,
    prepare_batch,
    run_pipeline,
)


def _source(path: Path, source_id: str) -> SourceFile:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return SourceFile(
        id=source_id,
        path=path,
        sha256=digest,
        media_type="application/pdf",
    )


def _page(tmp_path: Path, source: SourceFile, suffix: str) -> PageAsset:
    page_pdf = tmp_path / f"{suffix}.pdf"
    preview = tmp_path / f"{suffix}.png"
    page_pdf.write_bytes(f"PAGE-{suffix}-TEST".encode())
    preview.write_bytes(f"PREVIEW-{suffix}-TEST".encode())
    return PageAsset(
        id=f"{source.id}-0",
        source_id=source.id,
        source_path=source.path,
        page_index=0,
        page_pdf=page_pdf,
        preview_png=preview,
        width_pt=200,
        height_pt=100,
        pixel_width=800,
        pixel_height=400,
    )


def _warning(code: str, page_id: str = "source-test-0") -> WarningItem:
    return WarningItem(
        code=code,
        source_page_ids=(page_id,),
        output_page=None,
        message=f"{code} test warning",
        action="review test fixture",
        severity="warning",
    )


def _patch_successful_stages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    observations: dict[str, Observation],
    *,
    patch_provider: bool = True,
) -> tuple[list[SourceFile], list[PageAsset], dict[str, object]]:
    from invoice_layout import pipeline

    sources: list[SourceFile] = []
    pages: list[PageAsset] = []
    for index, page_id in enumerate(observations):
        source_path = tmp_path / f"source-{index}-TEST.pdf"
        source_path.write_bytes(f"SOURCE-{index}-TEST".encode())
        source = _source(source_path, page_id.rsplit("-", 1)[0])
        sources.append(source)
        pages.append(_page(tmp_path, source, f"normalized-{index}"))

    capture: dict[str, object] = {}
    monkeypatch.setattr(
        pipeline,
        "discover_inputs",
        lambda _inputs, _work: (sources, [_warning("isolated_bad_file")]),
    )
    monkeypatch.setattr(
        pipeline,
        "normalize_sources",
        lambda _sources, _settings: (pages, []),
    )

    class Provider:
        def observe(self, page: PageAsset) -> Observation:
            return observations[page.id]

    if patch_provider:
        monkeypatch.setattr(
            pipeline,
            "_observation_provider",
            lambda _provider, _manifest, _sources, _pages: Provider(),
        )

    def fake_pack(
        groups: list[AssociationGroup],
        page_map: dict[str, PageAsset],
        crops: dict[str, CropBox],
        observed: dict[str, Observation],
        _settings: Settings,
    ) -> list[Placement]:
        capture["ordered_ids"] = [
            page_id
            for group in groups
            for page_id in (*group.primary_page_ids, *group.support_page_ids)
        ]
        capture["page_ids"] = sorted(page_map)
        capture["crop_ids"] = sorted(crops)
        capture["observation_ids"] = sorted(observed)
        return [
            Placement(
                page_asset_id=page_id,
                crop=crops[page_id],
                x_mm=15,
                y_mm=15 + index * 55,
                width_mm=100,
                height_mm=50,
                output_page_index=0,
            )
            for index, page_id in enumerate(capture["ordered_ids"])
        ]

    monkeypatch.setattr(pipeline, "pack_a4", fake_pack)

    def fake_compose(
        placements: list[Placement],
        assets: list[PageAsset],
        output: Path,
    ) -> Path:
        capture["placements"] = placements
        capture["assets"] = assets
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"%PDF-TEST")
        return output

    monkeypatch.setattr(pipeline, "compose_ticket_pages", fake_compose)
    monkeypatch.setattr(
        pipeline,
        "verify_pdf",
        lambda _pdf, _placements, _assets, _settings: (True, []),
    )

    def fake_outputs(
        _ticket: Path,
        warnings: list[WarningItem],
        manifest: dict[str, object],
        output_dir: Path,
    ) -> PipelineResult:
        capture["warnings"] = warnings
        capture["manifest"] = manifest
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in ("printable.pdf", "sendable.pdf", "report.json"):
            (output_dir / name).write_bytes(b"TEST")
        return PipelineResult(
            printable_pdf=output_dir / "printable.pdf",
            sendable_pdf=output_dir / "sendable.pdf",
            report_json=output_dir / "report.json",
            warnings=tuple(warnings),
        )

    monkeypatch.setattr(pipeline, "build_outputs", fake_outputs)
    return sources, pages, capture


def test_mixed_batch_is_ordered_omits_physical_and_retains_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observations = {
        "taxi-0": Observation(
            document_type=DocumentType.TAXI_INVOICE, confidence=0.95
        ),
        "flight-0": Observation(
            document_type=DocumentType.FLIGHT_ITINERARY, confidence=0.95
        ),
        "physical-0": Observation(
            document_type=DocumentType.PHYSICAL_TICKET_PHOTO, confidence=0.95
        ),
        "unknown-0": Observation(document_type=DocumentType.UNKNOWN, confidence=0.2),
    }
    _, pages, capture = _patch_successful_stages(monkeypatch, tmp_path, observations)

    result = run_pipeline(
        [tmp_path / "batch-TEST"],
        tmp_path / "out",
        Settings(provider="local", work_dir=tmp_path / "work"),
    )

    assert capture["ordered_ids"] == ["flight-0", "taxi-0", "unknown-0"]
    assert capture["page_ids"] == ["flight-0", "taxi-0", "unknown-0"]
    assert sorted(item.page_asset_id for item in capture["placements"]) == [
        "flight-0",
        "taxi-0",
        "unknown-0",
    ]
    assert {warning.code for warning in result.warnings} >= {
        "isolated_bad_file",
        "physical_ticket_attach_original",
        "unknown_document",
        "low_observation_confidence",
    }
    manifest = capture["manifest"]
    assert isinstance(manifest, dict)
    assert manifest["provider"] == "local"
    assert len(manifest["pages"]) == len(pages)
    assert set(manifest["observations"]) == set(observations)
    assert all("label" not in placement for placement in manifest["placements"])


def test_prepare_writes_hash_bound_request_without_invoking_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline

    source_path = tmp_path / "input-TEST.pdf"
    source_path.write_bytes(b"SOURCE-TEST")
    source = _source(source_path, "source-test")
    page = _page(tmp_path, source, "prepared")
    monkeypatch.setattr(pipeline, "discover_inputs", lambda *_args: ([source], []))
    monkeypatch.setattr(pipeline, "normalize_sources", lambda *_args: ([page], []))
    request_path = tmp_path / "observation-request.json"

    result = prepare_batch(
        [source_path],
        request_path,
        Settings(provider="host", work_dir=tmp_path / "work"),
    )

    payload = json.loads(request_path.read_text("utf-8"))
    assert result.request_path == request_path
    assert result.warning_count == 0
    assert payload["pages"][0]["page_id"] == page.id
    assert payload["pages"][0]["preview_sha256"] == hashlib.sha256(
        page.preview_png.read_bytes()
    ).hexdigest()
    assert "observation_schema" in payload


def test_prepare_rejects_request_that_is_the_input_archive_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline

    archive = tmp_path / "batch-TEST.zip"
    original = b"ZIP-INPUT-TEST"
    archive.write_bytes(original)
    monkeypatch.setattr(
        pipeline,
        "discover_inputs",
        lambda *_args: pytest.fail("path plan must be validated before discovery"),
    )

    with pytest.raises(PipelineError, match="conflict"):
        prepare_batch(
            [archive],
            archive,
            Settings(provider="host", work_dir=tmp_path / "work"),
        )

    assert archive.read_bytes() == original
    assert not (tmp_path / "work").exists()


def test_prepare_rejects_request_that_is_an_unsupported_explicit_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline

    unsupported = tmp_path / "notes-TEST.bin"
    original = b"UNSUPPORTED-INPUT-TEST"
    unsupported.write_bytes(original)
    monkeypatch.setattr(
        pipeline,
        "discover_inputs",
        lambda *_args: pytest.fail("path plan must be validated before discovery"),
    )

    with pytest.raises(PipelineError, match="conflict"):
        prepare_batch(
            [unsupported],
            unsupported,
            Settings(provider="host", work_dir=tmp_path / "work"),
        )

    assert unsupported.read_bytes() == original
    assert not (tmp_path / "work").exists()


def test_prepare_rejects_request_inside_explicit_input_directory_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline

    input_dir = tmp_path / "batch-TEST"
    input_dir.mkdir()
    request = input_dir / "request.json"
    monkeypatch.setattr(
        pipeline,
        "discover_inputs",
        lambda *_args: pytest.fail("path plan must be validated before discovery"),
    )

    with pytest.raises(PipelineError, match="conflict"):
        prepare_batch(
            [input_dir],
            request,
            Settings(provider="host", work_dir=tmp_path / "work"),
        )

    assert not request.exists()
    assert not (tmp_path / "work").exists()


@pytest.mark.parametrize("output_name", ["printable.pdf", "sendable.pdf", "report.json"])
def test_process_rejects_specific_output_that_is_an_explicit_input_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_name: str,
) -> None:
    from invoice_layout import pipeline

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    source = output_dir / output_name
    original = f"ORIGINAL-{output_name}-TEST".encode()
    source.write_bytes(original)
    monkeypatch.setattr(
        pipeline,
        "discover_inputs",
        lambda *_args: pytest.fail("path plan must be validated before discovery"),
    )

    with pytest.raises(PipelineError, match="conflict"):
        run_pipeline(
            [source],
            output_dir,
            Settings(provider="local", work_dir=tmp_path / "work"),
        )

    assert source.read_bytes() == original
    assert not (tmp_path / "work").exists()


def test_process_rejects_output_directory_that_is_the_input_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline

    input_dir = tmp_path / "batch-TEST"
    input_dir.mkdir()
    printable = input_dir / "printable.pdf"
    original = b"ORIGINAL-PRINTABLE-TEST"
    printable.write_bytes(original)
    monkeypatch.setattr(
        pipeline,
        "discover_inputs",
        lambda *_args: pytest.fail("path plan must be validated before discovery"),
    )

    with pytest.raises(PipelineError, match="conflict"):
        run_pipeline(
            [input_dir],
            input_dir,
            Settings(provider="local", work_dir=tmp_path / "work"),
        )

    assert printable.read_bytes() == original
    assert not (tmp_path / "work").exists()


def test_process_rejects_manifest_that_is_a_planned_output_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline

    source = tmp_path / "source-TEST.pdf"
    source.write_bytes(b"SOURCE-TEST")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    manifest = output_dir / "sendable.pdf"
    original = b'{"pages":[]}'
    manifest.write_bytes(original)
    monkeypatch.setattr(
        pipeline,
        "discover_inputs",
        lambda *_args: pytest.fail("path plan must be validated before discovery"),
    )

    with pytest.raises(PipelineError, match="conflict"):
        run_pipeline(
            [source],
            output_dir,
            Settings(
                provider="host",
                host_manifest=manifest,
                work_dir=tmp_path / "work",
            ),
        )

    assert manifest.read_bytes() == original
    assert not (tmp_path / "work").exists()


def test_prepare_rejects_work_directory_inside_input_and_manifest_inside_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline

    input_dir = tmp_path / "batch-TEST"
    input_dir.mkdir()
    work_dir = input_dir / "work"
    manifest = work_dir / "observations.json"
    monkeypatch.setattr(
        pipeline,
        "discover_inputs",
        lambda *_args: pytest.fail("path plan must be validated before discovery"),
    )

    with pytest.raises(PipelineError, match="conflict"):
        prepare_batch(
            [input_dir],
            tmp_path / "request.json",
            Settings(
                provider="host",
                host_manifest=manifest,
                work_dir=work_dir,
            ),
        )

    assert not work_dir.exists()


def test_prepare_rejects_request_that_is_the_host_manifest_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline

    source = tmp_path / "source-TEST.pdf"
    source.write_bytes(b"SOURCE-TEST")
    request_and_manifest = tmp_path / "observations.json"
    original = b'{"pages":[]}'
    request_and_manifest.write_bytes(original)
    monkeypatch.setattr(
        pipeline,
        "discover_inputs",
        lambda *_args: pytest.fail("path plan must be validated before discovery"),
    )

    with pytest.raises(PipelineError, match="conflict"):
        prepare_batch(
            [source],
            request_and_manifest,
            Settings(
                provider="host",
                host_manifest=request_and_manifest,
                work_dir=tmp_path / "work",
            ),
        )

    assert request_and_manifest.read_bytes() == original
    assert not (tmp_path / "work").exists()


def test_process_rejects_hardlinked_source_and_output_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline

    input_dir = tmp_path / "batch-TEST"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()
    sendable = output_dir / "sendable.pdf"
    original = b"HARDLINK-SOURCE-TEST"
    sendable.write_bytes(original)
    linked_source = input_dir / "linked-source-TEST.pdf"
    try:
        os.link(sendable, linked_source)
    except OSError:
        pytest.skip("hard links are unavailable on this filesystem")
    monkeypatch.setattr(
        pipeline,
        "discover_inputs",
        lambda *_args: pytest.fail("path plan must be validated before discovery"),
    )

    with pytest.raises(PipelineError, match="conflict"):
        run_pipeline(
            [input_dir],
            output_dir,
            Settings(provider="local", work_dir=tmp_path / "work"),
        )

    assert sendable.read_bytes() == original
    assert linked_source.read_bytes() == original
    assert not (tmp_path / "work").exists()


def test_host_provider_without_manifest_fails_before_processing(tmp_path: Path) -> None:
    with pytest.raises(PipelineError, match="manifest"):
        run_pipeline(
            [tmp_path / "input-TEST.pdf"],
            tmp_path / "out",
            Settings(provider="host", work_dir=tmp_path / "work"),
        )


def test_host_prepare_process_rejects_stale_preview_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observations = {
        "flight-0": Observation(
            document_type=DocumentType.FLIGHT_ITINERARY, confidence=0.95
        )
    }
    _, pages, _ = _patch_successful_stages(
        monkeypatch, tmp_path, observations, patch_provider=False
    )
    request_path = tmp_path / "request.json"
    prepare_batch(
        [tmp_path / "batch-TEST"],
        request_path,
        Settings(provider="host", work_dir=tmp_path / "work"),
    )
    request = json.loads(request_path.read_text("utf-8"))
    manifest_path = tmp_path / "observations.json"
    manifest_path.write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "page_id": pages[0].id,
                        "preview_sha256": request["pages"][0]["preview_sha256"],
                        "observation": observations[pages[0].id].model_dump(
                            mode="json"
                        ),
                    }
                ]
            }
        ),
        "utf-8",
    )
    settings = Settings(
        provider="host",
        host_manifest=manifest_path,
        work_dir=tmp_path / "work",
    )

    result = run_pipeline([tmp_path / "batch-TEST"], tmp_path / "out", settings)
    assert result.sendable_pdf.name == "sendable.pdf"

    stale = json.loads(manifest_path.read_text("utf-8"))
    stale["pages"][0]["preview_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(stale), "utf-8")
    with pytest.raises(PipelineError, match="invalid or stale"):
        run_pipeline([tmp_path / "batch-TEST"], tmp_path / "stale-out", settings)


def test_auto_without_manifest_uses_local_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline

    observations = {
        "unknown-0": Observation(document_type=DocumentType.UNKNOWN, confidence=0.2)
    }
    _, _, capture = _patch_successful_stages(
        monkeypatch, tmp_path, observations, patch_provider=False
    )
    called: list[str] = []

    class Provider:
        def observe(self, page: PageAsset) -> Observation:
            called.append(page.id)
            return observations[page.id]

    monkeypatch.setattr(
        pipeline.LocalOCRProvider,
        "from_sources",
        classmethod(lambda _cls, _sources: Provider()),
    )

    run_pipeline(
        [tmp_path / "batch-TEST"],
        tmp_path / "out",
        Settings(provider="auto", work_dir=tmp_path / "work"),
    )

    assert called == ["unknown-0"]
    assert capture["manifest"]["provider"] == "local"


@pytest.mark.parametrize("media_type", ["image/jpeg", "image/png", "image/heic"])
def test_local_provider_fails_closed_for_unclassified_image_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    media_type: str,
) -> None:
    observations = {
        "pdf-0": Observation(document_type=DocumentType.TAXI_INVOICE, confidence=0.9),
        "image-0": Observation(document_type=DocumentType.UNKNOWN, confidence=0.2),
    }
    sources, _, _ = _patch_successful_stages(monkeypatch, tmp_path, observations)
    sources[1] = sources[1].model_copy(update={"media_type": media_type})
    output = tmp_path / "out"

    with pytest.raises(PipelineError, match="host observations"):
        run_pipeline(
            [tmp_path / "batch-TEST"],
            output,
            Settings(provider="local", work_dir=tmp_path / "work"),
        )

    assert not output.exists()


def test_host_provider_handles_electronic_image_and_excludes_physical_photo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observations = {
        "electronic-0": Observation(
            document_type=DocumentType.TAXI_INVOICE, confidence=0.95
        ),
        "physical-0": Observation(
            document_type=DocumentType.PHYSICAL_TICKET_PHOTO, confidence=0.95
        ),
    }
    sources, _, capture = _patch_successful_stages(
        monkeypatch, tmp_path, observations
    )
    sources[:] = [
        source.model_copy(update={"media_type": "image/jpeg"}) for source in sources
    ]

    result = run_pipeline(
        [tmp_path / "batch-TEST"],
        tmp_path / "out",
        Settings(
            provider="host",
            host_manifest=tmp_path / "observations.json",
            work_dir=tmp_path / "work",
        ),
    )

    assert capture["page_ids"] == ["electronic-0"]
    assert "physical_ticket_attach_original" in {
        warning.code for warning in result.warnings
    }


def test_no_valid_electronic_page_fails_without_completed_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observations = {
        "physical-0": Observation(
            document_type=DocumentType.PHYSICAL_TICKET_PHOTO, confidence=0.95
        )
    }
    _patch_successful_stages(monkeypatch, tmp_path, observations)
    output = tmp_path / "out"

    with pytest.raises(PipelineError, match="electronic"):
        run_pipeline(
            [tmp_path / "batch-TEST"],
            output,
            Settings(provider="local", work_dir=tmp_path / "work"),
        )

    assert not (output / "printable.pdf").exists()
    assert not (output / "sendable.pdf").exists()
    assert not (output / "report.json").exists()


def test_hard_verification_failure_does_not_publish_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from invoice_layout import pipeline

    observations = {
        "flight-0": Observation(
            document_type=DocumentType.FLIGHT_ITINERARY, confidence=0.95
        )
    }
    _patch_successful_stages(monkeypatch, tmp_path, observations)
    monkeypatch.setattr(
        pipeline,
        "verify_pdf",
        lambda *_args: (False, [_warning("content_mismatch", "flight-0")]),
    )
    monkeypatch.setattr(
        pipeline,
        "build_outputs",
        lambda *_args: pytest.fail("hard failure must not publish outputs"),
    )
    output = tmp_path / "out"

    with pytest.raises(PipelineError, match="verification"):
        run_pipeline(
            [tmp_path / "batch-TEST"],
            output,
            Settings(provider="local", work_dir=tmp_path / "work"),
        )

    assert not output.exists()


def test_fingerprint_changes_with_source_config_provider_and_schema(tmp_path: Path) -> None:
    first_path = tmp_path / "first-TEST.pdf"
    second_path = tmp_path / "second-TEST.pdf"
    first_path.write_bytes(b"FIRST-TEST")
    second_path.write_bytes(b"SECOND-TEST")
    first = _source(first_path, "first")
    second = _source(second_path, "second")
    local = Settings(provider="local", work_dir=tmp_path / "work")

    baseline = batch_fingerprint([first], local, "local")

    assert batch_fingerprint([second], local, "local") != baseline
    assert (
        batch_fingerprint(
            [first], local.model_copy(update={"page_margin_mm": 14.5}), "local"
        )
        != baseline
    )
    assert batch_fingerprint([first], local, "host") != baseline

    unused_manifest = tmp_path / "unused-observations.json"
    unused_manifest.write_text('{"pages":[]}', "utf-8")
    assert (
        batch_fingerprint(
            [first],
            local.model_copy(update={"host_manifest": unused_manifest}),
            "local",
        )
        == baseline
    )


def test_input_order_does_not_change_fingerprint(tmp_path: Path) -> None:
    paths = [tmp_path / "a-TEST.pdf", tmp_path / "b-TEST.pdf"]
    for index, path in enumerate(paths):
        path.write_bytes(f"SOURCE-{index}-TEST".encode())
    sources = [_source(path, path.stem) for path in paths]
    settings = Settings(provider="local", work_dir=tmp_path / "work")

    assert batch_fingerprint(sources, settings, "local") == batch_fingerprint(
        list(reversed(sources)), settings, "local"
    )
