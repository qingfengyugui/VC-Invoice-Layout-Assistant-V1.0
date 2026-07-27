"""Read-only, fail-closed verification of composed printable PDFs."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from . import layout
from .config import Settings
from .models import PageAsset, Placement, WarningItem
from .pdf_render import render_pdf_page

A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
MM_PER_INCH = 25.4
POINTS_PER_INCH = 72.0
A4_WIDTH_PT = A4_WIDTH_MM / MM_PER_INCH * POINTS_PER_INCH
A4_HEIGHT_PT = A4_HEIGHT_MM / MM_PER_INCH * POINTS_PER_INCH
_PAGE_TOLERANCE_PT = 0.75
_PIXEL_TOLERANCE = 2
_GEOMETRY_EPSILON = 1e-7

_HARD_CODES = {
    "asset_duplicate",
    "asset_mapping_mismatch",
    "content_mismatch",
    "content_verification_failed",
    "pdf_empty",
    "pdf_encrypted",
    "pdf_annotations",
    "pdf_non_a4",
    "pdf_page_clipped",
    "pdf_page_count",
    "pdf_unreadable",
    "placement_aspect_mismatch",
    "placement_clipped",
    "placement_duplicate",
    "placement_invalid",
    "placement_missing",
    "placement_overlap",
    "placement_page_gap",
    "placement_unknown_asset",
    "renderer_failed",
    "renderer_missing",
    "render_size_mismatch",
    "source_asset_invalid",
    "source_resolution_unverifiable",
    "unexpected_output_content",
}


def verify_pdf(
    pdf_path: Path,
    placements: Sequence[Placement],
    assets: Mapping[str, PageAsset] | Sequence[PageAsset],
    settings: Settings,
) -> tuple[bool, list[WarningItem]]:
    """Verify an output PDF without mutating or repairing any input.

    The verifier deliberately reports renderer or structural uncertainty as a
    hard failure. Low source resolution is advisory only; content is never
    enhanced, regenerated, sharpened, or otherwise rewritten.
    """
    pdf_path = Path(pdf_path)
    warnings: list[WarningItem] = []
    asset_by_id = _index_assets(assets, warnings)
    ordered_placements = sorted(
        placements,
        key=lambda item: (
            item.output_page_index,
            item.y_mm,
            item.x_mm,
            item.page_asset_id,
        ),
    )
    expected_page_count = _verify_placements(
        ordered_placements,
        asset_by_id,
        settings,
        warnings,
    )

    page_count = _inspect_output_pdf(pdf_path, expected_page_count, warnings)
    if page_count is not None:
        _verify_rendered_pages(pdf_path, page_count, settings.render_dpi, warnings)
        _verify_placement_content(
            pdf_path,
            ordered_placements,
            asset_by_id,
            page_count,
            settings.render_dpi,
            warnings,
        )

    ordered_warnings = sorted(warnings, key=_warning_sort_key)
    return (
        not any(warning.code in _HARD_CODES for warning in ordered_warnings),
        ordered_warnings,
    )


def _index_assets(
    assets: Mapping[str, PageAsset] | Sequence[PageAsset],
    warnings: list[WarningItem],
) -> dict[str, PageAsset]:
    indexed: dict[str, PageAsset] = {}
    if isinstance(assets, Mapping):
        entries = sorted(assets.items(), key=lambda item: item[0])
        for mapping_id, asset in entries:
            if mapping_id != asset.id:
                warnings.append(
                    _warning(
                        "asset_mapping_mismatch",
                        (asset.id,),
                        None,
                        "A normalized source page does not match its asset mapping.",
                        "Rebuild the normalized asset mapping and compose again.",
                    )
                )
            if asset.id in indexed:
                warnings.append(
                    _warning(
                        "asset_duplicate",
                        (asset.id,),
                        None,
                        "A normalized source page appears more than once.",
                        "Keep exactly one immutable asset for each source page.",
                    )
                )
            else:
                indexed[asset.id] = asset
        return indexed

    for asset in sorted(assets, key=lambda item: item.id):
        if asset.id in indexed:
            warnings.append(
                _warning(
                    "asset_duplicate",
                    (asset.id,),
                    None,
                    "A normalized source page appears more than once.",
                    "Keep exactly one immutable asset for each source page.",
                )
            )
        else:
            indexed[asset.id] = asset
    return indexed


def _verify_placements(
    placements: Sequence[Placement],
    assets: Mapping[str, PageAsset],
    settings: Settings,
    warnings: list[WarningItem],
) -> int:
    seen: set[str] = set()
    rectangles: dict[int, list[tuple[str, tuple[float, float, float, float]]]] = {}

    for placement in placements:
        page_number = (
            placement.output_page_index + 1
            if placement.output_page_index >= 0
            else None
        )
        asset_id = placement.page_asset_id
        if asset_id in seen:
            warnings.append(
                _warning(
                    "placement_duplicate",
                    (asset_id,),
                    page_number,
                    "A source page is placed more than once.",
                    "Remove the duplicate placement and compose again.",
                )
            )
        seen.add(asset_id)

        asset = assets.get(asset_id)
        if asset is None:
            warnings.append(
                _warning(
                    "placement_unknown_asset",
                    (asset_id,),
                    page_number,
                    "A placement has no matching normalized source page.",
                    "Rebuild placements from the normalized asset inventory.",
                )
            )
            continue

        geometry = (
            placement.x_mm,
            placement.y_mm,
            placement.width_mm,
            placement.height_mm,
        )
        if (
            placement.output_page_index < 0
            or not all(math.isfinite(value) for value in geometry)
            or placement.x_mm < 0
            or placement.y_mm < 0
            or placement.width_mm <= 0
            or placement.height_mm <= 0
        ):
            warnings.append(
                _warning(
                    "placement_invalid",
                    (asset_id,),
                    page_number,
                    "A placement has invalid page or geometry coordinates.",
                    "Recompute this placement from immutable source geometry.",
                )
            )
            continue

        right = placement.x_mm + placement.width_mm
        bottom = placement.y_mm + placement.height_mm
        if (
            right > A4_WIDTH_MM + _GEOMETRY_EPSILON
            or bottom > A4_HEIGHT_MM + _GEOMETRY_EPSILON
        ):
            warnings.append(
                _warning(
                    "placement_clipped",
                    (asset_id,),
                    page_number,
                    "A placement extends beyond the printable A4 page.",
                    "Re-layout this source page fully inside the A4 bounds.",
                )
            )

        source_size = _inspect_source_asset(asset, warnings)
        if source_size is not None:
            source_width, source_height = source_size
            source_ratio = (
                source_width * placement.crop.width
            ) / (source_height * placement.crop.height)
            output_ratio = placement.width_mm / placement.height_mm
            if not math.isclose(
                source_ratio,
                output_ratio,
                rel_tol=_GEOMETRY_EPSILON,
                abs_tol=_GEOMETRY_EPSILON,
            ):
                warnings.append(
                    _warning(
                        "placement_aspect_mismatch",
                        (asset_id,),
                        page_number,
                        "A placement changes the normalized crop aspect ratio.",
                        "Preserve the source crop aspect ratio and compose again.",
                    )
                )
            _warn_low_resolution(placement, asset, settings, warnings)

        rectangle = (placement.x_mm, placement.y_mm, right, bottom)
        same_page = rectangles.setdefault(placement.output_page_index, [])
        for other_id, other in same_page:
            if _overlap(rectangle, other):
                warnings.append(
                    _warning(
                        "placement_overlap",
                        tuple(sorted((asset_id, other_id))),
                        page_number,
                        "Two source pages overlap on the composed A4 page.",
                        "Re-layout the source pages without overlap.",
                    )
                )
        same_page.append((asset_id, rectangle))

    for missing_id in sorted(set(assets) - seen):
        warnings.append(
            _warning(
                "placement_missing",
                (missing_id,),
                None,
                "A normalized source page is missing from the output placements.",
                "Add the omitted source page and compose again.",
            )
        )

    indexes = {item.output_page_index for item in placements if item.output_page_index >= 0}
    if indexes:
        expected_indexes = set(range(max(indexes) + 1))
        if indexes != expected_indexes:
            warnings.append(
                _warning(
                    "placement_page_gap",
                    (),
                    None,
                    "Output page indices are not contiguous from the first page.",
                    "Rebuild output pages with contiguous page indices.",
                )
            )
        return max(indexes) + 1
    return 0


def _inspect_source_asset(
    asset: PageAsset,
    warnings: list[WarningItem],
) -> tuple[float, float] | None:
    try:
        reader = PdfReader(asset.page_pdf)
        if reader.is_encrypted or len(reader.pages) != 1:
            raise ValueError("normalized source page is not a readable single-page PDF")
        page = reader.pages[0]
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        if (
            not math.isfinite(width)
            or not math.isfinite(height)
            or width <= 0
            or height <= 0
            or not math.isclose(width, asset.width_pt, abs_tol=0.01)
            or not math.isclose(height, asset.height_pt, abs_tol=0.01)
        ):
            raise ValueError("normalized source geometry does not match metadata")
        return width, height
    except (OSError, PdfReadError, ValueError, TypeError, KeyError):
        warnings.append(
            _warning(
                "source_asset_invalid",
                (asset.id,),
                None,
                "A normalized source page cannot be verified against its metadata.",
                "Normalize this source page again before composing.",
            )
        )
        return None


def _inspect_output_pdf(
    pdf_path: Path,
    expected_page_count: int,
    warnings: list[WarningItem],
) -> int | None:
    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            warnings.append(
                _warning(
                    "pdf_encrypted",
                    (),
                    None,
                    "The composed PDF is encrypted and cannot be verified.",
                    "Compose an unencrypted printable PDF.",
                )
            )
            return None
        page_count = len(reader.pages)
    except (OSError, PdfReadError, ValueError, TypeError, KeyError):
        warnings.append(
            _warning(
                "pdf_unreadable",
                (),
                None,
                "The composed PDF is missing, corrupt, or unreadable.",
                "Compose the printable PDF again from immutable inputs.",
            )
        )
        return None

    if page_count == 0:
        warnings.append(
            _warning(
                "pdf_empty",
                (),
                None,
                "The composed PDF contains no printable pages.",
                "Compose at least one verified A4 page.",
            )
        )
        return None
    if page_count != expected_page_count:
        warnings.append(
            _warning(
                "pdf_page_count",
                (),
                None,
                "The composed PDF page count does not match the placements.",
                "Compose again from the verified placement list.",
            )
        )

    for index, page in enumerate(reader.pages):
        page_number = index + 1
        if "/Annots" in page:
            warnings.append(
                _warning(
                    "pdf_annotations",
                    (),
                    page_number,
                    "An output page contains interactive PDF annotations.",
                    "Compose again after removing annotations from source pages.",
                )
            )
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        if (
            int(page.rotation) % 360 != 0
            or not math.isclose(width, A4_WIDTH_PT, abs_tol=_PAGE_TOLERANCE_PT)
            or not math.isclose(height, A4_HEIGHT_PT, abs_tol=_PAGE_TOLERANCE_PT)
        ):
            warnings.append(
                _warning(
                    "pdf_non_a4",
                    (),
                    page_number,
                    "An output page is not unrotated portrait A4.",
                    "Compose this page on an unrotated portrait A4 canvas.",
                )
            )
        crop = page.cropbox
        if (
            float(crop.left) > float(page.mediabox.left) + _PAGE_TOLERANCE_PT
            or float(crop.bottom) > float(page.mediabox.bottom) + _PAGE_TOLERANCE_PT
            or float(crop.right) < float(page.mediabox.right) - _PAGE_TOLERANCE_PT
            or float(crop.top) < float(page.mediabox.top) - _PAGE_TOLERANCE_PT
        ):
            warnings.append(
                _warning(
                    "pdf_page_clipped",
                    (),
                    page_number,
                    "An output page crop box clips the A4 canvas.",
                    "Compose again with a full-page A4 crop box.",
                )
            )
    return page_count


def _verify_rendered_pages(
    pdf_path: Path,
    page_count: int,
    dpi: int,
    warnings: list[WarningItem],
) -> None:
    expected_width = round(A4_WIDTH_MM / MM_PER_INCH * dpi)
    expected_height = round(A4_HEIGHT_MM / MM_PER_INCH * dpi)
    for page_number in range(1, page_count + 1):
        try:
            width, height = _render_page_dimensions(pdf_path, page_number, dpi)
        except FileNotFoundError:
            warnings.append(
                _warning(
                    "renderer_missing",
                    (),
                    page_number,
                    "The bundled deterministic PDF renderer is unavailable.",
                    "Reinstall the complete runtime bundle and verify again.",
                )
            )
            continue
        except (OSError, RuntimeError, ValueError):
            warnings.append(
                _warning(
                    "renderer_failed",
                    (),
                    page_number,
                    "The deterministic renderer could not render this output page.",
                    "Repair the local renderer and run verification again.",
                )
            )
            continue
        if (
            abs(width - expected_width) > _PIXEL_TOLERANCE
            or abs(height - expected_height) > _PIXEL_TOLERANCE
        ):
            warnings.append(
                _warning(
                    "render_size_mismatch",
                    (),
                    page_number,
                    "The rendered page pixel dimensions do not match A4 at the requested DPI.",
                    "Check the A4 canvas and renderer DPI, then verify again.",
                )
            )


def _render_page_dimensions(
    pdf_path: Path,
    page_number: int,
    dpi: int,
) -> tuple[int, int]:
    with render_pdf_page(pdf_path, page_index=page_number - 1, dpi=dpi) as image:
        return image.size


def _verify_placement_content(
    pdf_path: Path,
    placements: Sequence[Placement],
    assets: Mapping[str, PageAsset],
    page_count: int,
    dpi: int,
    warnings: list[WarningItem],
) -> None:
    output_pages: dict[int, Image.Image] = {}
    source_pages: dict[tuple[str, int], Image.Image] = {}
    for placement in placements:
        asset = assets.get(placement.page_asset_id)
        page_number = placement.output_page_index + 1
        if (
            asset is None
            or not _asset_metadata_is_valid(asset)
            or placement.output_page_index < 0
            or placement.output_page_index >= page_count
            or placement.x_mm < 0
            or placement.y_mm < 0
            or placement.width_mm <= 0
            or placement.height_mm <= 0
            or placement.x_mm + placement.width_mm > A4_WIDTH_MM
            or placement.y_mm + placement.height_mm > A4_HEIGHT_MM
        ):
            continue
        try:
            output_page = output_pages.get(page_number)
            if output_page is None:
                output_page = _render_page_image(pdf_path, page_number, dpi)
                output_pages[page_number] = output_page
            source_dpi = _source_render_dpi(asset, placement, dpi)
            source_key = (asset.id, source_dpi)
            source_page = source_pages.get(source_key)
            if source_page is None:
                source_page = _render_page_image(asset.page_pdf, 1, source_dpi)
                source_pages[source_key] = source_page
            source_patch = _crop_normalized(source_page, placement)
            output_patch = _crop_output(output_page, placement)
            if not _patches_match(source_patch, output_patch):
                warnings.append(
                    _warning(
                        "content_mismatch",
                        (asset.id,),
                        page_number,
                        "The rendered placement does not match its immutable source crop.",
                        "Compose this page again from the matching normalized source.",
                    )
                )
        except (OSError, RuntimeError, ValueError):
            warnings.append(
                _warning(
                    "content_verification_failed",
                    (asset.id,),
                    page_number,
                    "The placement content could not be verified safely.",
                    "Repair the local renderer and verify this placement again.",
                )
            )

    for page_number in range(1, page_count + 1):
        try:
            output_page = output_pages.get(page_number)
            if output_page is None:
                output_page = _render_page_image(pdf_path, page_number, dpi)
                output_pages[page_number] = output_page
            page_placements = [
                placement
                for placement in placements
                if placement.output_page_index + 1 == page_number
                and _placement_is_on_a4(placement)
            ]
            if _has_unexpected_content(output_page, page_placements):
                warnings.append(
                    _warning(
                        "unexpected_output_content",
                        (),
                        page_number,
                        "Rendered content exists outside all verified placements.",
                        "Remove the extra content and compose this A4 page again.",
                    )
                )
        except (OSError, RuntimeError, ValueError):
            warnings.append(
                _warning(
                    "content_verification_failed",
                    (),
                    page_number,
                    "The complete output page could not be verified safely.",
                    "Repair the local renderer and verify this page again.",
                )
            )


def _source_render_dpi(
    asset: PageAsset,
    placement: Placement,
    output_dpi: int,
) -> int:
    if not _asset_metadata_is_valid(asset):
        raise ValueError("invalid source asset geometry")
    source_width_mm = (
        asset.width_pt * placement.crop.width / POINTS_PER_INCH * MM_PER_INCH
    )
    source_height_mm = (
        asset.height_pt * placement.crop.height / POINTS_PER_INCH * MM_PER_INCH
    )
    horizontal = output_dpi * placement.width_mm / source_width_mm
    vertical = output_dpi * placement.height_mm / source_height_mm
    return max(72, min(1200, round((horizontal + vertical) / 2)))


def _asset_metadata_is_valid(asset: PageAsset) -> bool:
    return (
        math.isfinite(asset.width_pt)
        and math.isfinite(asset.height_pt)
        and asset.width_pt > 0
        and asset.height_pt > 0
    )


def _placement_is_on_a4(placement: Placement) -> bool:
    return (
        placement.output_page_index >= 0
        and placement.x_mm >= 0
        and placement.y_mm >= 0
        and placement.width_mm > 0
        and placement.height_mm > 0
        and placement.x_mm + placement.width_mm <= A4_WIDTH_MM
        and placement.y_mm + placement.height_mm <= A4_HEIGHT_MM
    )


def _has_unexpected_content(
    output_page: Image.Image,
    placements: Sequence[Placement],
) -> bool:
    target_width = min(output_page.width, 1024)
    target_height = max(
        1,
        round(target_width * output_page.height / output_page.width),
    )
    normalized = output_page.resize(
        (target_width, target_height),
        Image.Resampling.LANCZOS,
    ).filter(ImageFilter.GaussianBlur(radius=0.5))
    allowed = Image.new("L", normalized.size, 0)
    draw = ImageDraw.Draw(allowed)
    for placement in placements:
        left = round(placement.x_mm / A4_WIDTH_MM * target_width)
        top = round(placement.y_mm / A4_HEIGHT_MM * target_height)
        right = round(
            (placement.x_mm + placement.width_mm)
            / A4_WIDTH_MM
            * target_width
        )
        bottom = round(
            (placement.y_mm + placement.height_mm)
            / A4_HEIGHT_MM
            * target_height
        )
        draw.rectangle(
            (
                max(0, left - 3),
                max(0, top - 3),
                min(target_width - 1, right + 3),
                min(target_height - 1, bottom + 3),
            ),
            fill=255,
        )
    outside = Image.new("L", normalized.size, 255)
    outside.paste(normalized, mask=ImageOps.invert(allowed))
    ink_pixels = sum(outside.histogram()[:245])
    tolerance = max(16, round(target_width * target_height * 0.00002))
    return ink_pixels > tolerance


def _render_page_image(
    pdf_path: Path,
    page_number: int,
    dpi: int,
) -> Image.Image:
    with render_pdf_page(
        pdf_path,
        page_index=page_number - 1,
        dpi=dpi,
    ) as rendered:
        return rendered.convert("L")


def _crop_normalized(image: Image.Image, placement: Placement) -> Image.Image:
    box = (
        round(placement.crop.x0 * image.width),
        round(placement.crop.y0 * image.height),
        round(placement.crop.x1 * image.width),
        round(placement.crop.y1 * image.height),
    )
    return _checked_crop(image, box)


def _crop_output(image: Image.Image, placement: Placement) -> Image.Image:
    box = (
        round(placement.x_mm / A4_WIDTH_MM * image.width),
        round(placement.y_mm / A4_HEIGHT_MM * image.height),
        round(
            (placement.x_mm + placement.width_mm)
            / A4_WIDTH_MM
            * image.width
        ),
        round(
            (placement.y_mm + placement.height_mm)
            / A4_HEIGHT_MM
            * image.height
        ),
    )
    return _checked_crop(image, box)


def _checked_crop(
    image: Image.Image,
    box: tuple[int, int, int, int],
) -> Image.Image:
    left, top, right, bottom = box
    if (
        left < 0
        or top < 0
        or right > image.width
        or bottom > image.height
        or right <= left
        or bottom <= top
    ):
        raise ValueError("rendered crop is outside the page")
    return image.crop(box)


def _patches_match(source: Image.Image, output: Image.Image) -> bool:
    if source.width <= 0 or source.height <= 0 or output.width <= 0 or output.height <= 0:
        raise ValueError("empty rendered patch")
    target_width = min(output.width, 1536)
    target_height = max(1, round(target_width * output.height / output.width))
    source_normalized = source.resize(
        (target_width, target_height),
        Image.Resampling.LANCZOS,
    ).filter(ImageFilter.GaussianBlur(radius=0.5))
    output_normalized = output.resize(
        (target_width, target_height),
        Image.Resampling.LANCZOS,
    ).filter(ImageFilter.GaussianBlur(radius=0.5))

    alignment_radius = max(1, min(3, round(target_width / 512)))
    edge = alignment_radius + 1
    for vertical_shift in range(-alignment_radius, alignment_radius + 1):
        for horizontal_shift in range(-alignment_radius, alignment_radius + 1):
            aligned_source, aligned_output = _aligned_inner_patches(
                source_normalized,
                output_normalized,
                horizontal_shift,
                vertical_shift,
                edge,
            )
            if _aligned_patches_match(aligned_source, aligned_output):
                return True
    return False


def _aligned_inner_patches(
    source: Image.Image,
    output: Image.Image,
    horizontal_shift: int,
    vertical_shift: int,
    edge: int,
) -> tuple[Image.Image, Image.Image]:
    source_left = edge + max(horizontal_shift, 0)
    output_left = edge + max(-horizontal_shift, 0)
    source_top = edge + max(vertical_shift, 0)
    output_top = edge + max(-vertical_shift, 0)
    width = source.width - 2 * edge - abs(horizontal_shift)
    height = source.height - 2 * edge - abs(vertical_shift)
    if width <= 0 or height <= 0:
        raise ValueError("rendered patch is too small for alignment")
    return (
        source.crop(
            (
                source_left,
                source_top,
                source_left + width,
                source_top + height,
            )
        ),
        output.crop(
            (
                output_left,
                output_top,
                output_left + width,
                output_top + height,
            )
        ),
    )


def _aligned_patches_match(source: Image.Image, output: Image.Image) -> bool:
    difference = ImageChops.difference(source, output)
    mean_difference = ImageStat.Stat(difference).mean[0] / 255.0
    local_mean, local_strong = _max_local_difference(difference)
    source_ink = _ink_fraction(source)
    output_ink = _ink_fraction(output)
    ink_tolerance = max(0.0001, 0.25 * max(source_ink, output_ink))
    return (
        mean_difference <= 0.012
        and local_mean <= 0.20
        and local_strong <= 0.35
        and abs(source_ink - output_ink) <= ink_tolerance
    )


def _max_local_difference(difference: Image.Image) -> tuple[float, float]:
    max_mean = 0.0
    max_strong = 0.0
    tile_size = 32
    for top in range(0, difference.height, tile_size):
        for left in range(0, difference.width, tile_size):
            tile = difference.crop(
                (
                    left,
                    top,
                    min(left + tile_size, difference.width),
                    min(top + tile_size, difference.height),
                )
            )
            max_mean = max(max_mean, ImageStat.Stat(tile).mean[0] / 255.0)
            histogram = tile.histogram()
            strong_pixels = sum(histogram[32:])
            max_strong = max(
                max_strong,
                strong_pixels / (tile.width * tile.height),
            )
    return max_mean, max_strong


def _ink_fraction(image: Image.Image) -> float:
    histogram = image.histogram()
    ink_pixels = sum(histogram[:245])
    return ink_pixels / (image.width * image.height)


def _warn_low_resolution(
    placement: Placement,
    asset: PageAsset,
    settings: Settings,
    warnings: list[WarningItem],
) -> None:
    try:
        scale_limit = layout._raster_scale_limit_mm_per_pt(asset)
    except layout.LayoutError:
        warnings.append(
            _warning(
                "source_resolution_unverifiable",
                (asset.id,),
                placement.output_page_index + 1,
                "The source raster resolution cannot be verified safely.",
                "Normalize this source again or provide a verifiable original.",
            )
        )
        return
    if scale_limit is None:
        return
    actual_scale = max(
        placement.width_mm / (asset.width_pt * placement.crop.width),
        placement.height_mm / (asset.height_pt * placement.crop.height),
    )
    effective_dpi = (
        layout.MAX_RASTER_EFFECTIVE_DPI * scale_limit / actual_scale
    )
    if effective_dpi + _GEOMETRY_EPSILON < settings.render_dpi:
        warnings.append(
            _warning(
                "low_source_resolution",
                (asset.id,),
                placement.output_page_index + 1,
                "A source page may print below the requested raster resolution.",
                "Use a higher-resolution original if available; do not enhance this file.",
                severity="warning",
            )
        )


def _overlap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return (
        min(first[2], second[2]) - max(first[0], second[0]) > _GEOMETRY_EPSILON
        and min(first[3], second[3]) - max(first[1], second[1])
        > _GEOMETRY_EPSILON
    )


def _warning(
    code: str,
    source_page_ids: tuple[str, ...],
    output_page: int | None,
    message: str,
    action: str,
    *,
    severity: str = "error",
) -> WarningItem:
    return WarningItem(
        code=code,
        source_page_ids=source_page_ids,
        output_page=output_page,
        message=message,
        action=action,
        severity=severity,
    )


def _warning_sort_key(
    warning: WarningItem,
) -> tuple[int, int, tuple[str, ...], str, str]:
    severity_order = 0 if warning.code in _HARD_CODES else 1
    output_page = warning.output_page if warning.output_page is not None else 2**31
    return (
        severity_order,
        output_page,
        warning.source_page_ids,
        warning.code,
        warning.message,
    )
