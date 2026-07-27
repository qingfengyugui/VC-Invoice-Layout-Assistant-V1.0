from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ContentStream
from reportlab.lib.colors import blue, red
from reportlab.pdfgen.canvas import Canvas

from invoice_layout.compose import compose_ticket_pages
from invoice_layout.models import CropBox, PageAsset, Placement
from invoice_layout.pdf_render import render_pdf_page

MM_TO_PT = 72 / 25.4
A4_WIDTH_PT = 210 * MM_TO_PT
A4_HEIGHT_PT = 297 * MM_TO_PT
PAINT_OPERATORS = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"sh"}


def _asset(
    tmp_path: Path,
    asset_id: str,
    *,
    text: str = "VECTOR-TEST",
    split_colors: bool = False,
) -> PageAsset:
    pdf_path = tmp_path / f"{asset_id}.pdf"
    canvas = Canvas(str(pdf_path), pagesize=(100, 50), pageCompression=0)
    canvas.setFillColor(red)
    canvas.rect(0, 0, 50 if split_colors else 100, 50, stroke=0, fill=1)
    if split_colors:
        canvas.setFillColor(blue)
        canvas.rect(50, 0, 50, 50, stroke=0, fill=1)
    canvas.setFillColorRGB(0, 0, 0)
    canvas.drawString(5, 20, text)
    canvas.showPage()
    canvas.save()
    return PageAsset(
        id=asset_id,
        source_id=f"source-{asset_id}",
        source_path=pdf_path,
        page_index=0,
        page_pdf=pdf_path,
        preview_png=tmp_path / f"{asset_id}.png",
        width_pt=100,
        height_pt=50,
        pixel_width=200,
        pixel_height=100,
    )


def _placement(
    asset_id: str,
    *,
    crop: CropBox | None = None,
    x_mm: float = 10,
    y_mm: float = 10,
    width_mm: float = 100,
    height_mm: float = 50,
    output_page_index: int = 0,
) -> Placement:
    return Placement(
        page_asset_id=asset_id,
        crop=crop or CropBox(x0=0, y0=0, x1=1, y1=1),
        x_mm=x_mm,
        y_mm=y_mm,
        width_mm=width_mm,
        height_mm=height_mm,
        output_page_index=output_page_index,
    )


def _render(pdf_path: Path, _output_prefix: Path) -> Image.Image:
    return render_pdf_page(pdf_path, page_index=0, dpi=72)


def _pixel_at_mm(image: Image.Image, x_mm: float, y_mm: float) -> tuple[int, int, int]:
    x = round(x_mm * image.width / 210)
    y = round(y_mm * image.height / 297)
    return image.convert("RGB").getpixel((x, y))


def _is_red(pixel: tuple[int, int, int]) -> bool:
    return pixel[0] > 200 and pixel[1] < 80 and pixel[2] < 80


def test_composes_exact_a4_page_count_and_preserves_vector_text(tmp_path: Path) -> None:
    first = _asset(tmp_path, "first", text="PAGE-ZERO-TEST")
    second = _asset(tmp_path, "second", text="PAGE-ONE-TEST")
    output = compose_ticket_pages(
        [
            _placement("second", output_page_index=1),
            _placement("first", output_page_index=0),
        ],
        [first, second],
        tmp_path / "tickets.pdf",
    )

    reader = PdfReader(output)
    assert len(reader.pages) == 2
    assert all(float(page.mediabox.width) == pytest.approx(A4_WIDTH_PT) for page in reader.pages)
    assert all(float(page.mediabox.height) == pytest.approx(A4_HEIGHT_PT) for page in reader.pages)
    assert "PAGE-ZERO-TEST" in (reader.pages[0].extract_text() or "")
    assert "PAGE-ONE-TEST" in (reader.pages[1].extract_text() or "")


def test_top_origin_millimetres_map_to_expected_page_region(tmp_path: Path) -> None:
    asset = _asset(tmp_path, "positioned")
    output = compose_ticket_pages(
        [_placement("positioned", x_mm=20, y_mm=30, width_mm=40, height_mm=20)],
        [asset],
        tmp_path / "tickets.pdf",
    )

    with _render(output, tmp_path / "positioned-render") as image:
        assert _is_red(_pixel_at_mm(image, 40, 35))
        assert _pixel_at_mm(image, 40, 20) == (255, 255, 255)
        assert _pixel_at_mm(image, 10, 40) == (255, 255, 255)


def test_crop_selects_original_region_without_mutating_source(tmp_path: Path) -> None:
    asset = _asset(tmp_path, "cropped", split_colors=True)
    before = asset.page_pdf.read_bytes()
    output = compose_ticket_pages(
        [
            _placement(
                "cropped",
                crop=CropBox(x0=0, y0=0, x1=0.5, y1=1),
                x_mm=10,
                y_mm=10,
                width_mm=40,
                height_mm=40,
            )
        ],
        [asset],
        tmp_path / "tickets.pdf",
    )

    with _render(output, tmp_path / "cropped-render") as image:
        assert _is_red(_pixel_at_mm(image, 30, 30))
        pixels = image.convert("RGB").getdata()
        assert not any(pixel[2] > 180 and pixel[0] < 100 for pixel in pixels)
    assert asset.page_pdf.read_bytes() == before


