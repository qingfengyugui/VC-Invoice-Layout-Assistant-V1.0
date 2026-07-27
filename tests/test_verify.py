from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfWriter
from reportlab.lib.utils import ImageReader  # type: ignore[import-untyped]
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from invoice_layout import verify
from invoice_layout.compose import compose_ticket_pages
from invoice_layout.config import Settings
from invoice_layout.models import CropBox, PageAsset, Placement
from invoice_layout.verify import verify_pdf


def _write_pdf(
    path: Path,
    sizes: tuple[tuple[float, float], ...] = ((595.2756, 841.8898),),
    *,
    encrypted: bool = False,
) -> None:
    writer = PdfWriter()
    for width, height in sizes:
        writer.add_blank_page(width=width, height=height)
    if encrypted:
        writer.encrypt("secret")
    with path.open("wb") as stream:
        writer.write(stream)


def _asset(
    tmp_path: Path,
    asset_id: str = "page-a",
    *,
    width_pt: float = 200,
    height_pt: float = 100,
    pixel_width: int = 1200,
    pixel_height: int = 600,
    source_pixel_width: int | None = None,
    source_pixel_height: int | None = None,
    pattern: str | None = None,
) -> PageAsset:
    page_pdf = tmp_path / f"{asset_id}.pdf"
    if pattern is None:
        _write_pdf(page_pdf, ((width_pt, height_pt),))
    else:
        canvas = Canvas(str(page_pdf), pagesize=(width_pt, height_pt))
        canvas.setFillColorRGB(0, 0, 0)
        if pattern == "left":
            canvas.rect(12, 15, 55, 65, fill=1, stroke=0)
            canvas.drawString(80, 45, "SOURCE LEFT")
        elif pattern == "right":
            canvas.circle(155, 50, 30, fill=1, stroke=0)
            canvas.drawString(15, 45, "SOURCE RIGHT")
        elif pattern == "thin":
            canvas.setFont("Helvetica", 8)
            canvas.drawString(80, 48, "TICKET")
        elif pattern.startswith("single-"):
            labels = {
                "single-char-a": "INVOICE CODE A",
                "single-char-b": "INVOICE CODE B",
                "single-digit-0": "INVOICE NO 202607240",
                "single-digit-1": "INVOICE NO 202607241",
                "single-amount-0": "TOTAL 1234.50",
                "single-amount-1": "TOTAL 1234.51",
            }
            canvas.line(10, 75, 190, 75)
            canvas.line(10, 25, 190, 25)
            canvas.setFont("Helvetica", 12)
            canvas.drawString(25, 45, labels[pattern])
        else:
            canvas.line(10, 75, 190, 75)
            canvas.line(10, 25, 190, 25)
            canvas.drawString(
                25,
                45,
                "ALPHA UNIQUE SOURCE" if pattern == "text-a" else "BETA OTHER SOURCE",
            )
        canvas.showPage()
        canvas.save()
    source = tmp_path / f"{asset_id}.png"
    Image.new(
        "1",
        (
            source_pixel_width or pixel_width,
            source_pixel_height or pixel_height,
        ),
    ).save(source)
    preview = tmp_path / f"{asset_id}-preview.png"
    preview.write_bytes(b"immutable-preview")
    return PageAsset(
        id=asset_id,
        source_id=f"source-{asset_id}",
        source_path=source,
        page_index=0,
        page_pdf=page_pdf,
        preview_png=preview,
        width_pt=width_pt,
        height_pt=height_pt,
        pixel_width=pixel_width,
        pixel_height=pixel_height,
    )


def _placement(
    asset_id: str = "page-a",
    *,
    x_mm: float = 20,
    y_mm: float = 20,
    width_mm: float = 100,
    height_mm: float = 50,
    output_page_index: int = 0,
) -> Placement:
    return Placement(
        page_asset_id=asset_id,
        crop=CropBox(x0=0, y0=0, x1=1, y1=1),
        x_mm=x_mm,
        y_mm=y_mm,
        width_mm=width_mm,
        height_mm=height_mm,
        output_page_index=output_page_index,
    )


