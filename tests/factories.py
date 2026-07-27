"""Non-financial geometry fixtures shared by invoice-layout tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import Color
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from invoice_layout.config import Settings
from invoice_layout.models import DocumentType, Observation, PageAsset, SourceFile


def _source_file(path: Path) -> SourceFile:
    return SourceFile(
        id=hashlib.sha256(path.read_bytes()).hexdigest()[:16],
        path=path,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        media_type={".pdf": "application/pdf", ".png": "image/png"}[path.suffix],
    )


def _fixture_path(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def make_blank_pdf(directory: Path, *, width: float = A4[0], height: float = A4[1]) -> list[SourceFile]:
    """Create a one-page blank PDF with a deterministic geometry size."""
    path = _fixture_path(directory, "blank-test.pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=height)
    with path.open("wb") as output:
        writer.write(output)
    return [_source_file(path)]


def make_encrypted_pdf(directory: Path, password: str = "geometry-test") -> list[SourceFile]:
    """Create a password-protected blank PDF for parser failure tests."""
    path = _fixture_path(directory, "encrypted-test.pdf")
    writer = PdfWriter()
    writer.add_blank_page(width=A4[0], height=A4[1])
    with path.open("wb") as output:
        writer.write(output)
    reader = PdfReader(path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)
    with path.open("wb") as output:
        writer.write(output)
    return [_source_file(path)]


def make_color_image(
    directory: Path, *, size: tuple[int, int] = (640, 480), color: tuple[int, int, int] = (32, 128, 224)
) -> list[SourceFile]:
    """Create a solid, labeled raster fixture without financial-like content."""
    path = _fixture_path(directory, "color-test.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", size, color)
    ImageDraw.Draw(image).rectangle((16, 16, size[0] - 16, size[1] - 16), outline="white", width=4)
    ImageDraw.Draw(image).text((28, 28), "GEOMETRY-TEST", fill="white")
    image.save(path)
    return [_source_file(path)]


def make_vector_pdf(directory: Path, *, width: float = A4[0], height: float = A4[1]) -> list[SourceFile]:
    """Create a one-page vector-only geometry fixture."""
    path = _fixture_path(directory, "vector-test.pdf")
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas = Canvas(str(path), pagesize=(width, height))
    canvas.setFillColor(Color(0.12, 0.45, 0.8))
    canvas.rect(width * 0.2, height * 0.2, width * 0.6, height * 0.6, fill=1, stroke=0)
    canvas.setFillColor(Color(1, 1, 1))
    canvas.drawString(width * 0.25, height * 0.5, "VECTOR-TEST")
    canvas.save()
    return [_source_file(path)]


def make_page_asset(path: Path, *, source_id: str = "source-test", page_index: int = 0) -> PageAsset:
    """Create the minimal deterministic page asset used by layout tests."""
    page_pdf = make_blank_pdf(path)[0].path
    preview_png = make_color_image(path, size=(640, 480))[0].path
    return PageAsset(
        id=f"{source_id}-{page_index}",
        source_id=source_id,
        source_path=page_pdf,
        page_index=page_index,
        page_pdf=page_pdf,
        preview_png=preview_png,
        width_pt=A4[0],
        height_pt=A4[1],
        pixel_width=640,
        pixel_height=480,
    )


def make_observation(
    *, document_type: DocumentType = DocumentType.UNKNOWN, confidence: float = 0.9
) -> Observation:
    """Create a non-financial observation for association and layout tests."""
    return Observation(
        document_type=document_type,
        text="OBSERVATION-TEST",
        confidence=confidence,
        evidence=("factory",),
    )


def test_settings(work_dir: Path, **overrides: object) -> Settings:
    """Return deterministic local settings without credentials."""
    return Settings(provider="local", work_dir=work_dir, **overrides)
