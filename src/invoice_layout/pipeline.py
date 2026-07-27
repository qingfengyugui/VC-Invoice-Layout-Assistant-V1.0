"""Fail-closed orchestration for deterministic invoice page layout."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .associate import associate
from .compose import compose_ticket_pages
from .config import Settings
from .crop import choose_crop
from .ingest import discover_inputs
from .layout import pack_a4
from .models import (
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
from .normalize import normalize_sources
from .observation_exchange import HostManifestError, prepare_observation_request
from .providers.base import ObservationProvider
from .providers.host_manifest import HostManifestProvider
from .providers.local_ocr import LocalOCRProvider
from .review import build_outputs
from .verify import verify_pdf

PIPELINE_SCHEMA_VERSION = 1
_OUTPUT_NAMES = ("printable.pdf", "sendable.pdf", "report.json")


class PipelineError(RuntimeError):
    """Raised when a batch cannot safely produce a complete output set."""


@dataclass(frozen=True)
class PrepareResult:
    """Result of the model-free host observation preparation step."""

    request_path: Path
    warning_count: int


def batch_fingerprint(
    sources: Sequence[SourceFile],
    settings: Settings,
    provider: Literal["host", "local"],
) -> str:
    """Bind one work directory to source bytes, output-affecting config and provider."""
    manifest_hash: str | None = None
    if (
        provider == "host"
        and settings.host_manifest is not None
        and settings.host_manifest.is_file()
    ):
        manifest_hash = _sha256(settings.host_manifest)
    payload = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "provider": provider,
        "sources": sorted(source.sha256 for source in sources),
        "settings": {
            "page_margin_mm": settings.page_margin_mm,
            "item_gap_mm": settings.item_gap_mm,
            "render_dpi": settings.render_dpi,
        },
        "host_manifest_sha256": manifest_hash,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def prepare_batch(
    inputs: Sequence[Path],
    request_path: Path,
    settings: Settings,
) -> PrepareResult:
    """Normalize inputs and write an immutable host-observation request.

    This function never selects or invokes an observation provider.
    """
    _validate_path_plan(
        inputs,
        protected_files=(request_path,),
        work_dir=settings.work_dir,
        host_manifest=settings.host_manifest,
    )
    sources, pages, warnings, _ = _prepare_sources(
        inputs,
        settings,
        fingerprint_provider="host",
        protected_files=(request_path,),
    )
    if not sources or not pages:
        raise PipelineError("no valid electronic page is available for observation")
    _reject_source_alias(request_path, sources, "request")
    prepare_observation_request(pages, request_path)
    return PrepareResult(
        request_path=Path(request_path),
        warning_count=len(_ordered_warnings(warnings)),
    )


def run_pipeline(
    inputs: Sequence[Path],
    output_dir: Path,
    settings: Settings,
) -> PipelineResult:
    """Run the complete invoice layout pipeline without generating ticket content."""
    provider_name = settings.resolved_provider()
    if provider_name == "host" and settings.host_manifest is None:
        raise PipelineError("host provider requires an observations manifest")
    output_files = tuple(output_dir / name for name in _OUTPUT_NAMES)
    _validate_path_plan(
        inputs,
        protected_files=output_files,
        work_dir=settings.work_dir,
        output_dir=output_dir,
        host_manifest=settings.host_manifest,
    )

    sources, pages, warnings, batch_settings = _prepare_sources(
        inputs,
        settings,
        fingerprint_provider=provider_name,
        protected_files=output_files,
    )
    if provider_name == "local" and any(
        source.media_type.startswith("image/") for source in sources
    ):
        raise PipelineError(
            "image inputs require hash-bound host observations; run prepare, "
            "inspect every preview, then process with --provider host"
        )
    if not pages:
        raise PipelineError("no valid electronic page remains after normalization")
    _validate_destinations(inputs, output_dir, settings.work_dir, sources)

    provider = _observation_provider(
        provider_name, settings.host_manifest, sources, pages
    )
    observations = {
        page.id: provider.observe(page)
        for page in sorted(pages, key=lambda item: item.id)
    }

    eligible_pages: dict[str, PageAsset] = {}
    eligible_observations: dict[str, Observation] = {}
    for page in sorted(pages, key=lambda item: item.id):
        observation = observations[page.id]
        if observation.document_type == DocumentType.PHYSICAL_TICKET_PHOTO:
            warnings.append(
                _warning(
                    "physical_ticket_attach_original",
                    page.id,
                    "A physical ticket photo was excluded from electronic ticket pages.",
                    "Attach the original physical ticket separately.",
                )
            )
            continue
        eligible_pages[page.id] = page
        eligible_observations[page.id] = observation
        if observation.document_type == DocumentType.UNKNOWN:
            warnings.append(
                _warning(
                    "unknown_document",
                    page.id,
                    "An electronic page could not be classified.",
                    "Review the retained full-page ticket content.",
                )
            )
        if observation.confidence < 0.65:
            warnings.append(
                _warning(
                    "low_observation_confidence",
                    page.id,
                    "An electronic page has a low-confidence observation.",
                    "Review the retained full-page ticket content.",
                )
            )

    if not eligible_pages:
        raise PipelineError("no valid electronic page remains after physical-ticket exclusion")

    groups, association_warnings = associate(eligible_observations)
    warnings.extend(association_warnings)
    crops: dict[str, CropBox] = {}
    for page_id, page in sorted(eligible_pages.items()):
        crop, crop_warnings = choose_crop(page, eligible_observations[page_id])
        crops[page_id] = crop
        warnings.extend(crop_warnings)

    placements = pack_a4(
        groups,
        eligible_pages,
        crops,
        eligible_observations,
        batch_settings,
    )
    expected_ids = set(eligible_pages)
    placed_ids = [placement.page_asset_id for placement in placements]
    if set(placed_ids) != expected_ids or len(placed_ids) != len(expected_ids):
        raise PipelineError("eligible pages were not placed exactly once")

    ticket_pdf = batch_settings.work_dir / "composed" / "ticket-pages.pdf"
    compose_ticket_pages(
        placements,
        [eligible_pages[page_id] for page_id in sorted(eligible_pages)],
        ticket_pdf,
    )
    verified, verification_warnings = verify_pdf(
        ticket_pdf,
        placements,
        eligible_pages,
        batch_settings,
    )
    warnings.extend(verification_warnings)
    if not verified:
        ticket_pdf.unlink(missing_ok=True)
        raise PipelineError("hard print/content verification failed")

    ordered_warnings = _ordered_warnings(warnings)
    manifest = _machine_manifest(
        sources=sources,
        pages={page.id: page for page in pages},
        observations=observations,
        included_page_ids=set(eligible_pages),
        groups=groups,
        crops=crops,
        placements=placements,
        provider=provider_name,
        fingerprint=batch_settings.work_dir.name,
        verification_warnings=verification_warnings,
    )
    return build_outputs(ticket_pdf, ordered_warnings, manifest, output_dir)


def _prepare_sources(
    inputs: Sequence[Path],
    settings: Settings,
    *,
    fingerprint_provider: Literal["host", "local"],
    protected_files: Sequence[Path] = (),
) -> tuple[list[SourceFile], list[PageAsset], list[WarningItem], Settings]:
    ordered_inputs = sorted(
        (Path(item) for item in inputs),
        key=lambda item: str(item.resolve(strict=False)).casefold(),
    )
    if not ordered_inputs:
        raise PipelineError("at least one input path is required")
    _reject_directory_as_file(settings.work_dir, "work directory")
    discovery_dir = settings.work_dir / "ingest"
    sources, warnings = discover_inputs(ordered_inputs, discovery_dir)
    sources = sorted(sources, key=lambda item: (item.sha256, item.id))
    _validate_discovered_sources(
        sources,
        protected_files=protected_files,
    )
    if not sources:
        return [], [], warnings, settings
    fingerprint = batch_fingerprint(sources, settings, fingerprint_provider)
    batch_dir = settings.work_dir / f"batch-{fingerprint}"
    batch_settings = settings.model_copy(update={"work_dir": batch_dir})
    pages, normalization_warnings = normalize_sources(sources, batch_settings)
    warnings.extend(normalization_warnings)
    pages = sorted(pages, key=lambda item: item.id)
    return sources, pages, warnings, batch_settings


def _observation_provider(
    provider: Literal["host", "local"],
    manifest_path: Path | None,
    sources: Sequence[SourceFile],
    pages: Sequence[PageAsset],
) -> ObservationProvider:
    if provider == "local":
        return LocalOCRProvider.from_sources(sources)
    if manifest_path is None:
        raise PipelineError("host provider requires an observations manifest")
    try:
        return HostManifestProvider.from_manifest(manifest_path, pages)
    except HostManifestError as error:
        raise PipelineError("host observations manifest is invalid or stale") from error


def _machine_manifest(
    *,
    sources: Sequence[SourceFile],
    pages: Mapping[str, PageAsset],
    observations: Mapping[str, Observation],
    included_page_ids: set[str],
    groups: Sequence[AssociationGroup],
    crops: Mapping[str, CropBox],
    placements: Sequence[Placement],
    provider: str,
    fingerprint: str,
    verification_warnings: Sequence[WarningItem],
) -> dict[str, object]:
    page_records = []
    page_sources: dict[str, str] = {}
    for page_id, page in sorted(pages.items()):
        page_sources[page_id] = str(page.source_path)
        page_records.append(
            {
                "page_id": page_id,
                "source_id": page.source_id,
                "page_index": page.page_index,
                "page_pdf_sha256": _sha256(page.page_pdf),
                "preview_sha256": _sha256(page.preview_png),
                "included_in_ticket_pages": page_id in included_page_ids,
            }
        )
    return {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "provider": provider,
        "sources": [
            {
                "source_id": source.id,
                "sha256": source.sha256,
                "path": str(source.path),
                "archive_member": source.archive_member,
            }
            for source in sources
        ],
        "pages": page_records,
        "page_sources": page_sources,
        "observations": {
            page_id: observation.model_dump(mode="json")
            for page_id, observation in sorted(observations.items())
        },
        "associations": [
            item.model_dump(mode="json")
            for item in groups
        ],
        "crops": {
            page_id: crop.model_dump(mode="json")
            for page_id, crop in sorted(crops.items())
        },
        "placements": [
            item.model_dump(mode="json")
            for item in placements
        ],
        "verification": {
            "passed": True,
            "warnings": [
                warning.model_dump(mode="json")
                for warning in _ordered_warnings(verification_warnings)
            ],
        },
    }


def _validate_destinations(
    inputs: Sequence[Path],
    output_dir: Path,
    work_dir: Path,
    sources: Sequence[SourceFile],
) -> None:
    _reject_directory_as_file(output_dir, "output directory")
    _reject_directory_as_file(work_dir, "work directory")
    for source in sources:
        _reject_source_alias(output_dir, (source,), "output directory")
        _reject_source_alias(work_dir, (source,), "work directory")
    for input_path in inputs:
        if input_path.is_file():
            for destination in (output_dir, work_dir):
                if _paths_alias(input_path, destination):
                    raise PipelineError("a destination aliases an input file")


def _reject_directory_as_file(path: Path, label: str) -> None:
    if path.exists() and not path.is_dir():
        raise PipelineError(f"{label} must be a directory")


def _reject_source_alias(
    destination: Path,
    sources: Sequence[SourceFile],
    label: str,
) -> None:
    if any(_paths_alias(destination, source.path) for source in sources):
        raise PipelineError(f"{label} aliases a source file")


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


def _validate_path_plan(
    inputs: Sequence[Path],
    *,
    protected_files: Sequence[Path],
    work_dir: Path,
    output_dir: Path | None = None,
    host_manifest: Path | None = None,
) -> None:
    """Reject input and destination overlap before discovery creates work files."""
    input_dirs: list[Path] = []
    input_files: list[Path] = []
    for raw_input in inputs:
        input_path = Path(raw_input)
        if input_path.is_dir():
            input_dirs.append(input_path)
            try:
                input_files.extend(
                    candidate
                    for candidate in input_path.rglob("*")
                    if candidate.is_file()
                )
            except OSError as error:
                raise PipelineError("explicit input directory cannot be inspected safely") from error
        else:
            input_files.append(input_path)

    for input_dir in input_dirs:
        if any(_contains(input_dir, destination) for destination in protected_files):
            raise PipelineError("planned output conflicts with an explicit input directory")
        if _paths_overlap(input_dir, work_dir):
            raise PipelineError("work directory conflicts with an explicit input directory")
        if output_dir is not None and _paths_overlap(input_dir, output_dir):
            raise PipelineError("output directory conflicts with an explicit input directory")

    for input_file in input_files:
        if any(_paths_alias(input_file, destination) for destination in protected_files):
            raise PipelineError("planned output conflicts with an explicit input")
        if _contains(work_dir, input_file) or _paths_alias(input_file, work_dir):
            raise PipelineError("work directory conflicts with an explicit input")
        if output_dir is not None and _contains(output_dir, input_file):
            raise PipelineError("output directory conflicts with an explicit input")

    if any(_contains(work_dir, destination) for destination in protected_files):
        raise PipelineError("planned output conflicts with the work directory")
    if output_dir is not None and _paths_overlap(output_dir, work_dir):
        raise PipelineError("output directory conflicts with the work directory")

    if host_manifest is not None:
        if any(_paths_alias(host_manifest, destination) for destination in protected_files):
            raise PipelineError("host manifest conflicts with a planned output")
        if _contains(work_dir, host_manifest) or _paths_alias(host_manifest, work_dir):
            raise PipelineError("host manifest conflicts with the work directory")
        if output_dir is not None and _paths_alias(host_manifest, output_dir):
            raise PipelineError("host manifest conflicts with the output directory")


def _validate_discovered_sources(
    sources: Sequence[SourceFile],
    *,
    protected_files: Sequence[Path],
) -> None:
    """Re-check materialized sources before normalization or output writes."""
    for source in sources:
        if any(_paths_alias(source.path, destination) for destination in protected_files):
            raise PipelineError("planned output conflicts with a discovered source")


def _paths_overlap(first: Path, second: Path) -> bool:
    return _contains(first, second) or _contains(second, first)


def _contains(directory: Path, candidate: Path) -> bool:
    try:
        resolved_directory = directory.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return (
        resolved_candidate == resolved_directory
        or resolved_directory in resolved_candidate.parents
    )


def _warning(code: str, page_id: str, message: str, action: str) -> WarningItem:
    return WarningItem(
        code=code,
        source_page_ids=(page_id,),
        output_page=None,
        message=message,
        action=action,
        severity="warning",
    )


def _ordered_warnings(warnings: Sequence[WarningItem]) -> list[WarningItem]:
    return sorted(
        warnings,
        key=lambda item: (
            item.severity,
            item.code,
            item.source_page_ids,
            -1 if item.output_page is None else item.output_page,
            item.message,
            item.action,
        ),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
