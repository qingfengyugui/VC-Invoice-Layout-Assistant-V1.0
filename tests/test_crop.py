from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw
from pytest import MonkeyPatch

from invoice_layout.crop import choose_crop
from invoice_layout.models import CropBox, DocumentType, Observation, PageAsset
from tests.factories import make_page_asset

FULL_PAGE = CropBox(x0=0, y0=0, x1=1, y1=1)


def _page_with_preview(tmp_path: Path, image: Image.Image) -> PageAsset:
    page = make_page_asset(tmp_path)
    preview = tmp_path / "crop-preview.png"
    image.save(preview)
    return page.model_copy(
        update={
            "preview_png": preview,
            "pixel_width": image.width,
            "pixel_height": image.height,
        }
    )


def _observation(
    *,
    document_type: DocumentType = DocumentType.UNKNOWN,
    confidence: float = 0.95,
    text_boxes: tuple[CropBox, ...] = (),
    qr_boxes: tuple[CropBox, ...] = (),
) -> Observation:
    return Observation(
        document_type=document_type,
        confidence=confidence,
        text_boxes=text_boxes,
        qr_boxes=qr_boxes,
    )


def _bordered_image(
    *,
    background: tuple[int, int, int] = (255, 255, 255),
    content: tuple[int, int, int] = (32, 64, 96),
) -> Image.Image:
    image = Image.new("RGB", (200, 160), background)
    ImageDraw.Draw(image).rectangle((40, 30, 159, 129), fill=content)
    return image


def _contains_with_padding(crop: CropBox, protected: CropBox, padding: float = 0.01) -> bool:
    return (
        crop.x0 <= protected.x0 - padding + 1e-9
        and crop.y0 <= protected.y0 - padding + 1e-9
        and crop.x1 >= protected.x1 + padding - 1e-9
        and crop.y1 >= protected.y1 + padding - 1e-9
    )


def test_clean_white_border_crops_contiguous_outer_margins(tmp_path: Path) -> None:
    page = _page_with_preview(tmp_path, _bordered_image())

    crop, warnings = choose_crop(page, _observation())

    assert 0 < crop.x0 < 0.25
    assert 0 < crop.y0 < 0.25
    assert 0.75 < crop.x1 < 1
    assert 0.75 < crop.y1 < 1
    assert warnings == []


def test_uniform_non_white_border_crops_when_separable(tmp_path: Path) -> None:
    page = _page_with_preview(
        tmp_path,
        _bordered_image(background=(24, 72, 120), content=(230, 225, 210)),
    )

    crop, warnings = choose_crop(page, _observation())

    assert crop != FULL_PAGE
    assert crop.x0 > 0
    assert crop.y0 > 0
    assert crop.x1 < 1
    assert crop.y1 < 1
    assert warnings == []


