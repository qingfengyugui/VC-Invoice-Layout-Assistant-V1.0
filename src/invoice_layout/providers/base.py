"""Common contract for page observation providers."""

from __future__ import annotations

from typing import Protocol

from invoice_layout.models import Observation, PageAsset


class ObservationProvider(Protocol):
    """Describe one page without modifying its immutable preview or source."""

    def observe(self, page: PageAsset) -> Observation:
        """Return a validated observation for ``page``."""
