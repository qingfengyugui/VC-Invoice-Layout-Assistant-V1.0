"""Deterministic physical-size layout for unmodified ticket crops."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import hypot
from typing import Any

from PIL import Image, ImageOps
from pypdf import PdfReader
from pypdf.generic import ContentStream

from .config import Settings
from .models import AssociationGroup, CropBox, Observation, PageAsset, Placement

A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
POINTS_PER_INCH = 72.0
MM_PER_INCH = 25.4
MIN_TEXT_HEIGHT_MM = 2.2
MIN_QR_SIDE_MM = 25.0
MAX_RASTER_EFFECTIVE_DPI = 300.0
_EPSILON = 1e-7
_IDENTITY_MATRIX = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_MAX_FORM_DEPTH = 16
_IMAGE_SUFFIXES = {".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
_Matrix = tuple[float, float, float, float, float, float]


class LayoutError(ValueError):
    """Raised when immutable source content cannot be laid out safely."""


@dataclass(frozen=True)
class _SizeBounds:
    ratio: float
    min_width_mm: float
    min_height_mm: float
    max_width_mm: float
    max_height_mm: float


@dataclass(frozen=True)
class _LayoutItem:
    page_id: str
    crop: CropBox
    bounds: _SizeBounds


def compute_safe_size(
    asset: PageAsset,
    crop: CropBox,
    observation: Observation,
    settings: Settings,
) -> tuple[float, float]:
    """Return the smallest readable crop size in millimetres.

    Detected text and QR geometry provides the physical lower bound. When
    neither is available, the source's intrinsic physical size is retained,
    reduced only as needed to fit inside the configured A4 margins.
    """
    bounds = _size_bounds(asset, crop, observation, settings)
    return bounds.min_width_mm, bounds.min_height_mm


def pack_a4(
    groups: Sequence[AssociationGroup],
    pages: Mapping[str, PageAsset],
    crops: Mapping[str, CropBox],
    observations: Mapping[str, Observation],
    settings: Settings,
) -> list[Placement]:
    """Pack ticket crops onto portrait A4 without altering their content.

    Output page indices are zero-based. Placement coordinates are millimetres
    from the A4 sheet's left and top edges.
    """
    ordered_groups = _ordered_items(groups, pages, crops, observations, settings)
    packed_pages: list[list[_LayoutItem]] = []
    current: list[_LayoutItem] = []

    for group_items in ordered_groups:
        if _items_fit_page(group_items, settings):
            if current and not _items_fit_page([*current, *group_items], settings):
                packed_pages.append(current)
                current = []
            current.extend(group_items)
            continue

        if current:
            packed_pages.append(current)
            current = []
        remaining = list(group_items)
        while remaining:
            chunk = _largest_prefix_that_fits(remaining, settings)
            if not chunk:
                raise LayoutError(
                    f"{remaining[0].page_id}: minimum safe size does not fit portrait A4"
                )
            packed_pages.append(chunk)
            del remaining[: len(chunk)]

    if current:
        packed_pages.append(current)

    placements: list[Placement] = []
    for output_page_index, items in enumerate(packed_pages):
        placements.extend(_place_page(items, output_page_index, settings))
    return placements


def _ordered_items(
    groups: Sequence[AssociationGroup],
    pages: Mapping[str, PageAsset],
    crops: Mapping[str, CropBox],
    observations: Mapping[str, Observation],
    settings: Settings,
) -> list[list[_LayoutItem]]:
    seen: set[str] = set()
    result: list[list[_LayoutItem]] = []
    for group in groups:
        page_ids = (*group.primary_page_ids, *group.support_page_ids)
        group_items: list[_LayoutItem] = []
        for page_id in page_ids:
            if page_id in seen:
                raise LayoutError(f"{page_id}: ticket appears in more than one association group")
            try:
                asset = pages[page_id]
                crop = crops[page_id]
                observation = observations[page_id]
            except KeyError as error:
                raise LayoutError(f"{page_id}: missing {error.args[0]} layout input") from error
            seen.add(page_id)
            group_items.append(
                _LayoutItem(
                    page_id=page_id,
                    crop=crop,
                    bounds=_size_bounds(asset, crop, observation, settings),
                )
            )
        if group_items:
            result.append(group_items)
    omitted = sorted(set(pages) - seen)
    if omitted:
        raise LayoutError(
            f"{omitted[0]}: page is not present in any association group"
        )
    return result


def _size_bounds(
    asset: PageAsset,
    crop: CropBox,
    observation: Observation,
    settings: Settings,
) -> _SizeBounds:
    usable_width, usable_height = _usable_size(settings)
    cropped_width_pt = asset.width_pt * crop.width
    cropped_height_pt = asset.height_pt * crop.height
    ratio = cropped_width_pt / cropped_height_pt

    detected_boxes = (*observation.text_boxes, *observation.qr_boxes)
    for box in detected_boxes:
        _require_box_inside_crop(asset.id, crop, box)

    if detected_boxes:
        minimum_height = 0.0
        for box in observation.text_boxes:
            relative_height = box.height / crop.height
            minimum_height = max(minimum_height, MIN_TEXT_HEIGHT_MM / relative_height)
        for box in observation.qr_boxes:
            relative_width = box.width / crop.width
            relative_height = box.height / crop.height
            minimum_height = max(
                minimum_height,
                MIN_QR_SIDE_MM / relative_height,
                (MIN_QR_SIDE_MM / relative_width) / ratio,
            )
        minimum_width = minimum_height * ratio
    else:
        intrinsic_width = cropped_width_pt / POINTS_PER_INCH * MM_PER_INCH
        intrinsic_height = cropped_height_pt / POINTS_PER_INCH * MM_PER_INCH
        fit_scale = min(1.0, usable_width / intrinsic_width, usable_height / intrinsic_height)
        minimum_width = intrinsic_width * fit_scale
        minimum_height = intrinsic_height * fit_scale

    raster_scale_limit = _raster_scale_limit_mm_per_pt(asset, settings.render_dpi)
    if raster_scale_limit is None:
        maximum_height = min(usable_height, usable_width / ratio)
    else:
        maximum_height = min(
            usable_height,
            usable_width / ratio,
            cropped_height_pt * raster_scale_limit,
        )
    maximum_width = maximum_height * ratio

    if (
        minimum_width > usable_width + _EPSILON
        or minimum_height > usable_height + _EPSILON
        or minimum_width > maximum_width + _EPSILON
        or minimum_height > maximum_height + _EPSILON
    ):
        raise LayoutError(f"{asset.id}: minimum safe size does not fit portrait A4")

    return _SizeBounds(
        ratio=ratio,
        min_width_mm=minimum_width,
        min_height_mm=minimum_height,
        max_width_mm=maximum_width,
        max_height_mm=maximum_height,
    )


def _usable_size(settings: Settings) -> tuple[float, float]:
    return (
        A4_WIDTH_MM - 2 * settings.page_margin_mm,
        A4_HEIGHT_MM - 2 * settings.page_margin_mm,
    )


def _require_box_inside_crop(asset_id: str, crop: CropBox, box: CropBox) -> None:
    if (
        box.x0 < crop.x0 - _EPSILON
        or box.y0 < crop.y0 - _EPSILON
        or box.x1 > crop.x1 + _EPSILON
        or box.y1 > crop.y1 + _EPSILON
    ):
        raise LayoutError(f"{asset_id}: detected critical region falls outside the crop")


def _raster_scale_limit_mm_per_pt(
    asset: PageAsset,
    target_dpi: float = MAX_RASTER_EFFECTIVE_DPI,
) -> float | None:
    if asset.source_path.suffix.lower() in _IMAGE_SUFFIXES:
        try:
            with Image.open(asset.source_path) as image:
                oriented = ImageOps.exif_transpose(image)
                pixel_width, pixel_height = oriented.size
        except (OSError, ValueError) as error:
            raise LayoutError(f"{asset.id}: cannot inspect source image pixels") from error
        return min(
            _effective_dpi_scale_limit(pixel_width, asset.width_pt, target_dpi),
            _effective_dpi_scale_limit(pixel_height, asset.height_pt, target_dpi),
        )

    try:
        reader = PdfReader(asset.page_pdf)
        page = reader.pages[0]
        resources = page.get("/Resources")
        limits = _content_raster_scale_limits(
            page.get_contents(),
            reader,
            resources,
            _IDENTITY_MATRIX,
            set(),
            0,
            asset.id,
            target_dpi,
        )
    except LayoutError:
        raise
    except Exception as error:
        raise LayoutError(f"{asset.id}: cannot safely inspect PDF raster geometry") from error
    return min(limits) if limits else None


def _content_raster_scale_limits(
    content: Any,
    reader: PdfReader,
    resources: Any,
    initial_ctm: _Matrix,
    active_forms: set[int],
    depth: int,
    asset_id: str,
    target_dpi: float = MAX_RASTER_EFFECTIVE_DPI,
) -> list[float]:
    if depth > _MAX_FORM_DEPTH:
        raise LayoutError(f"{asset_id}: PDF form nesting is too deep to inspect safely")
    _reject_unproven_raster_resources(resources, asset_id)
    if content is None:
        return []

    stream = ContentStream(content, reader)
    current = initial_ctm
    stack: list[_Matrix] = []
    limits: list[float] = []
    for operands, operator in stream.operations:
        if operator == b"q":
            stack.append(current)
        elif operator == b"Q":
            if not stack:
                raise LayoutError(f"{asset_id}: PDF graphics state is unbalanced")
            current = stack.pop()
        elif operator == b"cm":
            if len(operands) != 6:
                raise LayoutError(f"{asset_id}: PDF transformation matrix is malformed")
            current = _multiply_matrix(current, _matrix_value(operands, asset_id))
        elif operator == b"Do":
            if len(operands) != 1:
                raise LayoutError(f"{asset_id}: PDF XObject invocation is malformed")
            item = _resolve_xobject(resources, operands[0], asset_id)
            subtype = str(item.get("/Subtype"))
            if subtype == "/Image":
                limits.append(
                    _draw_scale_limit(
                        int(item["/Width"]),
                        int(item["/Height"]),
                        current,
                        asset_id,
                        target_dpi,
                    )
                )
            elif subtype == "/Form":
                form_id = id(item)
                if form_id in active_forms:
                    raise LayoutError(f"{asset_id}: recursive PDF form cannot be inspected safely")
                form_matrix = _matrix_value(item.get("/Matrix", _IDENTITY_MATRIX), asset_id)
                form_resources = item.get("/Resources", resources)
                active_forms.add(form_id)
                try:
                    limits.extend(
                        _content_raster_scale_limits(
                            item,
                            reader,
                            form_resources,
                            _multiply_matrix(current, form_matrix),
                            active_forms,
                            depth + 1,
                            asset_id,
                            target_dpi,
                        )
                    )
                finally:
                    active_forms.remove(form_id)
            else:
                raise LayoutError(f"{asset_id}: unsupported PDF XObject cannot be inspected safely")
        elif operator == b"INLINE IMAGE":
            settings = operands.get("settings") if isinstance(operands, dict) else None
            if settings is None:
                raise LayoutError(f"{asset_id}: inline image metadata is malformed")
            width = settings.get("/W", settings.get("/Width"))
            height = settings.get("/H", settings.get("/Height"))
            if width is None or height is None:
                raise LayoutError(f"{asset_id}: inline image dimensions are unavailable")
            limits.append(
                _draw_scale_limit(
                    int(width), int(height), current, asset_id, target_dpi
                )
            )
    if stack:
        raise LayoutError(f"{asset_id}: PDF graphics state is unbalanced")
    return limits


def _reject_unproven_raster_resources(resources: Any, asset_id: str) -> None:
    if resources is None:
        return
    resource_dictionary = resources.get_object()

    ext_gstates = resource_dictionary.get("/ExtGState")
    if ext_gstates is not None:
        for reference in ext_gstates.get_object().values():
            state = reference.get_object()
            soft_mask = state.get("/SMask")
            if soft_mask is not None and str(soft_mask) != "/None":
                raise LayoutError(
                    f"{asset_id}: PDF soft mask may contain unverified raster content"
                )

    patterns = resource_dictionary.get("/Pattern")
    if patterns is not None and patterns.get_object():
        raise LayoutError(
            f"{asset_id}: PDF Pattern resource may contain unverified raster content"
        )

    fonts = resource_dictionary.get("/Font")
    if fonts is not None:
        for reference in fonts.get_object().values():
            font = reference.get_object()
            if str(font.get("/Subtype")) == "/Type3" or font.get("/CharProcs") is not None:
                raise LayoutError(
                    f"{asset_id}: PDF Type3 CharProcs may contain unverified raster content"
                )


def _resolve_xobject(resources: Any, name: Any, asset_id: str) -> Any:
    if resources is None:
        raise LayoutError(f"{asset_id}: PDF XObject resources are unavailable")
    resource_dictionary = resources.get_object()
    xobjects = resource_dictionary.get("/XObject")
    if xobjects is None:
        raise LayoutError(f"{asset_id}: PDF XObject resources are unavailable")
    reference = xobjects.get_object().get(name)
    if reference is None:
        raise LayoutError(f"{asset_id}: referenced PDF XObject is unavailable")
    return reference.get_object()


def _matrix_value(value: Any, asset_id: str) -> _Matrix:
    if len(value) != 6:
        raise LayoutError(f"{asset_id}: PDF form matrix is malformed")
    return tuple(float(component) for component in value)  # type: ignore[return-value]


def _multiply_matrix(left: _Matrix, right: _Matrix) -> _Matrix:
    la, lb, lc, ld, le, lf = left
    ra, rb, rc, rd, re, rf = right
    return (
        la * ra + lc * rb,
        lb * ra + ld * rb,
        la * rc + lc * rd,
        lb * rc + ld * rd,
        la * re + lc * rf + le,
        lb * re + ld * rf + lf,
    )


def _draw_scale_limit(
    pixel_width: int,
    pixel_height: int,
    ctm: _Matrix,
    asset_id: str,
    target_dpi: float = MAX_RASTER_EFFECTIVE_DPI,
) -> float:
    drawn_width_pt = hypot(ctm[0], ctm[1])
    drawn_height_pt = hypot(ctm[2], ctm[3])
    if pixel_width <= 0 or pixel_height <= 0 or drawn_width_pt <= 0 or drawn_height_pt <= 0:
        raise LayoutError(f"{asset_id}: raster drawing geometry is invalid")
    return min(
        _effective_dpi_scale_limit(pixel_width, drawn_width_pt, target_dpi),
        _effective_dpi_scale_limit(pixel_height, drawn_height_pt, target_dpi),
    )


def _effective_dpi_scale_limit(
    pixels: int,
    drawn_points: float,
    target_dpi: float = MAX_RASTER_EFFECTIVE_DPI,
) -> float:
    return pixels * MM_PER_INCH / (target_dpi * drawn_points)


def _items_fit_page(items: Sequence[_LayoutItem], settings: Settings) -> bool:
    _, usable_height = _usable_size(settings)
    required_height = sum(item.bounds.min_height_mm for item in items)
    required_height += settings.item_gap_mm * max(0, len(items) - 1)
    return required_height <= usable_height + _EPSILON


def _largest_prefix_that_fits(
    items: Sequence[_LayoutItem],
    settings: Settings,
) -> list[_LayoutItem]:
    candidate: list[_LayoutItem] = []
    for item in items:
        extended = [*candidate, item]
        if not _items_fit_page(extended, settings):
            break
        candidate = extended
    return candidate


def _place_page(
    items: Sequence[_LayoutItem],
    output_page_index: int,
    settings: Settings,
) -> list[Placement]:
    usable_width, usable_height = _usable_size(settings)
    gap_total = settings.item_gap_mm * max(0, len(items) - 1)
    available_item_height = usable_height - gap_total

    def dimensions(target_width: float) -> list[tuple[float, float]]:
        result: list[tuple[float, float]] = []
        for item in items:
            width = max(
                item.bounds.min_width_mm,
                min(target_width, item.bounds.max_width_mm, usable_width),
            )
            result.append((width, width / item.bounds.ratio))
        return result

    lower = 0.0
    upper = usable_width
    if sum(height for _, height in dimensions(lower)) <= available_item_height + _EPSILON:
        for _ in range(64):
            midpoint = (lower + upper) / 2
            if sum(height for _, height in dimensions(midpoint)) <= available_item_height:
                lower = midpoint
            else:
                upper = midpoint
    chosen = dimensions(lower)
    content_height = sum(height for _, height in chosen) + gap_total
    y = settings.page_margin_mm + (usable_height - content_height) / 2

    placements: list[Placement] = []
    for item, (width, height) in zip(items, chosen, strict=True):
        x = (A4_WIDTH_MM - width) / 2
        placements.append(
            Placement(
                page_asset_id=item.page_id,
                crop=item.crop,
                x_mm=x,
                y_mm=y,
                width_mm=width,
                height_mm=height,
                output_page_index=output_page_index,
            )
        )
        y += height + settings.item_gap_mm
    return placements
