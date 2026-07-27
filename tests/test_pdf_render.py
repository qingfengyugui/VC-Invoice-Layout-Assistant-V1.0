from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter

from invoice_layout.pdf_render import render_pdf_page


def test_in_process_renderer_returns_requested_page_at_requested_dpi(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "two-pages.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=144)
    writer.add_blank_page(width=144, height=72)
    with pdf.open("wb") as output:
        writer.write(output)

    with render_pdf_page(pdf, page_index=1, dpi=200) as image:
        assert image.size == (400, 200)
        assert image.mode == "RGB"
