"""Best-effort local text extraction with deliberately conservative confidence."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader

from invoice_layout.models import Observation, PageAsset, SourceFile

_Metadata = tuple[tuple[str, str], ...]
_EXACT_METADATA_FIELDS = {
    "invoiceno": "invoice_number",
    "invoicenumber": "invoice_number",
    "orderno": "order_number",
    "ordernumber": "order_number",
    "vendor": "vendor",
    "seller": "vendor",
    "sellername": "vendor",
    "passenger": "traveler",
    "traveler": "traveler",
}


class LocalOCRProvider:
    """Use local PDF text and optional Tesseract without asserting model-level certainty.

    ``metadata_by_source_id`` carries structured XML metadata from ``SourceFile``
    through later pipeline stages; XML itself is never rendered into a page.
    """

    def __init__(self, metadata_by_source_id: Mapping[str, _Metadata] | None = None) -> None:
        self._metadata_by_source_id = dict(metadata_by_source_id or {})

    @classmethod
    def from_sources(cls, sources: Sequence[SourceFile]) -> LocalOCRProvider:
        """Join one XML companion to one renderable source by normalized filename stem.

        XML sources do not create pages. Metadata is therefore transferred only
        when an XML source and its PDF/OFD/image companion are each unique;
        ambiguous file names deliberately receive no inferred metadata.
        """
        xml_by_stem: dict[str, list[SourceFile]] = {}
        renderable_by_stem: dict[str, list[SourceFile]] = {}
        for source in sources:
            stem = _normalized_stem(source)
            if source.media_type == "application/xml":
                xml_by_stem.setdefault(stem, []).append(source)
            elif _is_renderable(source):
                renderable_by_stem.setdefault(stem, []).append(source)

        metadata_by_source_id: dict[str, _Metadata] = {}
        for stem, xml_sources in xml_by_stem.items():
            renderable_sources = renderable_by_stem.get(stem, [])
            if len(xml_sources) == len(renderable_sources) == 1 and xml_sources[0].metadata:
                metadata_by_source_id[renderable_sources[0].id] = xml_sources[0].metadata
        return cls(metadata_by_source_id)

    def observe(self, page: PageAsset) -> Observation:
        """Extract local text and use exact structured metadata when available."""
        text, evidence = _extract_local_text(page)
        fields = _exact_metadata_fields(self._metadata_by_source_id.get(page.source_id, ()))
        if fields:
            return Observation.model_validate(
                {
                    "text": text,
                    "confidence": 0.7,
                    "evidence": evidence + ("structured_xml_metadata",),
                    **fields,
                }
            )
        return Observation(text=text, confidence=0.4 if text else 0.2, evidence=evidence)


def _extract_local_text(page: PageAsset) -> tuple[str, tuple[str, ...]]:
    """Extract embedded PDF text, then use Tesseract only when it is installed."""
    try:
        text = "\n".join(filter(None, (item.extract_text() for item in PdfReader(page.page_pdf).pages))).strip()
    except Exception:  # noqa: BLE001 - malformed PDFs must remain a low-confidence result.
        text = ""
    if text:
        return text, ("embedded_pdf_text",)
    try:
        import pytesseract  # type: ignore[import-not-found]
        from PIL import Image

        with Image.open(page.preview_png) as preview:
            text = pytesseract.image_to_string(preview).strip()
    except Exception:  # noqa: BLE001 - Tesseract is optional and may be unavailable.
        text = ""
    return (text, ("tesseract",) if text else ())


def _exact_metadata_fields(metadata: _Metadata) -> dict[str, object]:
    """Map trusted scalar XML fields without trying to infer document structure."""
    result: dict[str, object] = {}
    for key, value in metadata:
        destination = _EXACT_METADATA_FIELDS.get(key.casefold())
        if destination and value:
            result.setdefault(destination, value)
        if key.casefold() == "amount" and value:
            try:
                result.setdefault("amount", Decimal(value))
            except InvalidOperation:
                continue
    return result


def _normalized_stem(source: SourceFile) -> str:
    """Return a cross-platform, case-insensitive filename stem for companion matching."""
    return unicodedata.normalize("NFKC", source.path.stem).casefold()


def _is_renderable(source: SourceFile) -> bool:
    """Return whether a source can produce a printable page asset."""
    return source.media_type in {"application/pdf", "application/ofd"} or source.media_type.startswith("image/")
