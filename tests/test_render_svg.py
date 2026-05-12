"""Tests for topology SVG rendering (zoom modes and rail overlay)."""

from app import ELLIPSIS, DesignInputs, _rail_ellipsis_fan_offsets, _slots_for_zoom, design_fabric, render_svg


def test_rail_ellipsis_fan_offsets_spread() -> None:
    o3 = _rail_ellipsis_fan_offsets(3)
    assert len(o3) == 3
    assert len({round(x, 4) for x in o3}) == 3
    assert _rail_ellipsis_fan_offsets(1) == [0.0]


def test_slots_for_zoom_fit_lists_all_indices() -> None:
    slots = _slots_for_zoom(40, "fit")
    assert slots == list(range(40))
    assert ELLIPSIS not in slots


def test_slots_for_zoom_detail_may_ellipsis() -> None:
    slots = _slots_for_zoom(40, "detail")
    assert ELLIPSIS in slots


def test_render_svg_fit_uses_smaller_root_font() -> None:
    inp = DesignInputs(
        num_gpus=512,
        gpus_per_node=8,
        nics_per_gpu=1,
        spine_ports=64,
        super_spine_ports=64,
        leaf_ports=64,
        nic_speed=400,
        leaf_speed=800,
        spine_speed=800,
        super_spine_speed=0,
        plans_per_nic=0,
        rail_design=False,
    )
    result = design_fabric(inp)
    assert result.feasible
    fit_svg = render_svg(result, "fit")
    detail_svg = render_svg(result, "detail")
    assert 'font-size="8"' in fit_svg
    assert 'font-size="12"' in detail_svg
    assert len(fit_svg) > len(detail_svg)


def test_rail_design_adds_dashed_rail_group() -> None:
    inp = DesignInputs(
        num_gpus=1024,
        gpus_per_node=8,
        nics_per_gpu=1,
        spine_ports=64,
        super_spine_ports=64,
        leaf_ports=64,
        nic_speed=400,
        leaf_speed=800,
        spine_speed=800,
        super_spine_speed=1600,
        plans_per_nic=0,
        rail_design=True,
    )
    result = design_fabric(inp)
    assert result.feasible
    svg = render_svg(result, "detail")
    assert 'class="rail-links"' in svg
    i = svg.index('class="rail-links"')
    j = svg.index("</g>", i)
    block = svg[i:j]
    assert block.count("stroke-dasharray") >= 1
    assert block.count("<line") >= 8
