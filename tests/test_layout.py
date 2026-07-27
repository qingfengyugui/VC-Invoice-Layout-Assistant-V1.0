"""Physical-size and deterministic packing tests using synthetic geometry only."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import pairwise
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, NumberObject
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from invoice_layout.config import Settings
from invoice_layout.layout import LayoutError, compute_safe_size, pack_a4
from invoice_layout.models import (
    AssociationGroup,
    CropBox,
    DocumentType,
    Observation,
    PageAsset,
    Placement,
)

A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
FULL_CROP = CropBox(x0=0, y0=0, x1=1, y1=1)


def _page(
    directory: Path,
    page_id: str,
    *,
    ratio: float = 2.0,
    vector: bool = True,
    raster_size: tuple[int, int] = (1800, 900),
) -> PageAsset:
    width_pt = 600.0
    height_pt = width_pt / ratio
    page_pdf = directory / f"{page_id}.pdf"
    preview_png = directory / f"{page_id}.png"
    directory.mkdir(parents=True, exist_ok=True)

    canvas = Canvas(str(page_pdf), pagesize=(width_pt, height_pt), pageCompression=1)
    if vector:
        canvas.rect(12, 12, width_pt - 24, height_pt - 24, fill=0, stroke=1)
        canvas.drawString(24, height_pt / 2, "VECTOR-GEOMETRY-TEST")
    else:
        image = Image.new("RGB", raster_size, (72, 126, 181))
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
    Image.new("RGB", (600, round(600 / ratio)), "white").save(preview_png)

    return PageAsset(
        id=page_id,
        source_id=f"source-{page_id}",
        source_path=page_pdf,
        page_index=0,
        page_pdf=page_pdf,
        preview_png=preview_png,
        width_pt=width_pt,
        height_pt=height_pt,
        pixel_width=600,
        pixel_height=round(600 / ratio),
    )


def _composite_raster_page(
    directory: Path,
    page_id: str,
    *,
    vector_overlay: bool,
) -> PageAsset:
    width_pt = 600.0
    height_pt = 300.0
    page_pdf = directory / f"{page_id}.pdf"
    preview_png = directory / f"{page_id}.png"
    directory.mkdir(parents=True, exist_ok=True)

    canvas = Canvas(str(page_pdf), pagesize=(width_pt, height_pt), pageCompression=1)
    low_resolution_background = Image.new("RGB", (300, 150), (80, 110, 140))
    high_resolution_badge = Image.new("RGB", (2000, 2000), (180, 90, 40))
    canvas.drawImage(
        ImageReader(low_resolution_background),
        0,
        0,
        width=width_pt,
        height=height_pt,
    )
    canvas.drawImage(
        ImageReader(high_resolution_badge),
        525,
        225,
        width=50,
        height=50,
    )
    if vector_overlay:
        canvas.drawString(20, 20, "VECTOR-OVERLAY-TEST")
    canvas.showPage()
    canvas.save()
    Image.new("RGB", (600, 300), "white").save(preview_png)
    return PageAsset(
        id=page_id,
        source_id=f"source-{page_id}",
        source_path=page_pdf,
        page_index=0,
        page_pdf=page_pdf,
        preview_png=preview_png,
        width_pt=width_pt,
        height_pt=height_pt,
        pixel_width=600,
        pixel_height=300,
    )


def _exif_oriented_asset(
    directory: Path,
    page_id: str,
    orientation: int,
) -> PageAsset:
    asset = _page(directory, page_id, ratio=0.5)
    source_image = directory / f"{page_id}-source.jpg"
    image = Image.new("RGB", (1200, 600), (60, 120, 180))
    exif = Image.Exif()
    exif[274] = orientation
    image.save(source_image, exif=exif)
    return asset.model_copy(
        update={
            "source_path": source_image,
            "width_pt": 300.0,
            "height_pt": 600.0,
        }
    )


def _asset_with_resource_risk(
    directory: Path,
    asset: PageAsset,
    resource_name: str,
    resource_value: DictionaryObject,
) -> PageAsset:
    reader = PdfReader(asset.page_pdf)
    page = reader.pages[0]
    resources = page["/Resources"].get_object()
    resources[NameObject(resource_name)] = resource_value
    risky_pdf = directory / f"{asset.id}-{resource_name.removeprefix('/')}.pdf"
    writer = PdfWriter()
    writer.add_page(page)
    with risky_pdf.open("wb") as output:
        writer.write(output)
    return asset.model_copy(update={"source_path": risky_pdf, "page_pdf": risky_pdf})


def _observation(
    *,
    text_height: float | None = 0.04,
    qr_box: CropBox | None = None,
    document_type: DocumentType = DocumentType.UNKNOWN,
) -> Observation:
    return Observation(
        document_type=document_type,
        text="GEOMETRY-TEST",
        text_boxes=(
            (CropBox(x0=0.1, y0=0.1, x1=0.9, y1=0.1 + text_height),)
            if text_height is not None
            else ()
        ),
        qr_boxes=(qr_box,) if qr_box is not None else (),
        confidence=0.99,
        evidence=("synthetic-geometry",),
    )


def _groups(*page_ids: str) -> list[AssociationGroup]:
    return [
        AssociationGroup(
            id=f"group-{page_id}",
            primary_page_ids=(page_id,),
            support_page_ids=(),
            score=1,
        )
        for page_id in page_ids
    ]


def _pack(
    tmp_path: Path,
    page_ids: Iterable[str],
    *,
    text_height: float,
    document_types: tuple[DocumentType, ...] = (),
) -> list[Placement]:
    ids = tuple(page_ids)
    pages = {page_id: _page(tmp_path, page_id) for page_id in ids}
    observations = {
        page_id: _observation(
            text_height=text_height,
            document_type=document_types[index] if document_types else DocumentType.UNKNOWN,
        )
        for index, page_id in enumerate(ids)
    }
    crops = {page_id: FULL_CROP for page_id in ids}
    return pack_a4(_groups(*ids), pages, crops, observations, Settings())


def _page_members(placements: list[Placement]) -> list[list[str]]:
    result: list[list[str]] = []
    for page_index in range(max(item.output_page_index for item in placements) + 1):
        result.append(
            [
                item.page_asset_id
                for item in placements
                if item.output_page_index == page_index
            ]
        )
    return result


def test_density_adapts_to_each_ticket_safe_size_not_document_type(tmp_path: Path) -> None:
    types = (
        DocumentType.RAIL_TICKET,
        DocumentType.TAXI_INVOICE,
        DocumentType.FLIGHT_ITINERARY,
        DocumentType.LODGING_INVOICE,
    )

    four_up = _pack(tmp_path / "four", ("a", "b", "c", "d"), text_height=0.04, document_types=types)
    three_up = _pack(
        tmp_path / "three",
        ("a", "b", "c", "d"),
        text_height=0.0275,
        document_types=types,
    )
    two_up = _pack(
        tmp_path / "two",
        ("a", "b", "c", "d"),
        text_height=2.2 / 85,
        document_types=types,
    )

    assert [len(page) for page in _page_members(four_up)] == [4]
    assert [len(page) for page in _page_members(three_up)] == [3, 1]
    assert [len(page) for page in _page_members(two_up)] == [2, 2]


def test_five_extremely_small_readable_tickets_may_share_one_page(tmp_path: Path) -> None:
    placements = _pack(
        tmp_path,
        ("a", "b", "c", "d", "e"),
        text_height=0.1,
    )

    assert _page_members(placements) == [["a", "b", "c", "d", "e"]]


def test_large_lodging_and_flight_pages_are_one_up_when_required(tmp_path: Path) -> None:
    pages = {
        "lodging": _page(tmp_path, "lodging", ratio=0.75),
        "flight": _page(tmp_path, "flight", ratio=0.75),
    }
    observations = {
        "lodging": _observation(
            text_height=2.2 / 230,
            document_type=DocumentType.LODGING_STATEMENT,
        ),
        "flight": _observation(
            text_height=2.2 / 230,
            document_type=DocumentType.FLIGHT_ITINERARY,
        ),
    }

    placements = pack_a4(
        _groups("lodging", "flight"),
        pages,
        {"lodging": FULL_CROP, "flight": FULL_CROP},
        observations,
        Settings(),
    )

    assert _page_members(placements) == [["lodging"], ["flight"]]


def test_layout_preserves_aspect_ratio_margins_gaps_and_every_item(tmp_path: Path) -> None:
    pages = {
        "wide": _page(tmp_path, "wide", ratio=2.5),
        "medium": _page(tmp_path, "medium", ratio=1.7),
        "tall": _page(tmp_path, "tall", ratio=0.8),
    }
    observations = {page_id: _observation(text_height=0.04) for page_id in pages}
    crops = {
        "wide": FULL_CROP,
        "medium": CropBox(x0=0.1, y0=0.1, x1=0.9, y1=0.9),
        "tall": FULL_CROP,
    }
    settings = Settings(page_margin_mm=13.5, item_gap_mm=8)

    placements = pack_a4(_groups("wide", "medium", "tall"), pages, crops, observations, settings)

    assert {item.page_asset_id for item in placements} == set(pages)
    assert len(placements) == len(pages)
    assert min(item.output_page_index for item in placements) == 0
    for item in placements:
        asset = pages[item.page_asset_id]
        crop = crops[item.page_asset_id]
        expected_ratio = asset.width_pt * crop.width / (asset.height_pt * crop.height)
        assert item.width_mm / item.height_mm == pytest.approx(expected_ratio, abs=1e-9)
        assert item.x_mm == pytest.approx((A4_WIDTH_MM - item.width_mm) / 2, abs=1e-9)
        assert item.x_mm >= settings.page_margin_mm
        assert item.y_mm >= settings.page_margin_mm
        assert item.x_mm + item.width_mm <= A4_WIDTH_MM - settings.page_margin_mm + 1e-9
        assert item.y_mm + item.height_mm <= A4_HEIGHT_MM - settings.page_margin_mm + 1e-9

    for page_index in {item.output_page_index for item in placements}:
        page_items = sorted(
            (item for item in placements if item.output_page_index == page_index),
            key=lambda item: item.y_mm,
        )
        for earlier, later in pairwise(page_items):
            assert later.y_mm - (earlier.y_mm + earlier.height_mm) >= settings.item_gap_mm - 1e-9


def test_text_and_qr_regions_meet_physical_size_floors(tmp_path: Path) -> None:
    asset = _page(tmp_path, "features", ratio=1.25)
    qr = CropBox(x0=0.55, y0=0.4, x1=0.75, y1=0.65)
    observation = _observation(text_height=0.02, qr_box=qr)
    settings = Settings()

    safe_width, safe_height = compute_safe_size(asset, FULL_CROP, observation, settings)
    placements = pack_a4(
        _groups(asset.id),
        {asset.id: asset},
        {asset.id: FULL_CROP},
        {asset.id: observation},
        settings,
    )
    placement = placements[0]

    assert safe_height * observation.text_boxes[0].height >= 2.2 - 1e-9
    assert safe_width * qr.width >= 25 - 1e-9
    assert safe_height * qr.height >= 25 - 1e-9
    assert placement.height_mm * observation.text_boxes[0].height >= 2.2 - 1e-9
    assert placement.width_mm * qr.width >= 25 - 1e-9
    assert placement.height_mm * qr.height >= 25 - 1e-9


def test_raster_is_not_enlarged_past_300_effective_dpi_but_vector_can_scale(
    tmp_path: Path,
) -> None:
    raster = _page(
        tmp_path,
        "raster",
        ratio=2,
        vector=False,
        raster_size=(1800, 900),
    )
    vector = _page(tmp_path, "vector", ratio=2, vector=True)
    observations = {
        "raster": _observation(text_height=0.04),
        "vector": _observation(text_height=0.04),
    }

    raster_placement = pack_a4(
        _groups("raster"),
        {"raster": raster},
        {"raster": FULL_CROP},
        {"raster": observations["raster"]},
        Settings(),
    )[0]
    vector_placement = pack_a4(
        _groups("vector"),
        {"vector": vector},
        {"vector": FULL_CROP},
        {"vector": observations["vector"]},
        Settings(),
    )[0]

    assert raster_placement.width_mm <= 1800 / 300 * 25.4 + 1e-9
    assert 1800 / (raster_placement.width_mm / 25.4) >= 300 - 1e-9
    assert vector_placement.width_mm > raster_placement.width_mm


def test_raster_scale_cap_uses_configured_render_dpi(tmp_path: Path) -> None:
    raster = _page(
        tmp_path,
        "raster-200-dpi",
        ratio=2,
        vector=False,
        raster_size=(1800, 900),
    )
    observation = _observation(text_height=0.04)

    placement = pack_a4(
        _groups(raster.id),
        {raster.id: raster},
        {raster.id: FULL_CROP},
        {raster.id: observation},
        Settings(render_dpi=200),
    )[0]

    assert placement.width_mm == pytest.approx(183.0, abs=1e-9)
    assert placement.width_mm > 1800 / 300 * 25.4


def test_low_resolution_full_page_raster_controls_cap_not_high_resolution_badge(
    tmp_path: Path,
) -> None:
    asset = _composite_raster_page(tmp_path, "composite", vector_overlay=False)
    placement = pack_a4(
        _groups(asset.id),
        {asset.id: asset},
        {asset.id: FULL_CROP},
        {asset.id: _observation(text_height=0.2)},
        Settings(),
    )[0]

    assert placement.width_mm <= 300 / 300 * 25.4 + 1e-9


def test_vector_overlay_does_not_disable_embedded_raster_dpi_cap(tmp_path: Path) -> None:
    asset = _composite_raster_page(tmp_path, "mixed", vector_overlay=True)
    placement = pack_a4(
        _groups(asset.id),
        {asset.id: asset},
        {asset.id: FULL_CROP},
        {asset.id: _observation(text_height=0.2)},
        Settings(),
    )[0]

    assert placement.width_mm <= 300 / 300 * 25.4 + 1e-9


def test_soft_mask_extgstate_resource_is_not_treated_as_pure_vector(tmp_path: Path) -> None:
    asset = _page(tmp_path, "soft-mask", vector=True)
    ext_gstate = DictionaryObject(
        {
            NameObject("/GS1"): DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/ExtGState"),
                    NameObject("/SMask"): DictionaryObject(
                        {NameObject("/S"): NameObject("/Luminosity")}
                    ),
                }
            )
        }
    )
    asset = _asset_with_resource_risk(tmp_path, asset, "/ExtGState", ext_gstate)

    with pytest.raises(LayoutError, match=r"soft-mask.*soft mask"):
        pack_a4(
            _groups(asset.id),
            {asset.id: asset},
            {asset.id: FULL_CROP},
            {asset.id: _observation()},
            Settings(),
        )


def test_pattern_resource_is_not_treated_as_pure_vector(tmp_path: Path) -> None:
    asset = _page(tmp_path, "pattern", vector=True)
    patterns = DictionaryObject(
        {
            NameObject("/P1"): DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Pattern"),
                    NameObject("/PatternType"): NumberObject(1),
                }
            )
        }
    )
    asset = _asset_with_resource_risk(tmp_path, asset, "/Pattern", patterns)

    with pytest.raises(LayoutError, match=r"pattern.*Pattern"):
        pack_a4(
            _groups(asset.id),
            {asset.id: asset},
            {asset.id: FULL_CROP},
            {asset.id: _observation()},
            Settings(),
        )


def test_type3_charprocs_resource_is_not_treated_as_pure_vector(tmp_path: Path) -> None:
    asset = _page(tmp_path, "type3", vector=True)
    fonts = DictionaryObject(
        {
            NameObject("/FType3"): DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type3"),
                    NameObject("/CharProcs"): DictionaryObject(),
                }
            )
        }
    )
    asset = _asset_with_resource_risk(tmp_path, asset, "/Font", fonts)

    with pytest.raises(LayoutError, match=r"type3.*Type3"):
        pack_a4(
            _groups(asset.id),
            {asset.id: asset},
            {asset.id: FULL_CROP},
            {asset.id: _observation()},
            Settings(),
        )


def test_plain_vector_resource_page_still_scales_without_raster_cap(tmp_path: Path) -> None:
    asset = _page(tmp_path, "plain-vector", vector=True)
    placement = pack_a4(
        _groups(asset.id),
        {asset.id: asset},
        {asset.id: FULL_CROP},
        {asset.id: _observation()},
        Settings(),
    )[0]

    assert placement.width_mm == pytest.approx(183.0)


@pytest.mark.parametrize("orientation", [6, 8])
def test_original_image_raster_cap_uses_exif_oriented_pixel_axes(
    tmp_path: Path,
    orientation: int,
) -> None:
    asset = _exif_oriented_asset(tmp_path, f"orientation-{orientation}", orientation)
    placement = pack_a4(
        _groups(asset.id),
        {asset.id: asset},
        {asset.id: FULL_CROP},
        {asset.id: _observation(text_height=0.04)},
        Settings(),
    )[0]

    assert placement.width_mm == pytest.approx(600 / 300 * 25.4, abs=1e-9)
    assert placement.height_mm == pytest.approx(1200 / 300 * 25.4, abs=1e-9)


def test_primary_precedes_support_and_fittable_group_moves_together(tmp_path: Path) -> None:
    ids = ("previous", "primary", "support")
    pages = {page_id: _page(tmp_path, page_id, ratio=1) for page_id in ids}
    observations = {
        "previous": _observation(text_height=2.2 / 150),
        "primary": _observation(text_height=2.2 / 100),
        "support": _observation(text_height=2.2 / 100),
    }
    groups = [
        AssociationGroup(
            id="previous-group",
            primary_page_ids=("previous",),
            support_page_ids=(),
            score=1,
        ),
        AssociationGroup(
            id="associated-group",
            primary_page_ids=("primary",),
            support_page_ids=("support",),
            score=1,
        ),
    ]

    placements = pack_a4(
        groups,
        pages,
        {page_id: FULL_CROP for page_id in ids},
        observations,
        Settings(),
    )

    assert [item.page_asset_id for item in placements] == ["previous", "primary", "support"]
    assert placements[0].output_page_index == 0
    assert placements[1].output_page_index == placements[2].output_page_index == 1


def test_physically_oversized_group_splits_only_across_consecutive_pages(
    tmp_path: Path,
) -> None:
    ids = ("p1", "p2", "s1", "s2", "s3", "next")
    pages = {page_id: _page(tmp_path, page_id, ratio=1) for page_id in ids}
    observations = {
        page_id: _observation(text_height=2.2 / 80)
        for page_id in ids
    }
    groups = [
        AssociationGroup(
            id="large-group",
            primary_page_ids=("p1", "p2"),
            support_page_ids=("s1", "s2", "s3"),
            score=1,
        ),
        AssociationGroup(
            id="next-group",
            primary_page_ids=("next",),
            support_page_ids=(),
            score=1,
        ),
    ]

    placements = pack_a4(
        groups,
        pages,
        {page_id: FULL_CROP for page_id in ids},
        observations,
        Settings(),
    )

    assert [item.page_asset_id for item in placements] == [*ids]
    group_pages = [item.output_page_index for item in placements[:5]]
    assert group_pages == sorted(group_pages)
    assert set(group_pages) == set(range(min(group_pages), max(group_pages) + 1))
    assert placements[0].output_page_index == 0
    assert placements[4].output_page_index == 1


def test_unreadable_ticket_raises_explicit_deterministic_error(tmp_path: Path) -> None:
    asset = _page(tmp_path, "unreadable", ratio=2)
    observation = _observation(text_height=0.005)

    with pytest.raises(LayoutError, match=r"unreadable.*minimum safe size"):
        pack_a4(
            _groups(asset.id),
            {asset.id: asset},
            {asset.id: FULL_CROP},
            {asset.id: observation},
            Settings(),
        )


def test_page_not_present_in_any_group_is_rejected_instead_of_omitted(tmp_path: Path) -> None:
    pages = {
        "included": _page(tmp_path, "included"),
        "forgotten": _page(tmp_path, "forgotten"),
    }
    observations = {page_id: _observation() for page_id in pages}
    crops = {page_id: FULL_CROP for page_id in pages}

    with pytest.raises(LayoutError, match=r"forgotten.*association group"):
        pack_a4(
            _groups("included"),
            pages,
            crops,
            observations,
            Settings(),
        )


def test_shuffled_mappings_serialize_to_identical_placements(tmp_path: Path) -> None:
    ids = ("a", "b", "c")
    pages = {page_id: _page(tmp_path, page_id, ratio=1.5 + index * 0.25) for index, page_id in enumerate(ids)}
    observations = {page_id: _observation(text_height=0.035) for page_id in ids}
    crops = {page_id: FULL_CROP for page_id in ids}
    groups = _groups(*ids)

    first = pack_a4(groups, pages, crops, observations, Settings())
    second = pack_a4(
        groups,
        dict(reversed(tuple(pages.items()))),
        dict(reversed(tuple(crops.items()))),
        dict(reversed(tuple(observations.items()))),
        Settings(),
    )

    assert [item.model_dump_json() for item in first] == [
        item.model_dump_json() for item in second
    ]