def test_content_split_by_interior_blank_band_remains_inside_crop(tmp_path: Path) -> None:
    image = Image.new("RGB", (200, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((35, 25, 164, 65), fill=(20, 40, 60))
    draw.rectangle((35, 95, 164, 134), fill=(20, 40, 60))
    page = _page_with_preview(tmp_path, image)

    crop, warnings = choose_crop(page, _observation())

    assert crop.y0 < 25 / 160
    assert crop.y1 > 134 / 160
    assert warnings == []


def test_noisy_content_like_edge_falls_back_to_full_page(tmp_path: Path) -> None:
    image = _bordered_image()
    draw = ImageDraw.Draw(image)
    for x in range(image.width):
        draw.point((x, 0), fill=(40 + x % 2 * 180, 60, 80))
    page = _page_with_preview(tmp_path, image)

    crop, warnings = choose_crop(page, _observation())

    assert crop == FULL_PAGE
    assert [warning.code for warning in warnings] == ["crop_uncertain"]


def test_gradient_edge_falls_back_to_full_page(tmp_path: Path) -> None:
    image = _bordered_image()
    draw = ImageDraw.Draw(image)
    for x in range(image.width):
        shade = round(40 + 180 * x / (image.width - 1))
        draw.point((x, 0), fill=(shade, shade, shade))
    page = _page_with_preview(tmp_path, image)

    crop, warnings = choose_crop(page, _observation())

    assert crop == FULL_PAGE
    assert [warning.code for warning in warnings] == ["crop_uncertain"]


def test_low_contrast_gradient_margin_is_not_treated_as_uniform(tmp_path: Path) -> None:
    image = _bordered_image()
    draw = ImageDraw.Draw(image)
    for y in range(30):
        shade = 255 - round(8 * y / 29)
        draw.line((0, y, image.width - 1, y), fill=(shade, shade, shade))
    page = _page_with_preview(tmp_path, image)

    crop, warnings = choose_crop(page, _observation())

    assert crop == FULL_PAGE
    assert [warning.code for warning in warnings] == ["crop_uncertain"]


def test_text_and_qr_boxes_near_every_edge_remain_padded(tmp_path: Path) -> None:
    protected = (
        CropBox(x0=0.03, y0=0.35, x1=0.08, y1=0.45),
        CropBox(x0=0.92, y0=0.35, x1=0.97, y1=0.45),
        CropBox(x0=0.35, y0=0.03, x1=0.45, y1=0.08),
        CropBox(x0=0.35, y0=0.92, x1=0.45, y1=0.97),
    )
    page = _page_with_preview(tmp_path, _bordered_image())
    observation = _observation(text_boxes=protected[:2], qr_boxes=protected[2:])

    crop, warnings = choose_crop(page, observation)

    assert warnings == []
    assert all(_contains_with_padding(crop, box) for box in protected)


def test_protected_boxes_dominate_pixel_candidate(tmp_path: Path) -> None:
    protected = CropBox(x0=0.08, y0=0.12, x1=0.18, y1=0.22)
    page = _page_with_preview(tmp_path, _bordered_image())

    crop, warnings = choose_crop(page, _observation(text_boxes=(protected,)))

    assert _contains_with_padding(crop, protected)
    assert crop.x0 <= 0.07
    assert crop.y0 <= 0.11
    assert warnings == []


def test_protected_box_too_close_to_edge_uses_full_page(tmp_path: Path) -> None:
    unsafe = CropBox(x0=0.005, y0=0.2, x1=0.1, y1=0.3)
    page = _page_with_preview(tmp_path, _bordered_image())

    crop, warnings = choose_crop(page, _observation(qr_boxes=(unsafe,)))

    assert crop == FULL_PAGE
    assert [warning.code for warning in warnings] == ["crop_protected_region_unsafe"]


def test_malformed_protected_box_uses_full_page(tmp_path: Path) -> None:
    malformed = CropBox.model_construct(x0="bad", y0=0.2, x1=0.3, y1=0.4)
    page = _page_with_preview(tmp_path, _bordered_image())
    observation = _observation().model_copy(update={"text_boxes": (malformed,)})

    crop, warnings = choose_crop(page, observation)

    assert crop == FULL_PAGE
    assert [warning.code for warning in warnings] == ["crop_protected_region_unsafe"]


def test_non_box_protected_value_uses_full_page(tmp_path: Path) -> None:
    page = _page_with_preview(tmp_path, _bordered_image())
    observation = _observation().model_copy(update={"qr_boxes": ("bad",)})

    crop, warnings = choose_crop(page, observation)

    assert crop == FULL_PAGE
    assert [warning.code for warning in warnings] == ["crop_protected_region_unsafe"]


def test_low_observation_confidence_uses_full_page(tmp_path: Path) -> None:
    page = _page_with_preview(tmp_path, _bordered_image())

    crop, warnings = choose_crop(page, _observation(confidence=0.64))

    assert crop == FULL_PAGE
    assert [warning.code for warning in warnings] == ["crop_low_observation_confidence"]


def test_physical_ticket_photo_is_never_cropped(tmp_path: Path) -> None:
    page = _page_with_preview(tmp_path, _bordered_image())

    crop, warnings = choose_crop(
        page,
        _observation(document_type=DocumentType.PHYSICAL_TICKET_PHOTO),
    )

    assert crop == FULL_PAGE
    assert [warning.code for warning in warnings] == ["crop_physical_ticket_photo"]


def test_missing_preview_uses_full_page_instead_of_crashing(tmp_path: Path) -> None:
    page = make_page_asset(tmp_path).model_copy(
        update={"preview_png": tmp_path / "missing-preview.png"}
    )

    crop, warnings = choose_crop(page, _observation())

    assert crop == FULL_PAGE
    assert [warning.code for warning in warnings] == ["crop_preview_invalid"]


def test_invalid_preview_uses_full_page_instead_of_crashing(tmp_path: Path) -> None:
    page = make_page_asset(tmp_path)
    invalid = tmp_path / "invalid-preview.png"
    invalid.write_bytes(b"not an image")
    page = page.model_copy(update={"preview_png": invalid})

    crop, warnings = choose_crop(page, _observation())

    assert crop == FULL_PAGE
    assert [warning.code for warning in warnings] == ["crop_preview_invalid"]


def test_decompression_bomb_preview_uses_full_page(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    page = make_page_asset(tmp_path)

    def raise_decompression_bomb(_path: object) -> None:
        raise Image.DecompressionBombError("preview exceeds safe pixel limit")

    monkeypatch.setattr(Image, "open", raise_decompression_bomb)

    crop, warnings = choose_crop(page, _observation())

    assert crop == FULL_PAGE
    assert [warning.code for warning in warnings] == ["crop_preview_invalid"]


def test_repeated_calls_are_deterministic(tmp_path: Path) -> None:
    page = _page_with_preview(tmp_path, _bordered_image())
    observation = _observation(
        text_boxes=(CropBox(x0=0.15, y0=0.15, x1=0.25, y1=0.25),)
    )

    first = choose_crop(page, observation)
    second = choose_crop(page, observation)

    assert first == second
