"""Read-only adapter for observations supplied by the host platform."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from invoice_layout.models import Observation, PageAsset
from invoice_layout.observation_exchange import load_host_observations


class HostManifestProvider:
    """Expose an already-validated host manifest through the provider contract."""

    def __init__(self, observations: Mapping[str, Observation]) -> None:
        self._observations = dict(observations)

    @classmethod
    def from_manifest(cls, path: Path, pages: Sequence[PageAsset]) -> HostManifestProvider:
        """Load only observations whose page IDs and preview hashes match ``pages``."""
        return cls(load_host_observations(path, pages))

    def observe(self, page: PageAsset) -> Observation:
        """Return the manifest observation for an exact normalized page."""
        try:
            return self._observations[page.id]
        except KeyError as error:
            raise ValueError(f"host manifest has no observation for page {page.id}") from error