def _raster_asset(tmp_path: Path, asset_id: str = "page-a") -> PageAsset:
    source = tmp_path / f"{asset_id}.png"
    image = Image.new("RGB", (1200, 600), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 80, 1120, 520), outline="black", width=8)
    draw.text((410, 280), "RASTER TICKET 202607241", fill="black")
    image.save(source)

    page_pdf = tmp_path / f"{asset_id}.pdf"
    canvas = Canvas(str(page_pdf), pagesize=(200, 100))
    canvas.drawImage(ImageReader(str(source)), 0, 0, width=200, height=100)
    canvas.showPage()
    canvas.save()
    preview = tmp_path / f"{asset_id}-preview.png"
    preview.write_bytes(b"immutable-preview")
    return PageAsset(
        id=asset_id,
        source_id=f"source-{asset_id}",
        source_path=source,
        page_index=0,
        page_pdf=page_pdf,
        preview_png=preview,
        width_pt=200,
        height_pt=100,
        pixel_width=1200,
        pixel_height=600,
    )


@pytest.fixture
def rendered_a4(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []

    def render(_pdf: Path, page_number: int, dpi: int) -> tuple[int, int]:
        calls.append((page_number, dpi))
        return 2480, 3508

    monkeypatch.setattr("invoice_layout.verify._render_page_dimensions", render)
    return calls


def test_valid_a4_output_passes_and_renders_at_300_dpi(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    _write_pdf(output)
    asset = _asset(tmp_path)

    ok, warnings = verify_pdf(
        output,
        [_placement()],
        {asset.id: asset},
        Settings(render_dpi=300),
    )

    assert ok is True
    assert warnings == []
    assert rendered_a4 == [(1, 300)]


def test_nonempty_source_cannot_pass_against_blank_a4(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    _write_pdf(output)
    asset = _asset(tmp_path, pattern="left")

    ok, warnings = verify_pdf(
        output,
        [_placement()],
        {asset.id: asset},
        Settings(render_dpi=300),
    )

    assert ok is False
    assert "content_mismatch" in [warning.code for warning in warnings]


def test_sparse_nonempty_source_cannot_pass_against_blank_a4(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    _write_pdf(output)
    asset = _asset(tmp_path, pattern="thin")

    ok, warnings = verify_pdf(
        output,
        [_placement()],
        {asset.id: asset},
        Settings(render_dpi=300),
    )

    assert ok is False
    assert "content_mismatch" in [warning.code for warning in warnings]


def test_nonempty_source_crop_matches_its_composed_placement(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    asset = _asset(tmp_path, pattern="left")
    placement = _placement()
    compose_ticket_pages([placement], [asset], output)

    ok, warnings = verify_pdf(
        output,
        [placement],
        {asset.id: asset},
        Settings(render_dpi=300),
    )

    assert ok is True
    assert warnings == []


@pytest.mark.parametrize(
    "pattern",
    ["single-char-a", "single-digit-0", "single-amount-0"],
)
def test_matching_single_character_vector_source_still_passes(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
    pattern: str,
) -> None:
    output = tmp_path / "output.pdf"
    asset = _asset(tmp_path, pattern=pattern)
    placement = _placement()
    compose_ticket_pages([placement], [asset], output)

    ok, warnings = verify_pdf(
        output,
        [placement],
        {asset.id: asset},
        Settings(render_dpi=300),
    )

    assert ok is True
    assert warnings == []


def test_matching_raster_source_still_passes(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    asset = _raster_asset(tmp_path)
    placement = _placement()
    compose_ticket_pages([placement], [asset], output)

    ok, warnings = verify_pdf(
        output,
        [placement],
        {asset.id: asset},
        Settings(render_dpi=300),
    )

    assert ok is True
    assert warnings == []


@pytest.mark.parametrize("dpi", [200, 600])
@pytest.mark.parametrize("source_kind", ["vector", "raster"])
def test_matching_content_tolerates_supported_render_dpi_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dpi: int,
    source_kind: str,
) -> None:
    output = tmp_path / "output.pdf"
    asset = (
        _asset(tmp_path, pattern="single-digit-0")
        if source_kind == "vector"
        else _raster_asset(tmp_path)
    )
    placement = _placement()
    compose_ticket_pages([placement], [asset], output)
    monkeypatch.setattr(
        verify,
        "_render_page_dimensions",
        lambda _pdf, _page, requested_dpi: (
            round(210 / 25.4 * requested_dpi),
            round(297 / 25.4 * requested_dpi),
        ),
    )

    ok, warnings = verify_pdf(
        output,
        [placement],
        {asset.id: asset},
        Settings(render_dpi=dpi),
    )

    assert ok is True, [
        (warning.code, warning.source_page_ids, warning.output_page)
        for warning in warnings
    ]
    assert "content_mismatch" not in [warning.code for warning in warnings]


@pytest.mark.parametrize("dpi", [200, 300, 600])
@pytest.mark.parametrize("source_kind", ["vector", "raster"])
def test_partial_crop_at_fractional_placement_matches_across_render_dpi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dpi: int,
    source_kind: str,
) -> None:
    output = tmp_path / "output.pdf"
    asset = (
        _asset(tmp_path, pattern="single-digit-0")
        if source_kind == "vector"
        else _raster_asset(tmp_path)
    )
    placement = Placement(
        page_asset_id=asset.id,
        crop=CropBox(x0=0.1, y0=0.1, x1=0.9, y1=0.9),
        x_mm=19.333,
        y_mm=77.777,
        width_mm=117.53,
        height_mm=58.765,
        output_page_index=0,
    )
    compose_ticket_pages([placement], [asset], output)
    monkeypatch.setattr(
        verify,
        "_render_page_dimensions",
        lambda _pdf, _page, requested_dpi: (
            round(210 / 25.4 * requested_dpi),
            round(297 / 25.4 * requested_dpi),
        ),
    )

    ok, warnings = verify_pdf(
        output,
        [placement],
        {asset.id: asset},
        Settings(render_dpi=dpi),
    )

    assert ok is True, [
        (warning.code, warning.source_page_ids, warning.output_page)
        for warning in warnings
    ]
    assert "content_mismatch" not in [warning.code for warning in warnings]


def test_swapped_source_content_fails_each_placement(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    first = _asset(tmp_path, "page-a", pattern="left")
    second = _asset(tmp_path, "page-b", pattern="right")
    placements = [
        _placement("page-a", y_mm=20),
        _placement("page-b", y_mm=100),
    ]
    swapped = [
        second.model_copy(update={"id": "page-a"}),
        first.model_copy(update={"id": "page-b"}),
    ]
    compose_ticket_pages(placements, swapped, output)

    ok, warnings = verify_pdf(
        output,
        placements,
        {first.id: first, second.id: second},
        Settings(render_dpi=300),
    )

    assert ok is False
    assert [
        warning.source_page_ids
        for warning in warnings
        if warning.code == "content_mismatch"
    ] == [("page-a",), ("page-b",)]


def test_omitted_rendered_content_fails_the_expected_placement(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    first = _asset(tmp_path, "page-a", pattern="left")
    second = _asset(tmp_path, "page-b", pattern="right")
    blank = _asset(tmp_path, "blank")
    placements = [
        _placement("page-a", y_mm=20),
        _placement("page-b", y_mm=100),
    ]
    rendered = [first, blank.model_copy(update={"id": "page-b"})]
    compose_ticket_pages(placements, rendered, output)

    ok, warnings = verify_pdf(
        output,
        placements,
        {first.id: first, second.id: second},
        Settings(render_dpi=300),
    )

    assert ok is False
    assert [
        warning.source_page_ids
        for warning in warnings
        if warning.code == "content_mismatch"
    ] == [("page-b",)]


def test_duplicate_rendered_content_cannot_replace_another_source(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    first = _asset(tmp_path, "page-a", pattern="left")
    second = _asset(tmp_path, "page-b", pattern="right")
    placements = [
        _placement("page-a", y_mm=20),
        _placement("page-b", y_mm=100),
    ]
    duplicated = [first, first.model_copy(update={"id": "page-b"})]
    compose_ticket_pages(placements, duplicated, output)

    ok, warnings = verify_pdf(
        output,
        placements,
        {first.id: first, second.id: second},
        Settings(render_dpi=300),
    )

    assert ok is False
    assert [
        warning.source_page_ids
        for warning in warnings
        if warning.code == "content_mismatch"
    ] == [("page-b",)]


def test_extra_duplicate_content_outside_placements_fails(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    asset = _asset(tmp_path, "page-a", pattern="left")
    expected = _placement("page-a", y_mm=20)
    extra = _placement("extra", y_mm=100)
    compose_ticket_pages(
        [expected, extra],
        [asset, asset.model_copy(update={"id": "extra"})],
        output,
    )

    ok, warnings = verify_pdf(
        output,
        [expected],
        {asset.id: asset},
        Settings(render_dpi=300),
    )

    assert ok is False
    assert "unexpected_output_content" in [
        warning.code for warning in warnings
    ]


def test_wrong_text_only_source_cannot_pass_on_shared_template(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    expected = _asset(tmp_path, "page-a", pattern="text-a")
    wrong = _asset(tmp_path, "page-b", pattern="text-b")
    placement = _placement("page-a")
    compose_ticket_pages(
        [placement],
        [wrong.model_copy(update={"id": "page-a"})],
        output,
    )

    ok, warnings = verify_pdf(
        output,
        [placement],
        {expected.id: expected},
        Settings(render_dpi=300),
    )

    assert ok is False
    assert "content_mismatch" in [warning.code for warning in warnings]


@pytest.mark.parametrize(
    ("expected_pattern", "wrong_pattern"),
    [
        ("single-char-a", "single-char-b"),
        ("single-digit-0", "single-digit-1"),
        ("single-amount-0", "single-amount-1"),
    ],
)
def test_single_character_change_in_shared_template_fails(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
    expected_pattern: str,
    wrong_pattern: str,
) -> None:
    output = tmp_path / "output.pdf"
    expected = _asset(tmp_path, "page-a", pattern=expected_pattern)
    wrong = _asset(tmp_path, "page-b", pattern=wrong_pattern)
    placement = _placement("page-a")
    compose_ticket_pages(
        [placement],
        [wrong.model_copy(update={"id": "page-a"})],
        output,
    )

    ok, warnings = verify_pdf(
        output,
        [placement],
        {expected.id: expected},
        Settings(render_dpi=300),
    )

    assert ok is False
    assert "content_mismatch" in [warning.code for warning in warnings]


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        ("corrupt", "pdf_unreadable"),
        ("encrypted", "pdf_encrypted"),
        ("empty", "pdf_empty"),
        ("non_a4", "pdf_non_a4"),
        ("page_count", "pdf_page_count"),
        ("annotated", "pdf_annotations"),
    ],
)
def test_invalid_output_pdf_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    expected_code: str,
) -> None:
    output = tmp_path / "output.pdf"
    if kind == "corrupt":
        output.write_bytes(b"not-a-pdf")
    elif kind == "encrypted":
        _write_pdf(output, encrypted=True)
    elif kind == "empty":
        _write_pdf(output, ())
    elif kind == "non_a4":
        _write_pdf(output, ((612, 792),))
    elif kind == "page_count":
        _write_pdf(output, ((595.2756, 841.8898), (595.2756, 841.8898)))
    else:
        writer = PdfWriter()
        writer.add_blank_page(width=595.2756, height=841.8898)
        writer.add_annotation(
            0,
            {
                "/Type": "/Annot",
                "/Subtype": "/Text",
                "/Rect": [0, 0, 10, 10],
                "/Contents": "SYNTHETIC-PRIVATE-NOTE",
            },
        )
        with output.open("wb") as stream:
            writer.write(stream)
    monkeypatch.setattr(
        "invoice_layout.verify._render_page_dimensions",
        lambda *_args: (2480, 3508),
    )
    asset = _asset(tmp_path)

    ok, warnings = verify_pdf(
        output,
        [_placement()],
        {asset.id: asset},
        Settings(),
    )

    assert ok is False
    assert expected_code in [warning.code for warning in warnings]


@pytest.mark.parametrize(
    ("placements_factory", "assets_factory", "expected_code"),
    [
        (
            lambda: [_placement(x_mm=150, width_mm=100)],
            lambda asset: {asset.id: asset},
            "placement_clipped",
        ),
        (
            lambda: [
                _placement(),
                _placement("page-b", x_mm=30, y_mm=30),
            ],
            lambda asset: {
                asset.id: asset,
                "page-b": asset.model_copy(
                    update={"id": "page-b", "source_id": "source-page-b"}
                ),
            },
            "placement_overlap",
        ),
        (
            list,
            lambda asset: {asset.id: asset},
            "placement_missing",
        ),
        (
            lambda: [_placement(), _placement()],
            lambda asset: {asset.id: asset},
            "placement_duplicate",
        ),
        (
            lambda: [_placement(width_mm=100, height_mm=60)],
            lambda asset: {asset.id: asset},
            "placement_aspect_mismatch",
        ),
    ],
)
def test_invalid_placement_geometry_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    placements_factory: Callable[[], list[Placement]],
    assets_factory: Callable[[PageAsset], dict[str, PageAsset]],
    expected_code: str,
) -> None:
    output = tmp_path / "output.pdf"
    _write_pdf(output)
    monkeypatch.setattr(
        "invoice_layout.verify._render_page_dimensions",
        lambda *_args: (2480, 3508),
    )
    asset = _asset(tmp_path)

    ok, warnings = verify_pdf(
        output,
        placements_factory(),
        assets_factory(asset),
        Settings(),
    )

    assert ok is False
    assert expected_code in [warning.code for warning in warnings]


@pytest.mark.parametrize(
    ("render_result", "expected_code"),
    [
        (FileNotFoundError("pdftoppm"), "renderer_missing"),
        (RuntimeError("render failed"), "renderer_failed"),
        ((2400, 3400), "render_size_mismatch"),
    ],
)
def test_renderer_failure_or_wrong_pixel_dimensions_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    render_result: tuple[int, int] | Exception,
    expected_code: str,
) -> None:
    output = tmp_path / "output.pdf"
    _write_pdf(output)
    asset = _asset(tmp_path)

    def render(*_args: object) -> tuple[int, int]:
        if isinstance(render_result, Exception):
            raise render_result
        return render_result

    monkeypatch.setattr("invoice_layout.verify._render_page_dimensions", render)

    ok, warnings = verify_pdf(
        output,
        [_placement()],
        {asset.id: asset},
        Settings(render_dpi=300),
    )

    assert ok is False
    assert expected_code in [warning.code for warning in warnings]


def test_content_renderer_does_not_require_external_programs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf = tmp_path / "page.pdf"
    _write_pdf(pdf)
    monkeypatch.setenv("PATH", "")

    with verify._render_page_image(pdf, 1, 72) as image:
        dimensions = image.size

    assert dimensions == (596, 842)


def test_content_rendering_uncertainty_fails_closed(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "output.pdf"
    _write_pdf(output)
    asset = _asset(tmp_path, pattern="left")
    monkeypatch.setattr(
        verify,
        "_render_page_image",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("render failed")),
    )

    ok, warnings = verify_pdf(
        output,
        [_placement()],
        {asset.id: asset},
        Settings(),
    )

    assert ok is False
    assert "content_verification_failed" in [
        warning.code for warning in warnings
    ]


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("width_pt", 0.0),
        ("height_pt", 0.0),
        ("width_pt", -1.0),
        ("height_pt", -1.0),
        ("width_pt", float("nan")),
        ("height_pt", float("nan")),
        ("width_pt", float("inf")),
        ("height_pt", float("-inf")),
    ],
)
def test_invalid_asset_metadata_fails_closed_without_raising(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
    field: str,
    invalid_value: float,
) -> None:
    output = tmp_path / "output.pdf"
    _write_pdf(output)
    valid = _asset(tmp_path, pattern="left")
    asset = valid.model_copy(update={field: invalid_value})

    ok, warnings = verify_pdf(
        output,
        [_placement()],
        {asset.id: asset},
        Settings(),
    )

    assert ok is False
    assert "source_asset_invalid" in [warning.code for warning in warnings]


def test_low_source_resolution_warns_without_modifying_any_bytes(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    _write_pdf(output)
    asset = _asset(
        tmp_path,
        pixel_width=1200,
        pixel_height=600,
        source_pixel_width=100,
        source_pixel_height=50,
    )
    before = {
        path: path.read_bytes()
        for path in (output, asset.source_path, asset.page_pdf, asset.preview_png)
    }

    ok, warnings = verify_pdf(
        output,
        [_placement()],
        {asset.id: asset},
        Settings(render_dpi=300),
    )

    assert ok is True
    assert [warning.code for warning in warnings] == ["low_source_resolution"]
    assert all(path.read_bytes() == content for path, content in before.items())


def test_warning_order_is_stable_across_shuffled_asset_mappings(
    tmp_path: Path,
    rendered_a4: list[tuple[int, int]],
) -> None:
    output = tmp_path / "output.pdf"
    _write_pdf(output)
    first = _asset(tmp_path, "page-a", pixel_width=100, pixel_height=50)
    second = _asset(tmp_path, "page-b", pixel_width=100, pixel_height=50)
    placements = [
        _placement("page-b", x_mm=20, y_mm=100),
        _placement("page-a", x_mm=20, y_mm=20),
    ]

    one = verify_pdf(
        output,
        placements,
        {"page-b": second, "page-a": first},
        Settings(),
    )[1]
    two = verify_pdf(
        output,
        list(reversed(placements)),
        {"page-a": first, "page-b": second},
        Settings(),
    )[1]

    assert [(item.code, item.source_page_ids, item.output_page) for item in one] == [
        (item.code, item.source_page_ids, item.output_page) for item in two
    ]
    assert [item.source_page_ids for item in one] == [("page-a",), ("page-b",)]
