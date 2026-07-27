"""Lossless page normalization for source PDFs and images."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from .config import Settings
from .electronic_voucher import (
    OFDConversionError,
    convert_ofd_to_pdf,
)
from .models import PageAsset, SourceFile, WarningItem
from .pdf_render import render_pdf_page


class PasswordProtectedError(ValueError):
    """Raised when a PDF cannot be safely read without a password."""


def normalize_sources(
    sources: list[SourceFile], settings: Settings
) -> tuple[list[PageAsset], list[WarningItem]]:
    """Split PDFs or wrap oriented images into independent printable pages."""
    pages: list[PageAsset] = []
    warnings: list[WarningItem] = []
    for source in sources:
        if source.path.suffix.lower() == ".xml":
            warnings.append(
                _source_warning(
                    "xml_requires_layout_companion",
                    source,
                    "XML voucher metadata requires an OFD or PDF layout companion",
                )
            )
            continue
        try:
            suffix = source.path.suffix.lower()
            if suffix == ".pdf":
                produced = _normalize_pdf(source, settings)
            elif suffix == ".ofd":
                produced = _normalize_ofd(source, settings)
            else:
                produced = _normalize_image(source, settings)
            pages.extend(produced)
        except PasswordProtectedError:
            warnings.append(_source_warning("password_protected", source, "PDF is password protected"))
        except OFDConversionError as error:
            warnings.append(_source_warning("ofd_conversion_failed", source, str(error)))
        except Exception as error:  # noqa: BLE001 - parser and renderer errors have varied types.
            warnings.append(_source_warning("normalization_failed", source, type(error).__name__))
    return pages, warnings


def _normalize_pdf(source: SourceFile, settings: Settings) -> list[PageAsset]:
    return _normalize_pdf_path(source, source.path, settings)


def _normalize_ofd(source: SourceFile, settings: Settings) -> list[PageAsset]:
    output_dir = _source_output_dir(source, settings)
    converted_pdf = convert_ofd_to_pdf(source.path, output_dir / "converted.pdf", _ofd_renderer_path())
    return _normalize_pdf_path(source, converted_pdf, settings)


def _normalize_pdf_path(source: SourceFile, pdf_path: Path, settings: Settings) -> list[PageAsset]:
    reader = PdfReader(pdf_path)
    if reader.is_encrypted:
        raise PasswordProtectedError

    output_dir = _source_output_dir(source, settings)
    output_dir.mkdir(parents=True, exist_ok=True)
    pages: list[PageAsset] = []
    for index, page in enumerate(reader.pages):
        page_pdf = output_dir / f"{index:04d}.pdf"
        writer = PdfWriter()
        writer.add_page(page)
        with page_pdf.open("wb") as output:
            writer.write(output)
        preview_png = _render_preview(page_pdf, settings)
        width_pt, height_pt = _display_dimensions(page)
        pages.append(
            _page_asset(
                source,
                index,
                page_pdf,
                preview_png,
                width_pt,
                height_pt,
            )
        )
    return pages


def _ofd_renderer_path() -> Path:
    """Return a packaged, configured, or developer-built OFDRW JAR."""
    configured = os.getenv("INVOICE_LAYOUT_OFD_RENDERER")
    if configured:
        configured_path = Path(configured)
        if not configured_path.is_file():
            raise OFDConversionError("configured OFD renderer JAR is unavailable")
        return configured_path
    packaged = Path(__file__).resolve().parent / "bin" / "ofd-renderer.jar"
    if packaged.is_file():
        return packaged
    project_root = Path(__file__).resolve().parents[2]
    return project_root / "tools" / "ofd-renderer" / "target" / "ofd-renderer.jar"


def _normalize_image(source: SourceFile, settings: Settings) -> list[PageAsset]:
    output_dir = _source_output_dir(source, settings)
    output_dir.mkdir(parents=True, exist_ok=True)
    pages: list[PageAsset] = []
    with _open_image(source.path) as source_image:
        frame_count = int(getattr(source_image, "n_frames", 1))
        if frame_count < 1:
            raise ValueError("image contains no frames")
        for index in range(frame_count):
            source_image.seek(index)
            with ImageOps.exif_transpose(source_image.copy()) as image:
                width_pt, height_pt = float(image.width), float(image.height)
                page_pdf = output_dir / f"{index:04d}.pdf"
                canvas = Canvas(
                    str(page_pdf),
                    pagesize=(width_pt, height_pt),
                    pageCompression=1,
                )
                canvas.drawImage(
                    ImageReader(image),
                    0,
                    0,
                    width=width_pt,
                    height=height_pt,
                    mask="auto",
                )
                canvas.showPage()
                canvas.save()
            preview_png = _render_preview(page_pdf, settings)
            pages.append(
                _page_asset(
                    source,
                    index,
                    page_pdf,
                    preview_png,
                    width_pt,
                    height_pt,
                )
            )
    return pages


def _open_image(path: Path) -> Image.Image:
    if path.suffix.lower() == ".heic":
        import pillow_heif  # type: ignore[import-not-found,import-untyped]

        pillow_heif.register_heif_opener()
    image = Image.open(path)
    image.load()
    return image


def _page_asset(
    source: SourceFile,
    index: int,
    page_pdf: Path,
    preview_png: Path,
    width_pt: float,
    height_pt: float,
) -> PageAsset:
    with Image.open(preview_png) as preview:
        pixel_width, pixel_height = preview.size
    return PageAsset(
        id=f"{source.id}-{index}",
        source_id=source.id,
        source_path=source.path,
        page_index=index,
        page_pdf=page_pdf,
        preview_png=preview_png,
        width_pt=width_pt,
        height_pt=height_pt,
        pixel_width=pixel_width,
        pixel_height=pixel_height,
    )


def _display_dimensions(page: object) -> tuple[float, float]:
    """Return PDF dimensions after its viewer-applied quarter-turn rotation."""
    media_box = page.mediabox  # type: ignore[attr-defined]
    width_pt, height_pt = float(media_box.width), float(media_box.height)
    rotation = int(page.rotation) % 360  # type: ignore[attr-defined]
    return (height_pt, width_pt) if rotation in {90, 270} else (width_pt, height_pt)


def _render_preview(page_pdf: Path, settings: Settings) -> Path:
    preview_prefix = page_pdf.with_suffix("")
    preview_path = preview_prefix.with_suffix(".png")
    with render_pdf_page(page_pdf, page_index=0, dpi=settings.render_dpi) as image:
        image.save(preview_path, format="PNG")
    return preview_path


def _source_output_dir(source: SourceFile, settings: Settings) -> Path:
    return settings.work_dir / "normalized" / source.id


def _source_warning(code: str, source: SourceFile, detail: str) -> WarningItem:
    return WarningItem(
        code=code,
        source_page_ids=(source.id,),
        output_page=None,
        message=f"{source.path}: {detail}",
        action="review source file",
        severity="warning",
    )
