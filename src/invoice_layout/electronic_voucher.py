"""Safe adapters for electronic-voucher source formats."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from .runtime import java_executable

MAX_XML_BYTES = 20 * 1024 * 1024
OFD_TIMEOUT_SECONDS = 120


class OFDConversionError(RuntimeError):
    """Raised when a fixed-layout OFD cannot be converted faithfully."""


def parse_voucher_xml(path: Path) -> dict[str, str]:
    """Extract non-empty leaf text by namespace-local element name.

    XML is metadata only; callers must never use these fields to fabricate a
    document layout.  ``defusedxml`` rejects DTDs, external entities, and
    entity expansion before any fields are returned.
    """
    if path.stat().st_size > MAX_XML_BYTES:
        raise ValueError("XML voucher exceeds 20 MiB limit")
    with path.open("rb") as source:
        payload = source.read(MAX_XML_BYTES + 1)
    if len(payload) > MAX_XML_BYTES:
        raise ValueError("XML voucher exceeds 20 MiB limit")
    try:
        root = ElementTree.fromstring(payload)
    except DefusedXmlException as error:
        raise ValueError("unsafe XML voucher") from error
    except (ElementTree.ParseError, UnicodeError) as error:
        raise ValueError("invalid XML voucher") from error

    fields: dict[str, str] = {}
    for element in root.iter():
        if len(element):
            continue
        value = (element.text or "").strip()
        if value:
            fields.setdefault(_local_name(element.tag), value)
    return fields


def convert_ofd_to_pdf(source: Path, output: Path, renderer: Path) -> Path:
    """Export an OFD with the deterministic OFDRW renderer, without rewriting it."""
    if not renderer.is_file():
        raise OFDConversionError("OFD renderer JAR is unavailable")
    java = java_executable()
    if java is None:
        raise OFDConversionError("Java runtime is unavailable")
    temporary: Path | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.stem}-",
            suffix=output.suffix or ".pdf",
            dir=output.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        command = [str(java), "-jar", str(renderer), str(source), str(temporary)]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=OFD_TIMEOUT_SECONDS)
        if temporary.stat().st_size == 0:
            raise OFDConversionError("OFD renderer did not create a PDF")
        temporary.replace(output)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise OFDConversionError(type(error).__name__) from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return output


def _local_name(tag: str) -> str:
    """Return the namespace-independent XML element name."""
    return tag.rsplit("}", maxsplit=1)[-1]
