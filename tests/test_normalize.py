from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image
from pypdf import PdfReader, PdfWriter

from invoice_layout.models import SourceFile
from invoice_layout.normalize import normalize_sources
from tests.factories import (
    make_blank_pdf,
    make_color_image,
    make_encrypted_pdf,
    make_vector_pdf,
)
from tests.factories import test_settings as settings_for_tests


def test_normalizes_pdf_and_image_to_page_assets(tmp_path: Path) -> None:
    sources = make_blank_pdf(tmp_path) + make_color_image(tmp_path)

    pages, warnings = normalize_sources(sources, settings_for_tests(tmp_path))

    assert len(pages) == 2
    assert all(page.page_pdf.exists() and page.preview_png.exists() for page in pages)
    assert warnings == []


def test_password_protected_pdf_is_isolated(tmp_path: Path) -> None:
    sources = make_encrypted_pdf(tmp_path)

    pages, warnings = normalize_sources(sources, settings_for_tests(tmp_path))

    assert pages == []
    assert [warning.code for warning in warnings] == ["password_protected"]


def test_pdf_normalization_preserves_vector_text_and_preview_geometry(tmp_path: Path) -> None:
    source = make_vector_pdf(tmp_path)[0]
    settings = settings_for_tests(tmp_path, render_dpi=200)

    pages, warnings = normalize_sources([source], settings)

    assert warnings == []
    assert PdfReader(pages[0].page_pdf).pages[0].extract_text().strip() == "VECTOR-TEST"
    assert pages[0].pixel_width == round(pages[0].width_pt / 72 * settings.render_dpi)
    assert pages[0].pixel_height == round(pages[0].height_pt / 72 * settings.render_dpi)


def test_rotated_pdf_uses_rendered_page_geometry(tmp_path: Path) -> None:
    path = tmp_path / "rotated.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=72, height=144)
    page.rotate(90)
    with path.open("wb") as output:
        writer.write(output)

    pages, warnings = normalize_sources([_source(path, "application/pdf")], settings_for_tests(tmp_path, render_dpi=200))

    assert warnings == []
    assert (pages[0].width_pt, pages[0].height_pt) == (144.0, 72.0)
    assert pages[0].pixel_width > pages[0].pixel_height


def test_normalizes_each_pdf_page_into_a_distinct_asset(tmp_path: Path) -> None:
    path = tmp_path / "two-pages.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=144, height=72)
    with path.open("wb") as output:
        writer.write(output)
    source = _source(path, "application/pdf")

    pages, warnings = normalize_sources([source], settings_for_tests(tmp_path))

    assert warnings == []
    assert [page.page_index for page in pages] == [0, 1]
    assert [page.id for page in pages] == [f"{source.id}-0", f"{source.id}-1"]
    assert len({page.page_pdf for page in pages}) == 2
    assert all(page.preview_png.exists() for page in pages)


def test_preview_rendering_does_not_require_external_programs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PATH", "")

    pages, warnings = normalize_sources(
        make_blank_pdf(tmp_path), settings_for_tests(tmp_path)
    )

    assert warnings == []
    assert pages[0].preview_png.is_file()


def test_image_normalization_applies_exif_orientation_before_wrapping(tmp_path: Path) -> None:
    path = tmp_path / "rotated.jpg"
    image = Image.new("RGB", (72, 144), "steelblue")
    exif = image.getexif()
    exif[274] = 6
    image.save(path, exif=exif)
    source = _source(path, "image/jpeg")

    pages, warnings = normalize_sources([source], settings_for_tests(tmp_path, render_dpi=200))

    assert warnings == []
    assert (pages[0].width_pt, pages[0].height_pt) == (144.0, 72.0)
    assert pages[0].pixel_width > pages[0].pixel_height


def test_normalizes_every_frame_of_a_multi_page_tiff(tmp_path: Path) -> None:
    path = tmp_path / "two-frames.tiff"
    first = Image.new("RGB", (72, 144), "red")
    second = Image.new("RGB", (144, 72), "blue")
    first.save(path, save_all=True, append_images=[second], format="TIFF")
    source = _source(path, "image/tiff")

    pages, warnings = normalize_sources(
        [source], settings_for_tests(tmp_path, render_dpi=200)
    )

    assert warnings == []
    assert [page.page_index for page in pages] == [0, 1]
    assert [page.id for page in pages] == [f"{source.id}-0", f"{source.id}-1"]
    assert [(page.width_pt, page.height_pt) for page in pages] == [
        (72.0, 144.0),
        (144.0, 72.0),
    ]
    assert all(page.page_pdf.is_file() and page.preview_png.is_file() for page in pages)


def test_corrupt_source_becomes_warning_without_stopping_other_sources(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a PDF")
    sources = [_source(corrupt, "application/pdf"), *make_color_image(tmp_path)]

    pages, warnings = normalize_sources(sources, settings_for_tests(tmp_path))

    assert len(pages) == 1
    assert [warning.code for warning in warnings] == ["normalization_failed"]


def _source(path: Path, media_type: str) -> SourceFile:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return SourceFile(id=digest[:16], path=path, sha256=digest, media_type=media_type)
