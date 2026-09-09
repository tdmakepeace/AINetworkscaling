"""Aggressive vs best-practice spine sizing."""

from app import DesignInputs, design_fabric


def _cluster_672_gpu(*, aggressive_design: bool = False) -> DesignInputs:
    return DesignInputs(
        num_gpus=672,
        gpus_per_node=8,
        nics_per_gpu=1,
        spine_ports=64,
        super_spine_ports=64,
        leaf_ports=64,
        nic_speed=400,
        leaf_speed=800,
        spine_speed=800,
        super_spine_speed=0,
        aggressive_design=aggressive_design,
    )


def test_best_practice_672_gpu_uses_eleven_leaves_and_eight_spines() -> None:
    # Arrange
    inp = _cluster_672_gpu(aggressive_design=False)

    # Act
    result = design_fabric(inp)

    # Assert
    assert result.feasible
    assert result.topology == "spine-leaf"
    assert result.plane.leaves_per_plane == 11
    assert result.plane.spines_per_plane == 8


def test_aggressive_672_gpu_uses_eleven_leaves_and_six_spines() -> None:
    # Arrange
    inp = _cluster_672_gpu(aggressive_design=True)

    # Act
    result = design_fabric(inp)

    # Assert
    assert result.feasible
    assert result.topology == "spine-leaf"
    assert result.plane.leaves_per_plane == 11
    assert result.plane.spines_per_plane == 6


def test_aggressive_notes_describe_minimum_device_sizing() -> None:
    # Arrange
    inp = _cluster_672_gpu(aggressive_design=True)

    # Act
    result = design_fabric(inp)

    # Assert
    notes = "\n".join(result.notes)
    assert "Aggressive sizing" in notes
    assert result.bom is not None
    assert "aggressive sizing" in result.bom.context_line
