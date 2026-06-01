"""Rail design affects node-to-leaf mapping and leaf-count alignment only."""

from app import DesignInputs, design_fabric, render_svg


def test_rail_minimum_leaf_count_is_gpus_per_node() -> None:
    """Rail requires at least gpus_per_node leaf switches (not a collapsed single leaf)."""
    inp = DesignInputs(
        num_gpus=64,
        gpus_per_node=8,
        nics_per_gpu=1,
        spine_ports=64,
        super_spine_ports=64,
        leaf_ports=64,
        nic_speed=400,
        leaf_speed=800,
        spine_speed=800,
        super_spine_speed=0,
        rail_design=True,
    )
    result = design_fabric(inp)
    assert result.feasible
    assert result.topology != "single-switch"
    assert result.plane.leaves_per_plane >= inp.gpus_per_node


def test_rail_rounds_leaves_to_multiple_of_gpus_per_node() -> None:
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
        rail_design=True,
    )
    result = design_fabric(inp)
    assert result.feasible
    assert result.plane.leaves_per_plane % inp.gpus_per_node == 0
    assert "node-to-leaf" in "\n".join(result.notes).lower()


def test_rail_svg_draws_node_to_leaf_rail_overlay() -> None:
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
