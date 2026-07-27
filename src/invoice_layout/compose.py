"""Annotation-free A4 composition from immutable normalized ticket pages."""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject

from .models import PageAsset, Placement

A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
MM_TO_PT = 72.0 / 25.4
A4_WIDTH_PT = A4_WIDTH_MM * MM_TO_PT
A4_HEIGHT_PT = A4_HEIGHT_MM * MM_TO_PT
_EPSILON = 1e-7
_GEOMETRY_TOLERANCE_PT = 1e-4


class CompositionError(ValueError):
    """Raised when placements cannot be composed without altering content."""


def compose_ticket_pages(
    placements: Sequence[Placement],
    assets: Sequence[PageAsset],
    output_path: Path,
) -> Path:
    """Compose normalized one-page PDFs onto pure portrait A4 sheets.

    Placement coordinates use millimetres from the sheet's top-left corner.
    The output is replaced atomically only after every placement validates and
    a complete PDF has been written.
    """
    output_path = Path(output_path)
    asset_by_id = _asset_index(assets)
    _validate_output_is_not_source(output_path, assets)
    ordered = _validate_placements(placements, asset_by_id)

    writer = PdfWriter()
    if ordered:
        page_count = ordered[-1].output_page_index + 1
        output_pages = [
            PageObject.create_blank_page(width=A4_WIDTH_PT, height=A4_HEIGHT_PT)
            for _ in range(page_count)
        ]
        for placement in ordered:
            source_page = _fresh_source_page(asset_by_id[placement.page_asset_id])
            source_page.pop("/Annots", None)
            source_page.pop("/AA", None)
            crop_left, crop_bottom, crop_right, crop_top = _source_crop(
                source_page, placement
            )
            source_page.cropbox = RectangleObject(
                (crop_left, crop_bottom, crop_right, crop_top)
            )

            scale = placement.width_mm * MM_TO_PT / (crop_right - crop_left)
            destination_x = placement.x_mm * MM_TO_PT
            destination_bottom = (
                A4_HEIGHT_MM - placement.y_mm - placement.height_mm
            ) * MM_TO_PT
            transform = Transformation(
                (
                    scale,
                    0.0,
                    0.0,
                    scale,
                    destination_x - scale * crop_left,
                    destination_bottom - scale * crop_bottom,
                )
            )
            output_pages[placement.output_page_index].merge_transformed_page(
                source_page,
                transform,
                expand=False,
            )
        for page in output_pages:
            page.pop("/Annots", None)
            page.pop("/AA", None)
            writer.add_page(page)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with temporary_path.open("wb") as output:
            writer.write(output)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return output_path


def _asset_index(assets: Sequence[PageAsset]) -> dict[str, PageAsset]:
    result: dict[str, PageAsset] = {}
    for asset in assets:
        if asset.id in result:
            raise CompositionError(f"duplicate asset id: {asset.id}")
        result[asset.id] = asset
    return result


def _validate_output_is_not_source(
    output_path: Path,
    assets: Sequence[PageAsset],
) -> None:
    for asset in assets:
        for source_path in (asset.page_pdf, asset.source_path):
            if _paths_alias(output_path, source_path):
                raise CompositionError(
                    f"output path aliases source file for {asset.id}: {source_path}"
                )


def _paths_alias(first: Path, second: Path) -> bool:
    try:
        if first.resolve(strict=False) == second.resolve(strict=False):
            return True
    except (OSError, RuntimeError):
        pass
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


