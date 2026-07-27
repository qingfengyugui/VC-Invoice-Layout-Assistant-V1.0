"""Behavior tests for host-provided observations and the local fallback."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from invoice_layout.models import Observation, SourceFile
from invoice_layout.observation_exchange import (
    load_host_observations,
    prepare_observation_request,
)
from invoice_layout.providers.local_ocr import LocalOCRProvider
from tests.factories import make_page_asset


@pytest.fixture
def page(tmp_path: Path):  # type: ignore[no-untyped-def]
    return make_page_asset(tmp_path)


def _write_manifest(path: Path, entries: list[dict[str, object]]) -> Path:
    manifest = path / "observations.json"
    manifest.write_text(json.dumps({"pages": entries}), "utf-8")
    return manifest


def _manifest_entry(page_id: str, preview: Path, observation: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "page_id": page_id,
        "preview_sha256": hashlib.sha256(preview.read_bytes()).hexdigest(),
        "observation": observation or {"text": "HOST-OBSERVATION", "confidence": 0.9},
    }


def test_request_contains_schema_hash_and_preview(page, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    request = json.loads(prepare_observation_request([page], tmp_path / "request.json").read_text("utf-8"))

    assert request["pages"][0]["page_id"] == page.id
    assert request["pages"][0]["preview_path"] == str(page.preview_png)
    assert request["pages"][0]["preview_sha256"]
    assert request["observation_schema"]["title"] == "Observation"


@pytest.mark.parametrize(
    "entries",
    [
        lambda page: [_manifest_entry("wrong", page.preview_png)],
        lambda page: [],
        lambda page: [_manifest_entry(page.id, page.preview_png), _manifest_entry("extra", page.preview_png)],
    ],
)
def test_manifest_rejects_unknown_or_missing_page(page, tmp_path: Path, entries) -> None:  # type: ignore[no-untyped-def]
    manifest = _write_manifest(tmp_path, entries(page))

    with pytest.raises(ValueError, match="page ids"):
        load_host_observations(manifest, [page])


def test_manifest_rejects_preview_hash_path_swap(page, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    entry = _manifest_entry(page.id, page.preview_png)
    entry["preview_sha256"] = "0" * 64
    manifest = _write_manifest(tmp_path, [entry])

    with pytest.raises(ValueError, match="preview hash"):
        load_host_observations(manifest, [page])


def test_manifest_rejects_preview_path_injection(page, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    entry = _manifest_entry(page.id, page.preview_png)
    entry["preview_path"] = str(tmp_path / "replacement.png")
    manifest = _write_manifest(tmp_path, [entry])

    with pytest.raises(ValueError, match="unexpected fields"):
        load_host_observations(manifest, [page])


def test_manifest_rejects_schema_invalid_observation(page, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    entry = _manifest_entry(page.id, page.preview_png, {"confidence": 2})
    manifest = _write_manifest(tmp_path, [entry])

    with pytest.raises(ValueError, match="invalid observation"):
        load_host_observations(manifest, [page])


@pytest.mark.parametrize(
    "observation",
    [
        {"confidence": "0.5"},
        {"confidence": 0.5, "unexpected": "must be rejected"},
    ],
)
def test_manifest_rejects_coercible_or_extra_observation_fields(
    page, tmp_path: Path, observation: dict[str, object]
) -> None:  # type: ignore[no-untyped-def]
    manifest = _write_manifest(tmp_path, [_manifest_entry(page.id, page.preview_png, observation)])

    with pytest.raises(ValueError, match="invalid observation"):
        load_host_observations(manifest, [page])


def test_manifest_loads_observation_for_exact_page_and_hash(page, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    manifest = _write_manifest(tmp_path, [_manifest_entry(page.id, page.preview_png)])

    observations = load_host_observations(manifest, [page])

    assert observations == {page.id: Observation(text="HOST-OBSERVATION", confidence=0.9)}


def test_local_fallback_never_claims_high_confidence(page) -> None:  # type: ignore[no-untyped-def]
    result = LocalOCRProvider().observe(page)

    assert result.confidence < 0.65


def test_local_fallback_uses_explicit_structured_metadata_for_exact_field(page) -> None:  # type: ignore[no-untyped-def]
    result = LocalOCRProvider({page.source_id: (("InvoiceNo", "XML-001"),)}).observe(page)

    assert result.invoice_number == "XML-001"
    assert result.confidence >= 0.65


def test_local_fallback_matches_unique_xml_companion_to_renderable_source(
    page, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    xml_source = SourceFile(
        id="xml-source",
        path=page.source_path.with_suffix(".xml"),
        sha256="a" * 64,
        media_type="application/xml",
        metadata=(("InvoiceNo", "XML-002"),),
    )
    renderable_source = SourceFile(
        id=page.source_id,
        path=page.source_path,
        sha256="b" * 64,
        media_type="application/pdf",
    )

    result = LocalOCRProvider.from_sources([xml_source, renderable_source]).observe(page)

    assert result.invoice_number == "XML-002"


def test_local_fallback_does_not_trust_non_xml_metadata(page) -> None:  # type: ignore[no-untyped-def]
    source = SourceFile(
        id=page.source_id,
        path=page.source_path,
        sha256="a" * 64,
        media_type="application/pdf",
        metadata=(("InvoiceNo", "UNTRUSTED-001"),),
    )

    result = LocalOCRProvider.from_sources([source]).observe(page)

    assert result.invoice_number is None
    assert result.confidence < 0.65


@pytest.mark.parametrize("ambiguous_sources", ["xml", "renderable"])
def test_local_fallback_does_not_guess_ambiguous_xml_companion(
    page, ambiguous_sources: str
) -> None:  # type: ignore[no-untyped-def]
    xml_sources = [
        SourceFile(
            id="xml-first",
            path=page.source_path.with_suffix(".xml"),
            sha256="c" * 64,
            media_type="application/xml",
            metadata=(("InvoiceNo", "XML-FIRST"),),
        )
    ]
    renderable_sources = [
        SourceFile(
            id=page.source_id,
            path=page.source_path,
            sha256="d" * 64,
            media_type="application/pdf",
        )
    ]
    if ambiguous_sources == "xml":
        xml_sources.append(
            SourceFile(
                id="xml-second",
                path=page.source_path.parent / "elsewhere" / page.source_path.with_suffix(".xml").name,
                sha256="e" * 64,
                media_type="application/xml",
                metadata=(("InvoiceNo", "XML-SECOND"),),
            )
        )
    else:
        renderable_sources.append(
            SourceFile(
                id="ofd-source",
                path=page.source_path.with_suffix(".ofd"),
                sha256="f" * 64,
                media_type="application/ofd",
            )
        )

    result = LocalOCRProvider.from_sources([*xml_sources, *renderable_sources]).observe(page)

    assert result.invoice_number is None
    assert result.confidence < 0.65
