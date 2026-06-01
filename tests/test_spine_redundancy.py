"""Spine and super-spine tiers must use at least two switches for redundancy."""

from app import MIN_TIER_SWITCHES, DesignInputs, design_fabric


def test_two_tier_never_sizes_to_one_spine_per_plan() -> None:
    """Bundling must not collapse to a single spine when a spine tier is used."""
    inp = DesignInputs(
        num_gpus=128,
        gpus_per_node=8,
        nics_per_gpu=1,
        spine_ports=64,
        super_spine_ports=64,
        leaf_ports=32,
        nic_speed=400,
        leaf_speed=800,
        spine_speed=800,
        super_spine_speed=0,
    )
    result = design_fabric(inp)
    assert result.feasible
    assert result.topology == "spine-leaf"
    assert result.plane.spines_per_plane >= MIN_TIER_SWITCHES


def test_three_tier_super_spine_count_at_least_two_per_plan() -> None:
    """When super-spine is introduced, at least two per plan."""
    inp = DesignInputs(
        num_gpus=8192,
        gpus_per_node=8,
        nics_per_gpu=1,
        spine_ports=64,
        super_spine_ports=64,
        leaf_ports=64,
        nic_speed=400,
        leaf_speed=800,
        spine_speed=800,
        super_spine_speed=1600,
    )
    result = design_fabric(inp)
    assert result.feasible
    assert result.plane.uses_super_spine
    assert result.plane.spines_per_pod >= MIN_TIER_SWITCHES
    assert result.plane.super_spines_per_plane >= MIN_TIER_SWITCHES
