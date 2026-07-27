"""Strict JSON handoff between normalized previews and a host multimodal agent."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from invoice_layout.models import Observation, PageAsset

_MANIFEST_FIELDS = frozenset({"page_id", "preview_sha256", "observation"})


class HostManifestError(ValueError):
    """Raised when host-supplied JSON cannot be safely bound to normalized pages."""


def prepare_observation_request(pages: Sequence[PageAsset], output: Path) -> Path:
    """Write a schema-bound request over immutable normalized preview files.

    The request deliberately contains no image-generation or image-modification
    instruction: the host only inspects the listed previews and returns JSON.
    """
    _require_unique_page_ids(pages)
    payload = {
        "observation_schema": Observation.model_json_schema(),
        "pages": [
            {
                "page_id": page.id,
                "preview_path": str(page.preview_png),
                "preview_sha256": _sha256(page.preview_png),
            }
            for page in pages
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), "utf-8")
    return output


def load_host_observations(path: Path, pages: Sequence[PageAsset]) -> dict[str, Observation]:
    """Accept a host manifest only when it covers exactly the supplied previews."""
    _require_unique_page_ids(pages)
    manifest = _load_manifest(path)
    entries = manifest.get("pages")
    if set(manifest) != {"pages"} or not isinstance(entries, list):
        raise HostManifestError("host manifest must contain only a pages list")

    expected_hashes = {page.id: _sha256(page.preview_png) for page in pages}
    received_ids: list[str] = []
    observations: dict[str, Observation] = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _MANIFEST_FIELDS:
            raise HostManifestError("host manifest entry has unexpected fields")
        page_id = entry["page_id"]
        preview_hash = entry["preview_sha256"]
        raw_observation = entry["observation"]
        if not isinstance(page_id, str) or not isinstance(preview_hash, str):
            raise HostManifestError("host manifest entry has invalid identifiers")
        received_ids.append(page_id)
        expected_hash = expected_hashes.get(page_id)
        if expected_hash is None:
            continue
        if not hmac.compare_digest(preview_hash, expected_hash):
            raise HostManifestError(f"preview hash does not match page {page_id}")
        if not isinstance(raw_observation, Mapping):
            raise HostManifestError(f"invalid observation for page {page_id}")
        try:
            observations[page_id] = Observation.model_validate_json(
                json.dumps(raw_observation), strict=True, extra="forbid"
            )
        except ValidationError as error:
            raise HostManifestError(f"invalid observation for page {page_id}") from error

    if set(received_ids) != set(expected_hashes) or len(received_ids) != len(set(received_ids)):
        raise HostManifestError("host manifest page ids do not exactly match normalized pages")
    return observations


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostManifestError("host manifest is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise HostManifestError("host manifest must be a JSON object")
    return parsed


def _require_unique_page_ids(pages: Sequence[PageAsset]) -> None:
    page_ids = [page.id for page in pages]
    if len(page_ids) != len(set(page_ids)):
        raise ValueError("normalized page ids must be unique")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
