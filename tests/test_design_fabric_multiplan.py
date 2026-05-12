"""Multi-plan (NIC breakout) fabric sizing: every GPU is in every plan."""

from app import DesignInputs, design_fabric


def test_multiplan_sizes_full_gpu_count_per_plan_not_partition() -> None:
    """1024 GPUs x 4 plans => 1024 uplinks per plan (2-tier), not collapsed single-switch."""
    inp = DesignInputs(
        num_gpus=1024,
        gpus_per_node=8,
        nics_per_gpu=1,
        spine_ports=64,
        super_spine_ports=64,
        leaf_ports=64,
        nic_speed=400,
        leaf_speed=400,
        spine_speed=800,
        super_spine_speed=0,
        plans_per_nic=4,
        rail_design=False,
    )
    result = design_fabric(inp)
    assert result.feasible
    assert result.plane.gpus_per_plane == 1024
    assert result.topology != "single-switch"
    text = "\n".join(result.notes)
    assert "Every GPU is present in every plan" in text
    assert "GPUs per plan (rounded up)" not in text


def test_multiplan_collapsed_when_each_plan_fits_on_one_leaf() -> None:
    inp = DesignInputs(
        num_gpus=64,
        gpus_per_node=8,
        nics_per_gpu=1,
        spine_ports=64,
        super_spine_ports=64,
        leaf_ports=64,
        nic_speed=400,
        leaf_speed=400,
        spine_speed=800,
        super_spine_speed=0,
        plans_per_nic=4,
        rail_design=False,
    )
    result = design_fabric(inp)
    assert result.feasible
    assert result.topology == "single-switch"
    assert result.plane.gpus_per_plane == 64
    assert result.num_planes == 4
    leaf_node = next(c for c in result.cables if c.end_a == "Leaf" and c.end_b == "Node")
    assert leaf_node.count == 64
