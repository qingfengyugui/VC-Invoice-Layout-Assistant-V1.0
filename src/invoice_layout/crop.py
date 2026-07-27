"""Conservative crop selection based only on normalized preview geometry."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Sequence
from typing import cast

from PIL import Image, UnidentifiedImageError

from .models import CropBox, DocumentType, Observation, PageAsset, WarningItem

_FULL_PAGE = CropBox(x0=0, y0=0, x1=1, y1=1)
_MIN_OBSERVATION_CONFIDENCE = 0.65
_MIN_EDGE_CONFIDENCE = 0.98
_PROTECTED_PADDING = 0.01
_BACKGROUND_DISTANCE = 10
_MAX_EDGE_DEVIATION = 4.0
_MAX_EDGE_COLOR_RANGE = 4


def _warning(page_id: str, code: str) -> WarningItem:
    messages = {
        "crop_low_observation_confidence": "Automatic cropping was skipped because page analysis confidence is low.",
        "crop_physical_ticket_photo": "Automatic cropping was skipped for a physical ticket photo.",
        "crop_preview_invalid": "Automatic cropping was skipped because the page preview could not be read.",
        "crop_protected_region_unsafe": "Automatic cropping was skipped because a protected region is too close to a page edge.",
        "crop_uncertain": "Automatic cropping was skipped because the outer margins are not confidently uniform.",
    }
    actions = {
        "crop_low_observation_confidence": "Review the page classification before printing.",
        "crop_physical_ticket_photo": "Keep the full photo and review its print size.",
        "crop_preview_invalid": "Regenerate the page preview and retry.",
        "crop_protected_region_unsafe": "Review the protected text and code regions before printing.",
        "crop_uncertain": "Review the full page and crop it manually only if the outer area is disposable.",
    }
    return WarningItem(
        code=code,
        source_page_ids=(page_id,),
        output_page=None,
        message=messages[code],
        action=actions[code],
        severity="warning",
    )


def _fallback(page: PageAsset, code: str) -> tuple[CropBox, list[WarningItem]]:
    return _FULL_PAGE, [_warning(page.id, code)]


def _valid_protected_boxes(boxes: Sequence[object]) -> bool:
    for box in boxes:
        if not isinstance(box, CropBox):
            return False
        coordinates: tuple[object, ...] = (box.x0, box.y0, box.x1, box.y1)
        if not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            for value in coordinates
        ):
            return False
        if not (0 <= box.x0 < box.x1 <= 1 and 0 <= box.y0 < box.y1 <= 1):
            return False
        if (
            box.x0 <= _PROTECTED_PADDING
            or box.y0 <= _PROTECTED_PADDING
            or box.x1 >= 1 - _PROTECTED_PADDING
            or box.y1 >= 1 - _PROTECTED_PADDING
        ):
            return False
    return True


def _median_color(pixels: Sequence[tuple[int, int, int]]) -> tuple[int, int, int]:
    channels = tuple(
        int(statistics.median(pixel[channel] for pixel in pixels)) for channel in range(3)
    )
    return channels  # type: ignore[return-value]


def _color_distance(first: tuple[int, int, int], second: tuple[int, int, int]) -> int:
    return max(abs(first[channel] - second[channel]) for channel in range(3))


def _deviation(pixels: Iterable[tuple[int, int, int]]) -> float:
    materialized = tuple(pixels)
    if not materialized:
        return math.inf
    return max(
        statistics.pstdev(pixel[channel] for pixel in materialized) for channel in range(3)
    )


def _color_range(pixels: Sequence[tuple[int, int, int]]) -> int:
    if not pixels:
        return _BACKGROUND_DISTANCE + 1
    return max(
        max(pixel[channel] for pixel in pixels) - min(pixel[channel] for pixel in pixels)
        for channel in range(3)
    )


def _region_is_confident(
    pixels: Sequence[tuple[int, int, int]],
    background: tuple[int, int, int],
) -> bool:
    if not pixels:
        return False
    matching = sum(
        _color_distance(pixel, background) <= _BACKGROUND_DISTANCE for pixel in pixels
    )
    return (
        matching / len(pixels) >= _MIN_EDGE_CONFIDENCE
        and _deviation(pixels) <= _MAX_EDGE_DEVIATION
        and _color_range(pixels) <= _MAX_EDGE_COLOR_RANGE
    )


def _corner_pixels(
    pixels: Sequence[tuple[int, int, int]],
    width: int,
    height: int,
) -> tuple[tuple[int, int, int], ...]:
    patch = max(1, min(5, width // 50, height // 50))
    result: list[tuple[int, int, int]] = []
    for y_start in (0, height - patch):
        for x_start in (0, width - patch):
            for y in range(y_start, y_start + patch):
                row = y * width
                result.extend(pixels[row + x_start : row + x_start + patch])
    return tuple(result)


def _outer_edge_pixels(
    pixels: Sequence[tuple[int, int, int]],
    width: int,
    height: int,
) -> tuple[tuple[tuple[int, int, int], ...], ...]:
    top = tuple(pixels[:width])
    bottom = tuple(pixels[(height - 1) * width : height * width])
    left = tuple(pixels[y * width] for y in range(height))
    right = tuple(pixels[y * width + width - 1] for y in range(height))
    return top, right, bottom, left


def _background_is_confident(
    corners: Sequence[tuple[int, int, int]],
    edges: Sequence[Sequence[tuple[int, int, int]]],
    background: tuple[int, int, int],
) -> bool:
    if not _region_is_confident(corners, background):
        return False
    return all(_region_is_confident(edge, background) for edge in edges)


def _content_bounds(
    pixels: Sequence[tuple[int, int, int]],
    width: int,
    height: int,
    background: tuple[int, int, int],
) -> tuple[int, int, int, int] | None:
    left = width
    top = height
    right = -1
    bottom = -1
    for index, pixel in enumerate(pixels):
        if _color_distance(pixel, background) <= _BACKGROUND_DISTANCE:
            continue
        x = index % width
        y = index // width
        left = min(left, x)
        top = min(top, y)
        right = max(right, x)
        bottom = max(bottom, y)
    if right < left or bottom < top:
        return None
    return left, top, right, bottom


def _pixel_crop(
    bounds: tuple[int, int, int, int],
    width: int,
    height: int,
) -> CropBox | None:
    left, top, right, bottom = bounds
    if left <= 0 or top <= 0 or right >= width - 1 or bottom >= height - 1:
        return None
    x0 = max(0, left - 1) / width
    y0 = max(0, top - 1) / height
    x1 = min(width, right + 2) / width
    y1 = min(height, bottom + 2) / height
    if x0 <= 0 or y0 <= 0 or x1 >= 1 or y1 >= 1:
        return None
    return CropBox(
        x0=round(x0, 10),
        y0=round(y0, 10),
        x1=round(x1, 10),
        y1=round(y1, 10),
    )


def _include_protected(candidate: CropBox, boxes: Sequence[CropBox]) -> CropBox:
    if not boxes:
        return candidate
    return CropBox(
        x0=round(min(candidate.x0, *(box.x0 - _PROTECTED_PADDING for box in boxes)), 10),
        y0=round(min(candidate.y0, *(box.y0 - _PROTECTED_PADDING for box in boxes)), 10),
        x1=round(max(candidate.x1, *(box.x1 + _PROTECTED_PADDING for box in boxes)), 10),
        y1=round(max(candidate.y1, *(box.y1 + _PROTECTED_PADDING for box in boxes)), 10),
    )


def _removed_margins_are_confident(
    pixels: Sequence[tuple[int, int, int]],
    width: int,
    height: int,
    candidate: CropBox,
    background: tuple[int, int, int],
) -> bool:
    left_end = math.floor(candidate.x0 * width + 1e-9)
    right_start = math.ceil(candidate.x1 * width - 1e-9)
    top_end = math.floor(candidate.y0 * height + 1e-9)
    bottom_start = math.ceil(candidate.y1 * height - 1e-9)
    if left_end <= 0 or right_start >= width or top_end <= 0 or bottom_start >= height:
        return False

    left = tuple(
        pixels[y * width + x] for y in range(height) for x in range(left_end)
    )
    right = tuple(
        pixels[y * width + x]
        for y in range(height)
        for x in range(right_start, width)
    )
    top = tuple(pixels[y * width + x] for y in range(top_end) for x in range(width))
    bottom = tuple(
        pixels[y * width + x]
        for y in range(bottom_start, height)
        for x in range(width)
    )
    return all(
        _region_is_confident(region, background) for region in (left, right, top, bottom)
    )


def choose_crop(
    page: PageAsset,
    observation: Observation,
) -> tuple[CropBox, list[WarningItem]]:
    """Choose a normalized safe crop without modifying any source page pixels."""
    if observation.document_type == DocumentType.PHYSICAL_TICKET_PHOTO:
        return _fallback(page, "crop_physical_ticket_photo")
    if observation.confidence < _MIN_OBSERVATION_CONFIDENCE:
        return _fallback(page, "crop_low_observation_confidence")

    protected = (*observation.text_boxes, *observation.qr_boxes)
    if not _valid_protected_boxes(protected):
        return _fallback(page, "crop_protected_region_unsafe")

    try:
        with Image.open(page.preview_png) as preview:
            preview.load()
            image = preview.convert("RGB")
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError):
        return _fallback(page, "crop_preview_invalid")

    width, height = image.size
    if width < 3 or height < 3:
        return _fallback(page, "crop_preview_invalid")

    pixels = cast(tuple[tuple[int, int, int], ...], tuple(image.getdata()))
    corners = _corner_pixels(pixels, width, height)
    background = _median_color(corners)
    edges = _outer_edge_pixels(pixels, width, height)
    if not _background_is_confident(corners, edges, background):
        return _fallback(page, "crop_uncertain")

    content_bounds = _content_bounds(pixels, width, height, background)
    if content_bounds is None:
        return _fallback(page, "crop_uncertain")
    candidate = _pixel_crop(content_bounds, width, height)
    if candidate is None:
        return _fallback(page, "crop_uncertain")

    candidate = _include_protected(candidate, protected)
    if not _removed_margins_are_confident(pixels, width, height, candidate, background):
        return _fallback(page, "crop_uncertain")
    return candidate, []