def _validate_placements(
    placements: Sequence[Placement],
    assets: dict[str, PageAsset],
) -> list[Placement]:
    seen_assets: set[str] = set()
    page_rectangles: dict[int, list[tuple[float, float, float, float]]] = {}

    for placement in placements:
        if placement.page_asset_id in seen_assets:
            raise CompositionError(
                f"duplicate page placement: {placement.page_asset_id}"
            )
        seen_assets.add(placement.page_asset_id)
        if placement.page_asset_id not in assets:
            raise CompositionError(f"missing asset: {placement.page_asset_id}")
        if placement.output_page_index < 0:
            raise CompositionError("page index must be non-negative")

        geometry = (
            placement.x_mm,
            placement.y_mm,
            placement.width_mm,
            placement.height_mm,
        )
        if not all(math.isfinite(value) for value in geometry):
            raise CompositionError("geometry values must be finite")
        if (
            placement.x_mm < 0
            or placement.y_mm < 0
            or placement.width_mm <= 0
            or placement.height_mm <= 0
        ):
            raise CompositionError("geometry must have a non-negative origin and positive size")
        if (
            placement.x_mm + placement.width_mm > A4_WIDTH_MM + _EPSILON
            or placement.y_mm + placement.height_mm > A4_HEIGHT_MM + _EPSILON
        ):
            raise CompositionError("placement exceeds A4 bounds")

        asset = assets[placement.page_asset_id]
        actual_width, actual_height = _actual_page_dimensions(asset)
        crop_width = actual_width * placement.crop.width
        crop_height = actual_height * placement.crop.height
        source_ratio = crop_width / crop_height
        output_ratio = placement.width_mm / placement.height_mm
        if not math.isclose(source_ratio, output_ratio, rel_tol=_EPSILON, abs_tol=_EPSILON):
            raise CompositionError(
                f"aspect ratio mismatch for {placement.page_asset_id}"
            )

        rectangle = (
            placement.x_mm,
            placement.y_mm,
            placement.x_mm + placement.width_mm,
            placement.y_mm + placement.height_mm,
        )
        existing = page_rectangles.setdefault(placement.output_page_index, [])
        if any(_rectangles_overlap(rectangle, other) for other in existing):
            raise CompositionError(
                f"overlap on output page {placement.output_page_index}"
            )
        existing.append(rectangle)

    indexes = set(page_rectangles)
    if indexes:
        expected = set(range(max(indexes) + 1))
        if indexes != expected:
            raise CompositionError("output page indexes must be contiguous from zero")
    return sorted(
        placements,
        key=lambda item: (
            item.output_page_index,
            item.y_mm,
            item.x_mm,
            item.page_asset_id,
        ),
    )


def _actual_page_dimensions(asset: PageAsset) -> tuple[float, float]:
    page = _fresh_source_page(asset)
    width = float(page.mediabox.width)
    height = float(page.mediabox.height)
    if (
        not math.isfinite(width)
        or not math.isfinite(height)
        or width <= 0
        or height <= 0
    ):
        raise CompositionError(f"invalid source PDF geometry for {asset.id}")
    if not math.isclose(
        width,
        asset.width_pt,
        rel_tol=_EPSILON,
        abs_tol=_GEOMETRY_TOLERANCE_PT,
    ) or not math.isclose(
        height,
        asset.height_pt,
        rel_tol=_EPSILON,
        abs_tol=_GEOMETRY_TOLERANCE_PT,
    ):
        raise CompositionError(
            f"source PDF geometry does not match asset metadata for {asset.id}"
        )
    return width, height


def _rectangles_overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return (
        min(first[2], second[2]) - max(first[0], second[0]) > _EPSILON
        and min(first[3], second[3]) - max(first[1], second[1]) > _EPSILON
    )


def _fresh_source_page(asset: PageAsset) -> PageObject:
    reader = PdfReader(asset.page_pdf)
    if reader.is_encrypted:
        raise CompositionError(f"source PDF is encrypted: {asset.id}")
    if len(reader.pages) != 1:
        raise CompositionError(f"source PDF must contain exactly one page: {asset.id}")
    page = reader.pages[0]
    if int(page.rotation) % 360:
        page.transfer_rotation_to_content()
    return page


def _source_crop(
    page: PageObject,
    placement: Placement,
) -> tuple[float, float, float, float]:
    media = page.mediabox
    width = float(media.width)
    height = float(media.height)
    left = float(media.left) + placement.crop.x0 * width
    right = float(media.left) + placement.crop.x1 * width
    bottom = float(media.bottom) + (1 - placement.crop.y1) * height
    top = float(media.bottom) + (1 - placement.crop.y0) * height
    return left, bottom, right, top
