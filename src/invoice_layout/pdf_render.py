"""In-process deterministic PDF rendering without external Poppler tools."""

from __future__ import annotations

from pathlib import Path

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image


def render_pdf_page(pdf_path: Path, *, page_index: int, dpi: int) -> Image.Image:
    """Render one zero-based PDF page to an owned RGB Pillow image."""
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    document = pdfium.PdfDocument(str(pdf_path))
    try:
        if page_index < 0 or page_index >= len(document):
            raise IndexError("PDF page index is out of range")
        page = document[page_index]
        try:
            bitmap = page.render(scale=dpi / 72.0)
            try:
                return bitmap.to_pil().convert("RGB").copy()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()
