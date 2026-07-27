"""Observation providers that never own external model credentials."""

from .base import ObservationProvider
from .host_manifest import HostManifestProvider
from .local_ocr import LocalOCRProvider

__all__ = ["HostManifestProvider", "LocalOCRProvider", "ObservationProvider"]