def test_output_path_cannot_alias_normalized_page_or_original_source(
    tmp_path: Path,
) -> None:
    normalized_asset = _asset(tmp_path, "normalized-alias")
    normalized_before = normalized_asset.page_pdf.read_bytes()

    with pytest.raises(ValueError, match="source"):
        compose_ticket_pages(
            [_placement(normalized_asset.id)],
            [normalized_asset],
            normalized_asset.page_pdf,
        )

    original_source = tmp_path / "original-source.png"
    original_source.write_bytes(b"immutable-original-source")
    source_asset = _asset(tmp_path, "source-alias").model_copy(
        update={"source_path": original_source}
    )
    with pytest.raises(ValueError, match="source"):
        compose_ticket_pages(
            [_placement(source_asset.id)],
            [source_asset],
            original_source,
        )

    assert normalized_asset.page_pdf.read_bytes() == normalized_before
    assert original_source.read_bytes() == b"immutable-original-source"


def test_output_path_cannot_be_hardlink_to_source(tmp_path: Path) -> None:
    asset = _asset(tmp_path, "hardlink-source")
    alias = tmp_path / "hardlink-output.pdf"
    os.link(asset.page_pdf, alias)
    before = asset.page_pdf.read_bytes()

    with pytest.raises(ValueError, match="source"):
        compose_ticket_pages(
            [_placement(asset.id)],
            [asset],
            alias,
        )

    assert asset.page_pdf.read_bytes() == before
    assert alias.read_bytes() == before


def test_actual_pdf_geometry_must_match_asset_metadata_before_composition(
    tmp_path: Path,
) -> None:
    asset = _asset(tmp_path, "bad-metadata").model_copy(
        update={"width_pt": 200.0}
    )

    with pytest.raises(ValueError, match="metadata"):
        compose_ticket_pages(
            [_placement(asset.id, width_mm=100, height_mm=25)],
            [asset],
            tmp_path / "tickets.pdf",
        )


def test_composer_adds_no_text_or_painted_drawing_operators(tmp_path: Path) -> None:
    asset = _asset(tmp_path, "pure", text="ONLY-SOURCE-TEST")
    output = compose_ticket_pages(
        [_placement("pure")],
        [asset],
        tmp_path / "tickets.pdf",
    )

    source_reader = PdfReader(asset.page_pdf)
    output_reader = PdfReader(output)
    source_ops = ContentStream(source_reader.pages[0].get_contents(), source_reader).operations
    output_ops = ContentStream(output_reader.pages[0].get_contents(), output_reader).operations
    source_paints = [operator for _, operator in source_ops if operator in PAINT_OPERATORS]
    output_paints = [operator for _, operator in output_ops if operator in PAINT_OPERATORS]

    assert (output_reader.pages[0].extract_text() or "").strip() == "ONLY-SOURCE-TEST"
    assert output_paints == source_paints


def test_composer_strips_source_pdf_annotations_without_changing_content(
    tmp_path: Path,
) -> None:
    asset = _asset(tmp_path, "annotated", text="ANNOTATED-SOURCE-TEST")
    reader = PdfReader(asset.page_pdf)
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.add_annotation(
        0,
        {
            "/Type": "/Annot",
            "/Subtype": "/Text",
            "/Rect": [0, 0, 10, 10],
            "/Contents": "SYNTHETIC-PRIVATE-NOTE",
        },
    )
    with asset.page_pdf.open("wb") as stream:
        writer.write(stream)

    output = compose_ticket_pages(
        [_placement(asset.id)],
        [asset],
        tmp_path / "tickets.pdf",
    )

    output_page = PdfReader(output).pages[0]
    assert "/Annots" not in output_page
    assert "ANNOTATED-SOURCE-TEST" in (output_page.extract_text() or "")
    assert "SYNTHETIC-PRIVATE-NOTE" not in output.read_text(
        encoding="latin-1", errors="ignore"
    )


def test_output_metadata_contains_no_dynamic_timestamps(tmp_path: Path) -> None:
    asset = _asset(tmp_path, "metadata")
    first = compose_ticket_pages(
        [_placement(asset.id)],
        [asset],
        tmp_path / "first.pdf",
    )
    second = compose_ticket_pages(
        [_placement(asset.id)],
        [asset],
        tmp_path / "second.pdf",
    )

    metadata = dict(PdfReader(first).metadata or {})
    assert "/CreationDate" not in metadata
    assert "/ModDate" not in metadata
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize(
    ("placements", "assets", "message"),
    [
        (
            [_placement("one"), _placement("one", output_page_index=1)],
            "one",
            "duplicate",
        ),
        ([_placement("missing")], "one", "missing"),
        ([_placement("one", x_mm=-1)], "one", "geometry"),
        ([_placement("one", x_mm=150, width_mm=100)], "one", "bounds"),
        (
            [
                _placement("one", x_mm=10, y_mm=10, width_mm=40, height_mm=20),
                _placement("two", x_mm=30, y_mm=20, width_mm=40, height_mm=20),
            ],
            "both",
            "overlap",
        ),
        ([_placement("one", width_mm=40, height_mm=40)], "one", "aspect"),
        ([_placement("one", output_page_index=-1)], "one", "page index"),
        ([_placement("one", output_page_index=1)], "one", "contiguous"),
    ],
)
def test_invalid_placements_fail_without_replacing_output(
    tmp_path: Path,
    placements: list[Placement],
    assets: str,
    message: str,
) -> None:
    first = _asset(tmp_path, "one")
    selected_assets = [first]
    if assets == "both":
        selected_assets.append(_asset(tmp_path, "two"))
    output = tmp_path / "tickets.pdf"
    output.write_bytes(b"existing-output")

    with pytest.raises(ValueError, match=message):
        compose_ticket_pages(placements, selected_assets, output)

    assert output.read_bytes() == b"existing-output"
