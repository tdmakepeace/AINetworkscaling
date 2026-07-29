"""Flask app for AI network spine-and-leaf (+ optional super-spine) design.

Computes a non-blocking (1:1 subscription) fabric sized for a given GPU
count, organised into nodes. Three port speeds are selectable
independently - NIC, leaf, spine - and an optional super-spine tier at
800G or 1600G is introduced only when a 2-tier spine-leaf cannot fan
out wide enough.

Where a switch port runs faster than what it connects to, breakout
(splitter) cables are assumed.

All speeds must be integer multiples of 400G, which is true for the
allowed values (400 / 800 / 1600).
"""

from __future__ import annotations
from pickle import FALSE
import webview
import threading
import time
import urllib.request
import math
from dataclasses import dataclass, field
from typing import Literal, Optional

DiagramZoomMode = Literal["fit", "detail"]

from flask import Flask, render_template, request

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Design logic
# ---------------------------------------------------------------------------


@dataclass
class DesignInputs:
    num_gpus: int
    gpus_per_node: int
    nics_per_gpu: int  # 1, 2, 3 -> NICs per GPU
    spine_ports: int
    super_spine_ports: int
    leaf_ports: int
    nic_speed: int  # 400 or 800
    leaf_speed: int  # 400, 800 or 1600
    spine_speed: int  # 400, 800 or 1600
    super_spine_speed: int = 0  # 0 = not used, else 800 or 1600
    plans_per_nic: int = 0  # 0, 1, 2, or 4; 0 = all NICs in one plan
    rail_design: bool = False
    # Cable ratio controlling how many leaf ports are allocated down to nodes
    # vs up to spines. Format: "<down>:<up>" where values are positive.
    # Example: "1:1.16" means up uses ~1.16x the ports of down.
    leaf_spine_ratio: str = "1:1"
    # Cable ratio controlling how many spine ports are allocated down to leaves
    # vs up to super-spines. Format: "<down>:<up>".
    spine_super_ratio: str = "1:1"
    # If enabled, fabric interfaces (leaf<->spine and spine<->super-spine)
    # use NIC-speed breakout lanes even when both switch ports are faster.
    match_interface_speed_to_nic: bool = False


@dataclass
class PlaneDesign:
    # Speeds and breakouts
    nic_speed: int  # Effective NIC link speed per plan
    nic_speed_raw: int  # Physical NIC port speed before plans_per_nic split
    leaf_speed: int
    spine_speed: int
    super_spine_speed: int
    leaf_breakout: int
    leaf_to_spine_fanout: int
    spine_to_leaf_fanout: int
    spine_to_super_fanout: int
    super_to_spine_fanout: int

    # Leaf characterisation
    downlink_ports_per_leaf: int
    uplink_ports_per_leaf: int
    gpus_per_leaf: int
    # Logical GPU uplinks sized per plane (multi-plan: every GPU is in every plan).
    gpus_per_plane: int
    leaves_per_plane: int

    # 2-tier specifics (also used inside each pod in 3-tier)
    spines_per_plane: int  # total spines in the plane
    links_per_leaf_to_each_spine: int
    spine_ports_used_for_leaves: int  # per spine

    # 3-tier specifics
    uses_super_spine: bool = False
    leaves_per_pod: int = 0
    spines_per_pod: int = 0
    pods_per_plane: int = 0
    spine_ports_up_to_super: int = 0  # per spine
    super_spines_per_plane: int = 0
    ports_used_per_super_spine: int = 0

    oversubscription: str = "1:1"


@dataclass
class CableGroup:
    count: int  # total physical cables (summed across planes)
    label: str  # e.g. "800G-2x400G" or "800G-800G"
    end_a: str  # e.g. "Leaf"
    end_b: str  # e.g. "Node"


@dataclass
class DesignResult:
    inputs: DesignInputs
    num_planes: int
    plane: PlaneDesign
    total_leaves: int
    total_spines: int
    total_super_spines: int
    total_nodes: int
    cables: list[CableGroup] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    feasible: bool = True
    topology: str = "spine-leaf"  # "single-switch", "spine-leaf", "3-tier"
    bom: Optional["BillOfMaterials"] = None


@dataclass
class BOMConnectionDetail:
    """Optics assemblies (physical) vs logical cable/link count for one hop."""

    optics_count: int = 0
    optics_label: str = ""
    cables_count: int = 0


@dataclass
class BOMLayer:
    """One fabric layer: switches plus south/north interface breakdown."""

    title: str
    switch_quantity: int
    switch_specification: str
    south_to: str = ""
    north_to: str = ""
    south: BOMConnectionDetail = field(default_factory=BOMConnectionDetail)
    north: BOMConnectionDetail = field(default_factory=BOMConnectionDetail)
    layer_note: str = ""


@dataclass
class BillOfMaterials:
    context_line: str
    super_spine: BOMLayer
    spine: BOMLayer
    leaf: BOMLayer
    shuffle_box_quantity: int
    shuffle_box_note: str


def _compute_fanouts(a: int, b: int) -> tuple[int, int]:
    """Given two port speeds a, b (Gbps), return (a_to_b_fanout, b_to_a_fanout)
    where the faster side is assumed to use breakout cables.
    Only one of the two values will be >1.
    """
    if a >= b:
        return max(1, a // b), 1
    return 1, max(1, b // a)


def _compute_matched_interface_fanouts(
    speed_a: int, speed_b: int, interface_speed: int
) -> tuple[int, int]:
    """Return fanouts when both ends should expose `interface_speed` lanes."""
    if speed_a < interface_speed or speed_b < interface_speed:
        raise ValueError(
            f"Matched interface speed {_fmt_speed(interface_speed)} requires both "
            f"ends to be at least {_fmt_speed(interface_speed)}."
        )
    if speed_a % interface_speed != 0 or speed_b % interface_speed != 0:
        raise ValueError(
            f"Matched interface speed {_fmt_speed(interface_speed)} must divide both "
            f"{_fmt_speed(speed_a)} and {_fmt_speed(speed_b)}."
        )
    return speed_a // interface_speed, speed_b // interface_speed


def _cable_label(
    speed_a: int, fanout_a_to_b: int, speed_b: int, fanout_b_to_a: int
) -> str:
    """Build a cable-type label like '800G-800G' or '800G-2x400G'."""
    if fanout_a_to_b > 1 and fanout_b_to_a > 1:
        lane_speed_a = speed_a // fanout_a_to_b
        lane_speed_b = speed_b // fanout_b_to_a
        if lane_speed_a == lane_speed_b:
            lane = _fmt_speed(lane_speed_a)
            return f"{lane}-{lane}"
    a = _fmt_speed(speed_a)
    b = _fmt_speed(speed_b)
    if fanout_a_to_b > 1:
        return f"{a}-{fanout_a_to_b}x{b}"
    if fanout_b_to_a > 1:
        # Faster side first, then breakout toward slower (e.g. 800G-2x400G not 2x400G-800G).
        return f"{b}-{fanout_b_to_a}x{a}"
    return f"{a}-{b}"


def _format_bom_optic_label(label: str) -> str:
    """Format cable labels for BOM display, e.g. 800G-2x400G -> 800G - 2x400G."""
    if "-" not in label:
        return label
    left, right = label.split("-", 1)
    return f"{left} - {right}"


def _bom_connection_detail(
    logical_links: int,
    speed_a: int,
    fanout_a_to_b: int,
    speed_b: int,
    fanout_b_to_a: int,
) -> BOMConnectionDetail:
    if logical_links <= 0:
        return BOMConnectionDetail()
    optics_count = _cable_count(logical_links, fanout_a_to_b, fanout_b_to_a)
    optics_label = _format_bom_optic_label(
        _cable_label(speed_a, fanout_a_to_b, speed_b, fanout_b_to_a)
    )
    return BOMConnectionDetail(
        optics_count=optics_count,
        optics_label=optics_label,
        cables_count=logical_links,
    )


def _cable_count(total_links: int, fanout_a_to_b: int, fanout_b_to_a: int) -> int:
    """Number of physical cables carrying `total_links` logical links,
    given breakout fanouts between the two ends.
    """
    if fanout_a_to_b > 1 and fanout_b_to_a > 1:
        return total_links
    per_cable = max(1, fanout_a_to_b, fanout_b_to_a)
    return math.ceil(total_links / per_cable)


# Minimum spines / super-spines whenever that tier is introduced (path redundancy).
MIN_TIER_SWITCHES = 2


def _clampTierSwitchCount(count: int) -> int:
    return max(count, MIN_TIER_SWITCHES)


def _parsePortRatio(ratio: str) -> tuple[float, float]:
    """
    Parse a "<down>:<up>" style ratio such as "1:1.16".

    Returns (down, up) as positive floats.
    """
    try:
        down_s, up_s = ratio.strip().split(":")
        down = float(down_s)
        up = float(up_s)
    except ValueError as exc:
        raise ValueError(f"Invalid ratio format: {ratio!r}. Expected '<down>:<up>'.") from exc
    if down <= 0 or up <= 0:
        raise ValueError(f"Ratio values must be > 0. Got {ratio!r}.")
    return down, up


def _floor_to_multiple(value: float, multiple: int) -> int:
    if multiple <= 1:
        return int(math.floor(value))
    return int((math.floor(value) // multiple) * multiple)


def _ceil_to_multiple(value: float, multiple: int) -> int:
    if multiple <= 1:
        return int(math.ceil(value))
    return int(math.ceil(value / multiple) * multiple)


def _design_fabric_compute(inp: DesignInputs) -> DesignResult:
    notes: list[str] = []

    single_plan_mode = inp.plans_per_nic == 0
    num_planes = 1 if single_plan_mode else inp.plans_per_nic * inp.nics_per_gpu
    nic_plan_speed = (
        inp.nic_speed if single_plan_mode else inp.nic_speed // inp.plans_per_nic
    )

    if not single_plan_mode and inp.nic_speed % inp.plans_per_nic != 0:
        return _infeasible(
            inp,
            notes
            + [
                f"NIC speed ({inp.nic_speed}G) must be divisible by plans_per_nic ({inp.plans_per_nic})."
            ],
        )

    # --- Validate speeds --------------------------------------------------
    if inp.leaf_speed < nic_plan_speed or inp.leaf_speed % nic_plan_speed != 0:
        return _infeasible(
            inp,
            notes
            + ["Leaf port speed must be >= NIC speed and an integer multiple of it."],
        )

    # Single-plan: one fabric carries every NIC link. Multi-plan: parallel
    # physical fabrics; each GPU still appears in every plan (NIC breakout legs).
    gpus_per_plane = (
        inp.num_gpus * inp.nics_per_gpu
        if single_plan_mode
        else inp.num_gpus
    )

    # Breakouts
    leaf_breakout = inp.leaf_speed // nic_plan_speed
    if inp.match_interface_speed_to_nic:
        leaf_to_spine_fanout, spine_to_leaf_fanout = (
            _compute_matched_interface_fanouts(
                inp.leaf_speed, inp.spine_speed, nic_plan_speed
            )
        )
    else:
        leaf_to_spine_fanout, spine_to_leaf_fanout = _compute_fanouts(
            inp.leaf_speed, inp.spine_speed
        )

    # --- Plans note -------------------------------------------------------
    if single_plan_mode:
        notes.append(
            f"Single-plan mode enabled: all NICs stay in one fabric plan "
            f"({inp.num_gpus} GPUs x {inp.nics_per_gpu} NIC/GPU = "
            f"{gpus_per_plane} NIC endpoints in the plan)."
        )
    else:
        notes.append(
            f"Parallel fabrics: plans_per_nic x NICs_per_GPU = "
            f"{inp.plans_per_nic} x {inp.nics_per_gpu} = {num_planes} plan(s). "
            f"NIC breakout per GPU NIC: 1x{inp.nic_speed}G -> "
            f"{inp.plans_per_nic}x{nic_plan_speed}G. "
            f"Every GPU is present in every plan at {nic_plan_speed}G on that plan's leg "
            f"({gpus_per_plane} GPU uplinks per plan for fabric sizing — parallel fabrics, "
            f"not a partition of the GPU fleet across plans)."
        )

    # --- Single-switch short-circuit ------------------------------------
    # If all GPU NICs in a plane fit on one leaf switch using every port as a
    # downlink (with breakout), a spine layer is unnecessary - a single leaf
    # terminates the whole plane.
    max_gpus_one_switch = inp.leaf_ports * leaf_breakout
    # Rail needs at least one leaf per GPU in a node (gpus_per_node leaf switches).
    rail_needs_leaf_row = inp.rail_design and inp.gpus_per_node > 1
    if not inp.rail_design:
        endpoints_per_node = inp.gpus_per_node * (
            inp.nics_per_gpu if single_plan_mode else 1
        )
        ports_per_node = math.ceil(endpoints_per_node / leaf_breakout)
        nodes_total = math.ceil(inp.num_gpus / inp.gpus_per_node)
        max_nodes_one_switch = inp.leaf_ports // ports_per_node
        can_single_switch = (
            gpus_per_plane <= max_gpus_one_switch
            and nodes_total <= max_nodes_one_switch
            and not rail_needs_leaf_row
            and inp.leaf_spine_ratio == "1:1"
        )
    else:
        can_single_switch = (
            gpus_per_plane <= max_gpus_one_switch
            and not rail_needs_leaf_row
            and inp.leaf_spine_ratio == "1:1"
        )

    if can_single_switch:
        return _single_switch_result(
            inp, num_planes, gpus_per_plane, leaf_breakout, notes
        )

    # --- Leaf port split via leaf_spine_ratio ---------------------------
    down_ratio, up_ratio = _parsePortRatio(inp.leaf_spine_ratio)

    downlink_ports_ideal = inp.leaf_ports * down_ratio / (down_ratio + up_ratio)

    if inp.rail_design:
        # Rail allows splitting a node across leaf switches, so only enforce
        # that both tiers have at least one port.
        downlink_ports = max(1, int(math.floor(downlink_ports_ideal)))
        uplink_granularity = max(1, leaf_to_spine_fanout)
        uplink_ports = _ceil_to_multiple(
            downlink_ports * up_ratio / down_ratio, uplink_granularity
        )
        max_uplink_ports = inp.leaf_ports - downlink_ports
        if uplink_ports > max_uplink_ports:
            uplink_ports = _floor_to_multiple(max_uplink_ports, uplink_granularity)
    else:
        # Non-rail: a switch can only connect to whole node NIC ports.
        endpoints_per_node = inp.gpus_per_node * (
            inp.nics_per_gpu if single_plan_mode else 1
        )
        ports_per_node = math.ceil(endpoints_per_node / leaf_breakout)
        downlink_ports = _floor_to_multiple(downlink_ports_ideal, ports_per_node)
        uplink_granularity = max(1, leaf_to_spine_fanout)
        uplink_ports = _ceil_to_multiple(
            downlink_ports * up_ratio / down_ratio, uplink_granularity
        )
        max_uplink_ports = inp.leaf_ports - downlink_ports
        if uplink_ports > max_uplink_ports:
            uplink_ports = _floor_to_multiple(max_uplink_ports, uplink_granularity)

    if downlink_ports <= 0 or uplink_ports <= 0:
        return _infeasible(
            inp,
            notes
            + ["Leaf radix too small to split given leaf-to-spine ratio."],
        )

    gpus_per_leaf = downlink_ports * leaf_breakout

    if inp.rail_design:
        leaves_per_plane = math.ceil(gpus_per_plane / gpus_per_leaf)
    else:
        endpoints_per_node = inp.gpus_per_node * (
            inp.nics_per_gpu if single_plan_mode else 1
        )
        ports_per_node = math.ceil(endpoints_per_node / leaf_breakout)
        nodes_per_leaf = downlink_ports // ports_per_node
        if nodes_per_leaf <= 0:
            return _infeasible(
                inp,
                notes
                + ["Non-rail node ports do not fit on even one leaf switch."],
            )
        endpoints_capacity_per_leaf = nodes_per_leaf * endpoints_per_node
        leaves_per_plane = math.ceil(gpus_per_plane / endpoints_capacity_per_leaf)
    if inp.rail_design:
        rail_multiple = inp.gpus_per_node
        leaves_per_plane = max(leaves_per_plane, rail_multiple)
        leaves_per_plane = math.ceil(leaves_per_plane / rail_multiple) * rail_multiple
        notes.append(
            f"Rail design enabled: node-to-leaf mapping uses {inp.gpus_per_node} leaf "
            f"switches per node (GPUi to leaf i); minimum {rail_multiple} leaves/plan, "
            f"rounded up to a multiple of GPUs per node — using {leaves_per_plane} "
            f"leaves/plan."
        )

    # --- 2-tier sizing (first-pass) -------------------------------------
    links_per_leaf = uplink_ports * leaf_to_spine_fanout

    # Classic: one link per (leaf, spine) pair -> `links_per_leaf` spines
    spines_per_plane = links_per_leaf
    links_per_leaf_to_each_spine = 1
    spine_ports_used = math.ceil(leaves_per_plane / spine_to_leaf_fanout)

    # Try link bundling to reduce spine count while respecting spine radix.
    # Never bundle below MIN_TIER_SWITCHES spines (redundant spine pair).
    for b in range(links_per_leaf, 1, -1):
        if links_per_leaf % b:
            continue
        spines_candidate = links_per_leaf // b
        if spines_candidate < MIN_TIER_SWITCHES:
            continue
        candidate = math.ceil((leaves_per_plane * b) / spine_to_leaf_fanout)
        if candidate <= inp.spine_ports:
            if b > 1:
                links_per_leaf_to_each_spine = b
                spines_per_plane = spines_candidate
                spine_ports_used = candidate
            break

    spine_redundancy_ok = links_per_leaf >= MIN_TIER_SWITCHES
    if spines_per_plane < MIN_TIER_SWITCHES and spine_redundancy_ok:
        links_per_leaf_to_each_spine = links_per_leaf // MIN_TIER_SWITCHES
        spines_per_plane = MIN_TIER_SWITCHES
        spine_ports_used = math.ceil(
            (leaves_per_plane * links_per_leaf_to_each_spine) / spine_to_leaf_fanout
        )

    two_tier_ok = spine_ports_used <= inp.spine_ports and spine_redundancy_ok
    plane_kwargs = dict(
        nic_speed_raw=inp.nic_speed,
        nic_speed=nic_plan_speed,
        leaf_speed=inp.leaf_speed,
        spine_speed=inp.spine_speed,
        super_spine_speed=inp.super_spine_speed,
        leaf_breakout=leaf_breakout,
        leaf_to_spine_fanout=leaf_to_spine_fanout,
        spine_to_leaf_fanout=spine_to_leaf_fanout,
        spine_to_super_fanout=1,
        super_to_spine_fanout=1,
        downlink_ports_per_leaf=downlink_ports,
        uplink_ports_per_leaf=uplink_ports,
        gpus_per_leaf=gpus_per_leaf,
        gpus_per_plane=gpus_per_plane,
        leaves_per_plane=leaves_per_plane,
        spines_per_plane=spines_per_plane,
        links_per_leaf_to_each_spine=links_per_leaf_to_each_spine,
        spine_ports_used_for_leaves=spine_ports_used,
    )

    # --- Decide 2-tier vs 3-tier ----------------------------------------
    if two_tier_ok:
        notes.append(
            f"2-tier spine-leaf is sufficient "
            f"(leaves={leaves_per_plane}, {spines_per_plane} spines/plan, "
            f"each spine uses {spine_ports_used}/{inp.spine_ports} ports)."
        )
        notes.append(
            f"Spine redundancy: at least {MIN_TIER_SWITCHES} spines per plan."
        )
        if inp.super_spine_speed:
            notes.append(
                f"Super-spine ({inp.super_spine_speed}G) not required at this "
                "scale - not included in the design."
            )
        _add_common_notes(
            notes,
            inp,
            leaf_breakout,
            leaf_to_spine_fanout,
            spine_to_leaf_fanout,
            downlink_ports,
            uplink_ports,
            gpus_per_leaf,
            num_planes,
        )
        plane = PlaneDesign(**plane_kwargs)
        return DesignResult(
            inputs=inp,
            num_planes=num_planes,
            plane=plane,
            total_leaves=leaves_per_plane * num_planes,
            total_spines=spines_per_plane * num_planes,
            total_super_spines=0,
            total_nodes=math.ceil(inp.num_gpus / inp.gpus_per_node),
            cables=_compute_cables(inp, plane, num_planes),
            notes=notes,
            feasible=True,
            topology="spine-leaf",
        )

    # 2-tier not sufficient.
    if inp.super_spine_speed == 0:
        if not spine_redundancy_ok:
            notes.append(
                f"2-tier infeasible: leaf uplinks support only {links_per_leaf} "
                f"spine path(s), but at least {MIN_TIER_SWITCHES} spines per plan "
                f"are required for redundancy. Increase leaf uplink capacity or "
                f"enable a super-spine tier."
            )
        else:
            notes.append(
                f"2-tier infeasible: a single spine would need {spine_ports_used} "
                f"ports to accept one link from each of {leaves_per_plane} leaves "
                f"(after {spine_to_leaf_fanout}:1 breakout), but spines only have "
                f"{inp.spine_ports} ports. Enable a super-spine tier to continue."
            )
        _add_common_notes(
            notes,
            inp,
            leaf_breakout,
            leaf_to_spine_fanout,
            spine_to_leaf_fanout,
            downlink_ports,
            uplink_ports,
            gpus_per_leaf,
            num_planes,
        )
        plane = PlaneDesign(**plane_kwargs)
        return DesignResult(
            inputs=inp,
            num_planes=num_planes,
            plane=plane,
            total_leaves=leaves_per_plane * num_planes,
            total_spines=0,
            total_super_spines=0,
            total_nodes=math.ceil(inp.num_gpus / inp.gpus_per_node),
            notes=notes,
            feasible=False,
        )

    # --- 3-tier (super-spine) sizing ------------------------------------
    down_ratio, up_ratio = _parsePortRatio(inp.spine_super_ratio)

    # Split spine ports between "down" (to leaves) and "up" (to super-spines)
    # using spine_super_ratio.
    #
    # We intentionally allow some ports to remain unused when rounding to
    # integer ports and to fanout-aligned multiples.
    if inp.match_interface_speed_to_nic:
        spine_to_super_fanout, super_to_spine_fanout = (
            _compute_matched_interface_fanouts(
                inp.spine_speed, inp.super_spine_speed, nic_plan_speed
            )
        )
    else:
        spine_to_super_fanout, super_to_spine_fanout = _compute_fanouts(
            inp.spine_speed, inp.super_spine_speed
        )

    spine_ports_down_ideal = inp.spine_ports * down_ratio / (down_ratio + up_ratio)
    spine_ports_down = max(1, int(math.floor(spine_ports_down_ideal)))
    uplink_granularity = max(1, spine_to_super_fanout)
    spine_ports_up = _ceil_to_multiple(
        spine_ports_down * up_ratio / down_ratio, uplink_granularity
    )
    max_spine_ports_up = inp.spine_ports - spine_ports_down
    if spine_ports_up > max_spine_ports_up:
        spine_ports_up = _floor_to_multiple(max_spine_ports_up, uplink_granularity)

    if spine_ports_up <= 0:
        return _infeasible(
            inp,
            notes
            + ["Spine radix too small to split given spine-to-super-spine ratio."],
        )

    leaves_per_pod = spine_ports_down * spine_to_leaf_fanout

    # Each pod uses a full spine-leaf mesh without bundling.
    spines_per_pod = _clampTierSwitchCount(
        links_per_leaf
    )  # = uplink_ports * leaf_to_spine_fanout
    pods_per_plane = math.ceil(leaves_per_plane / leaves_per_pod)
    spines_per_plane_3t = spines_per_pod * pods_per_plane

    # Super-spine layer: size by aggregate bandwidth (fat-tree Clos). Each
    # super-spine fully uses its ports toward the spine layer; we compute the
    # number of super-spines needed to absorb all spine uplinks. Spines do
    # not need to fully mesh with every super-spine - Clos non-blocking
    # holds as long as aggregate capacity and path diversity are sufficient.
    total_spine_super_links = (
        spines_per_plane_3t * spine_ports_up * spine_to_super_fanout
    )
    links_absorbed_per_super = inp.super_spine_ports * super_to_spine_fanout
    super_spines_per_plane = _clampTierSwitchCount(
        math.ceil(total_spine_super_links / links_absorbed_per_super)
    )
    ports_used_per_super = (
        inp.super_spine_ports
    )  # all super-spine ports used toward spine layer

    # Each spine should be able to reach at least `super_spines_per_plane`
    # super-spines (one link each) for path diversity; fewer works with
    # bundling. We flag infeasibility only if a spine can't even fan out
    # at one link per super-spine that holds it.
    spine_reach = spine_ports_up * spine_to_super_fanout
    feasible = (
        spine_reach >= 1
        and super_spines_per_plane >= MIN_TIER_SWITCHES
        and spines_per_pod >= MIN_TIER_SWITCHES
    )

    plane_kwargs.update(
        spines_per_plane=spines_per_plane_3t,
        links_per_leaf_to_each_spine=1,
        spine_ports_used_for_leaves=math.ceil(leaves_per_pod / spine_to_leaf_fanout),
        spine_to_super_fanout=spine_to_super_fanout,
        super_to_spine_fanout=super_to_spine_fanout,
    )
    plane = PlaneDesign(
        **plane_kwargs,
        uses_super_spine=True,
        leaves_per_pod=leaves_per_pod,
        spines_per_pod=spines_per_pod,
        pods_per_plane=pods_per_plane,
        spine_ports_up_to_super=spine_ports_up,
        super_spines_per_plane=super_spines_per_plane,
        ports_used_per_super_spine=ports_used_per_super,
    )

    notes.append(
        f"3-tier design: {pods_per_plane} pod(s) per plane, each pod with "
        f"{leaves_per_pod} leaves and {spines_per_pod} spines; "
        f"{super_spines_per_plane} super-spines @ {inp.super_spine_speed}G "
        f"with {inp.super_spine_ports} ports each."
    )
    notes.append(
        f"Spine and super-spine redundancy: at least {MIN_TIER_SWITCHES} "
        f"spines per pod and {MIN_TIER_SWITCHES} super-spines per plan."
    )
    notes.append(
        f"Each spine splits its {inp.spine_ports} ports as "
        f"{spine_ports_down} down (to pod leaves) + {spine_ports_up} up "
        f"(to super-spines) for spine-to-super ratio {inp.spine_super_ratio}."
    )
    if inp.match_interface_speed_to_nic and (
        spine_to_super_fanout > 1 or super_to_spine_fanout > 1
    ):
        notes.append(
            "Fabric interface speed matched to NIC speed: "
            f"spine↔super-spine uses {_fmt_speed(nic_plan_speed)} lanes, with each "
            f"{_fmt_speed(inp.spine_speed)} spine port breaking out {spine_to_super_fanout}:1 "
            f"and each {_fmt_speed(inp.super_spine_speed)} super-spine port breaking out "
            f"{super_to_spine_fanout}:1."
        )
    else:
        if spine_to_super_fanout > 1:
            notes.append(
                f"Spine-to-super breakout: each {inp.spine_speed}G spine port "
                f"splits into {spine_to_super_fanout} x {inp.super_spine_speed}G "
                "super-spine links."
            )
        if super_to_spine_fanout > 1:
            notes.append(
                f"Super-to-spine breakout: each {inp.super_spine_speed}G super-spine "
                f"port splits into {super_to_spine_fanout} x {inp.spine_speed}G "
                "spine-side links."
            )
    links_per_spine_super_pair = (
        max(1, spine_reach // super_spines_per_plane) if super_spines_per_plane else 0
    )
    notes.append(
        f"Super-spine sizing: {spines_per_plane_3t} spines x {spine_ports_up} "
        f"uplink ports = {total_spine_super_links} links absorbed by "
        f"{super_spines_per_plane} super-spine(s) at "
        f"{links_absorbed_per_super} links each "
        f"(~{links_per_spine_super_pair} link(s) per spine-super pair)."
    )

    _add_common_notes(
        notes,
        inp,
        leaf_breakout,
        leaf_to_spine_fanout,
        spine_to_leaf_fanout,
        downlink_ports,
        uplink_ports,
        gpus_per_leaf,
        num_planes,
    )

    return DesignResult(
        inputs=inp,
        num_planes=num_planes,
        plane=plane,
        total_leaves=leaves_per_plane * num_planes,
        total_spines=spines_per_plane_3t * num_planes,
        total_super_spines=super_spines_per_plane * num_planes,
        total_nodes=math.ceil(inp.num_gpus / inp.gpus_per_node),
        cables=_compute_cables(inp, plane, num_planes),
        notes=notes,
        feasible=feasible,
        topology="3-tier",
    )


def _single_switch_result(
    inp: DesignInputs,
    num_planes: int,
    gpus_per_plane: int,
    leaf_breakout: int,
    notes: list[str],
) -> DesignResult:
    """All GPU NICs in a plane fit on one leaf. No spine layer required."""
    if inp.rail_design:
        downlink_ports_used = math.ceil(gpus_per_plane / leaf_breakout)
    else:
        # Non-rail: map whole node NIC ports onto the single leaf.
        single_plan_mode = inp.plans_per_nic == 0
        endpoints_per_node = inp.gpus_per_node * (
            inp.nics_per_gpu if single_plan_mode else 1
        )
        ports_per_node = math.ceil(endpoints_per_node / leaf_breakout)
        nodes_total = math.ceil(inp.num_gpus / inp.gpus_per_node)
        downlink_ports_used = nodes_total * ports_per_node
    plane = PlaneDesign(
        nic_speed_raw=inp.nic_speed,
        nic_speed=inp.nic_speed // max(1, inp.plans_per_nic),
        leaf_speed=inp.leaf_speed,
        spine_speed=inp.spine_speed,
        super_spine_speed=0,
        leaf_breakout=leaf_breakout,
        leaf_to_spine_fanout=1,
        spine_to_leaf_fanout=1,
        spine_to_super_fanout=1,
        super_to_spine_fanout=1,
        downlink_ports_per_leaf=downlink_ports_used,
        uplink_ports_per_leaf=0,
        gpus_per_leaf=gpus_per_plane,
        gpus_per_plane=gpus_per_plane,
        leaves_per_plane=1,
        spines_per_plane=0,
        links_per_leaf_to_each_spine=0,
        spine_ports_used_for_leaves=0,
    )

    cables = _compute_cables(inp, plane, num_planes)

    notes.append(
        f"Single-switch (collapsed) design: {gpus_per_plane} GPU NICs per "
        f"plane fit on one {inp.leaf_ports}-port {_fmt_speed(inp.leaf_speed)} "
        f"leaf using {downlink_ports_used} downlink ports "
        f"(breakout {leaf_breakout}:1). No spine layer required."
    )
    if leaf_breakout > 1:
        notes.append(
            f"Leaf-to-NIC breakout: each {inp.leaf_speed}G port splits into "
            f"{leaf_breakout} x {inp.nic_speed // max(1, inp.plans_per_nic)}G NIC links."
        )
    if inp.plans_per_nic > 1:
        notes.append(
            "Node-side NIC breakout is in use (plans per NIC > 1); a shuffle box might be needed per node."
        )
    elif inp.plans_per_nic == 0:
        notes.append(
            "Single-plan mode: all NICs per GPU are placed in one fabric (no per-NIC breakout plans)."
        )
    total_nodes = math.ceil(inp.num_gpus / inp.gpus_per_node)
    nic_split_note = (
        f"{inp.plans_per_nic} x {inp.nic_speed // max(1, inp.plans_per_nic)}G per NIC"
        if inp.plans_per_nic > 0
        else "single-plan mode (no per-NIC split)"
    )
    notes.append(
        f"Nodes: {total_nodes} total (each with {inp.gpus_per_node} GPUs and "
        f"{inp.nics_per_gpu} x {inp.nic_speed}G NIC(s), split as "
        f"{nic_split_note})."
    )
    if num_planes > 1:
        notes.append(
            f"Totals are {num_planes} x per-plan counts (one collapsed leaf per plan). "
            f"Each plan is a separate fabric, but all {inp.num_gpus} GPUs attach to each."
        )

    return DesignResult(
        inputs=inp,
        num_planes=num_planes,
        plane=plane,
        total_leaves=num_planes,
        total_spines=0,
        total_super_spines=0,
        total_nodes=total_nodes,
        cables=cables,
        notes=notes,
        feasible=True,
        topology="single-switch",
    )


def _compute_cables(
    inp: DesignInputs, plane: PlaneDesign, num_planes: int
) -> list[CableGroup]:
    """Count physical cables per layer (summed across planes).
    Breakout cables count as one physical cable carrying N logical links.
    """
    cables: list[CableGroup] = []

    # Leaf <-> Node (GPU NIC): per plane, one logical uplink per GPU per NIC leg
    # in multi-plan mode; summed across planes for total cabling.
    if plane.leaves_per_plane > 0:
        leaf_nic_links_per_plane = (
            inp.num_gpus * inp.nics_per_gpu if inp.plans_per_nic == 0 else inp.num_gpus
        )
        leaf_nic_count = (
            _cable_count(leaf_nic_links_per_plane, plane.leaf_breakout, 1) * num_planes
        )
        cables.append(
            CableGroup(
                count=leaf_nic_count,
                label=_cable_label(
                    plane.leaf_speed, plane.leaf_breakout, plane.nic_speed, 1
                ),
                end_a="Leaf",
                end_b="Node",
            )
        )

    # Spine <-> Leaf
    if plane.spines_per_plane > 0:
        sl_links = (
            plane.leaves_per_plane
            * plane.uplink_ports_per_leaf
            * plane.leaf_to_spine_fanout
        )
        sl_count = (
            _cable_count(
                sl_links, plane.leaf_to_spine_fanout, plane.spine_to_leaf_fanout
            )
            * num_planes
        )
        cables.append(
            CableGroup(
                count=sl_count,
                label=_cable_label(
                    plane.leaf_speed,
                    plane.leaf_to_spine_fanout,
                    plane.spine_speed,
                    plane.spine_to_leaf_fanout,
                ),
                end_a="Spine",
                end_b="Leaf",
            )
        )

    # Super-spine <-> Spine
    if plane.uses_super_spine and plane.super_spines_per_plane > 0:
        ss_links = (
            plane.spines_per_plane
            * plane.spine_ports_up_to_super
            * plane.spine_to_super_fanout
        )
        ss_count = (
            _cable_count(
                ss_links, plane.spine_to_super_fanout, plane.super_to_spine_fanout
            )
            * num_planes
        )
        cables.append(
            CableGroup(
                count=ss_count,
                label=_cable_label(
                    plane.spine_speed,
                    plane.spine_to_super_fanout,
                    plane.super_spine_speed,
                    plane.super_to_spine_fanout,
                ),
                end_a="Super-spine",
                end_b="Spine",
            )
        )

    return cables


def _bom_port_breakout_detail(
    port_links: int,
    port_speed: int,
    lane_speed: int,
) -> BOMConnectionDetail:
    """Physical optics at port speed, logical cables at lane speed (e.g. 800G -> 2x400G)."""
    if port_links <= 0 or port_speed <= 0 or lane_speed <= 0:
        return BOMConnectionDetail()
    if port_speed == lane_speed:
        label = _format_bom_optic_label(f"{_fmt_speed(port_speed)}-{_fmt_speed(lane_speed)}")
        return BOMConnectionDetail(
            optics_count=port_links,
            optics_label=label,
            cables_count=port_links,
        )
    if port_speed % lane_speed != 0:
        return BOMConnectionDetail()
    lane_mult = port_speed // lane_speed
    label = _format_bom_optic_label(
        f"{_fmt_speed(port_speed)}-{lane_mult}x{_fmt_speed(lane_speed)}"
    )
    return BOMConnectionDetail(
        optics_count=port_links,
        optics_label=label,
        cables_count=port_links * lane_mult,
    )


def build_bill_of_materials(result: DesignResult) -> BillOfMaterials:
    """Layered BOM with per-tier switch counts and south/north optics vs cables."""
    inp = result.inputs
    plane = result.plane
    num_planes = result.num_planes

    leaf_switch_spec = f"{inp.leaf_ports}-port @{_fmt_speed(inp.leaf_speed)}"
    spine_switch_spec = f"{inp.spine_ports}-port @{_fmt_speed(inp.spine_speed)}"
    super_speed = plane.super_spine_speed
    if result.total_super_spines == 0:
        super_switch_spec = "Not used"
    elif super_speed:
        super_switch_spec = f"{inp.super_spine_ports}-port @{_fmt_speed(super_speed)}"
    else:
        super_switch_spec = f"{inp.super_spine_ports}-port (speed unset)"

    # --- Leaf-to-node logical links (summed across planes) ---------------------------
    leaf_node_links = 0
    if plane.leaves_per_plane > 0:
        leaf_node_links_per_plane = (
            inp.num_gpus * inp.nics_per_gpu if inp.plans_per_nic == 0 else inp.num_gpus
        )
        leaf_node_links = leaf_node_links_per_plane * num_planes

    # Leaf south always follows leaf-port vs NIC (plan) speed breakout.
    leaf_south = _bom_connection_detail(
        leaf_node_links,
        plane.leaf_speed,
        plane.leaf_breakout,
        plane.nic_speed,
        1,
    )

    # Spine <-> leaf hop:
    # - match Yes: model as NIC-speed lanes (e.g. 800G - 2x400G)
    # - match No: model as native port-speed links (e.g. 800G - 800G)
    spine_leaf_port_links = 0
    if plane.leaves_per_plane > 0 and plane.uplink_ports_per_leaf > 0:
        spine_leaf_port_links = (
            plane.leaves_per_plane * plane.uplink_ports_per_leaf * num_planes
        )
    if inp.match_interface_speed_to_nic:
        spine_leaf_hop = _bom_port_breakout_detail(
            spine_leaf_port_links,
            min(inp.leaf_speed, inp.spine_speed),
            plane.nic_speed,
        )
    else:
        spine_leaf_hop = _bom_connection_detail(
            spine_leaf_port_links,
            plane.leaf_speed,
            plane.leaf_to_spine_fanout,
            plane.spine_speed,
            plane.spine_to_leaf_fanout,
        )
    leaf_north = spine_leaf_hop
    spine_south = spine_leaf_hop

    # Spine <-> super-spine hop follows the same Yes/No rule.
    super_spine_port_links = 0
    if plane.uses_super_spine and plane.spines_per_plane > 0:
        super_spine_port_links = (
            plane.spines_per_plane * plane.spine_ports_up_to_super * num_planes
        )
    if inp.match_interface_speed_to_nic and plane.super_spine_speed > 0:
        super_spine_hop = _bom_port_breakout_detail(
            super_spine_port_links,
            min(inp.spine_speed, inp.super_spine_speed),
            plane.nic_speed,
        )
    else:
        super_spine_hop = _bom_connection_detail(
            super_spine_port_links,
            plane.spine_speed,
            plane.spine_to_super_fanout,
            plane.super_spine_speed,
            plane.super_to_spine_fanout,
        )
    spine_north = super_spine_hop
    super_south = super_spine_hop

    super_layer_note = ""
    if result.total_super_spines == 0:
        if result.topology == "3-tier" and plane.uses_super_spine:
            super_layer_note = (
                "Super-spine layer included in topology model but count is zero "
                "or design marked infeasible; verify inputs."
            )

    spine_layer_note = ""
    if result.total_spines == 0:
        if result.topology == "single-switch":
            spine_layer_note = (
                "Spine layer not required (collapsed single-switch / leaf-only)."
            )
        elif not result.feasible:
            spine_layer_note = (
                "Spine layer not sized — design infeasible with current inputs."
            )

    leaf_layer_note = ""
    if result.total_leaves == 0 and not result.feasible:
        leaf_layer_note = (
            "Leaf layer not sized — design infeasible with current inputs."
        )
    elif result.total_leaves > 0:
        single_plan_mode = inp.plans_per_nic == 0
        endpoints_per_node = inp.gpus_per_node * (
            inp.nics_per_gpu if single_plan_mode else 1
        )
        ports_per_node = (
            math.ceil(endpoints_per_node / plane.leaf_breakout)
            if plane.leaf_breakout > 0
            else 0
        )
        nodes_per_leaf = (
            plane.downlink_ports_per_leaf // ports_per_node
            if ports_per_node > 0
            else 0
        )
        leaf_layer_note = (
            f"Per leaf (per plan): {nodes_per_leaf} node(s); "
            f"{plane.downlink_ports_per_leaf} south ports, "
            f"{plane.uplink_ports_per_leaf} north ports."
        )

    shuffle_qty = 0
    shuffle_note = (
        "No shuffle count is assumed in the BOM. If you use multiple parallel "
        "planes with plans_per_nic > 1, shuffle assemblies might still be needed "
        "for node-side fan-out; actual materials depend on cable and optic "
        "choices, and many valid options exist."
    )
    if result.num_planes > 1 and inp.plans_per_nic > 1 and leaf_node_links > 0:
        shuffle_qty = leaf_south.optics_count
        shuffle_note = (
            f"Multi-plane ({result.num_planes}) with NIC plan breakout "
            f"(plans_per_nic={inp.plans_per_nic}): shuffle boxes might be needed "
            f"for node-side fan-out into per-plan links. This model’s "
            f"{shuffle_qty:,} leaf↔node optic(s) is only a planning hint tied to "
            f"the breakout math above — not a firm order of materials, because "
            f"real deployments depend on cable and optic choices and there are "
            f"many options."
        )

    context_line = (
        f"Based on {inp.num_gpus:,} GPU(s) with a {inp.leaf_spine_ratio} "
        f"leaf-to-spine ratio"
    )
    if inp.spine_super_ratio != "1:1":
        context_line += f" and {inp.spine_super_ratio} spine-to-super-spine ratio"
    context_line += "."

    return BillOfMaterials(
        context_line=context_line,
        super_spine=BOMLayer(
            title="Super-spine",
            switch_quantity=result.total_super_spines,
            switch_specification=super_switch_spec,
            south_to="Spine",
            north_to="",
            south=super_south,
            north=BOMConnectionDetail(),
            layer_note=super_layer_note,
        ),
        spine=BOMLayer(
            title="Spine",
            switch_quantity=result.total_spines,
            switch_specification=spine_switch_spec,
            south_to="Leaf",
            north_to="Super-spine",
            south=spine_south,
            north=spine_north,
            layer_note=spine_layer_note,
        ),
        leaf=BOMLayer(
            title="Leaf",
            switch_quantity=result.total_leaves,
            switch_specification=leaf_switch_spec,
            south_to="node",
            north_to="Spine",
            south=leaf_south,
            north=leaf_north,
            layer_note=leaf_layer_note,
        ),
        shuffle_box_quantity=shuffle_qty,
        shuffle_box_note=shuffle_note,
    )


def design_fabric(inp: DesignInputs) -> DesignResult:
    result = _design_fabric_compute(inp)
    result.bom = build_bill_of_materials(result)
    return result


def _infeasible(inp: DesignInputs, notes: list[str]) -> DesignResult:
    plane = PlaneDesign(
        nic_speed=inp.nic_speed // max(1, inp.plans_per_nic),
        nic_speed_raw=inp.nic_speed,
        leaf_speed=inp.leaf_speed,
        spine_speed=inp.spine_speed,
        super_spine_speed=inp.super_spine_speed,
        leaf_breakout=0,
        leaf_to_spine_fanout=0,
        spine_to_leaf_fanout=0,
        spine_to_super_fanout=0,
        super_to_spine_fanout=0,
        downlink_ports_per_leaf=0,
        uplink_ports_per_leaf=0,
        gpus_per_leaf=0,
        gpus_per_plane=(
            inp.num_gpus * inp.nics_per_gpu
            if inp.plans_per_nic == 0
            else inp.num_gpus
        ),
        leaves_per_plane=0,
        spines_per_plane=0,
        links_per_leaf_to_each_spine=0,
        spine_ports_used_for_leaves=0,
    )
    return DesignResult(
        inputs=inp,
        num_planes=1
        if inp.plans_per_nic == 0
        else inp.plans_per_nic * inp.nics_per_gpu,
        plane=plane,
        total_leaves=0,
        total_spines=0,
        total_super_spines=0,
        total_nodes=math.ceil(inp.num_gpus / inp.gpus_per_node),
        notes=notes,
        feasible=False,
    )


def _add_common_notes(
    notes: list[str],
    inp: DesignInputs,
    leaf_breakout: int,
    leaf_to_spine_fanout: int,
    spine_to_leaf_fanout: int,
    downlink_ports: int,
    uplink_ports: int,
    gpus_per_leaf: int,
    num_planes: int,
) -> None:
    nic_plan_speed = inp.nic_speed if inp.plans_per_nic == 0 else inp.nic_speed // inp.plans_per_nic
    if leaf_breakout > 1:
        notes.append(
            f"Leaf-to-NIC breakout: each {inp.leaf_speed}G leaf port splits "
            f"into {leaf_breakout} x {inp.nic_speed // max(1, inp.plans_per_nic)}G NIC links."
        )
    if inp.plans_per_nic > 1:
        notes.append(
            "Node-side NIC breakout is in use (plans per NIC > 1); a shuffle box might be needed per node."
        )
    elif inp.plans_per_nic == 0:
        notes.append(
            "Single-plan mode: all NICs per GPU are placed in one fabric (no per-NIC breakout plans)."
        )
    if inp.match_interface_speed_to_nic and (
        leaf_to_spine_fanout > 1 or spine_to_leaf_fanout > 1
    ):
        notes.append(
            "Fabric interface speed matched to NIC speed: "
            f"leaf↔spine uses {_fmt_speed(nic_plan_speed)} lanes, with each "
            f"{_fmt_speed(inp.leaf_speed)} leaf port breaking out {leaf_to_spine_fanout}:1 "
            f"and each {_fmt_speed(inp.spine_speed)} spine port breaking out "
            f"{spine_to_leaf_fanout}:1."
        )
    else:
        if leaf_to_spine_fanout > 1:
            notes.append(
                f"Leaf-to-spine breakout: each {inp.leaf_speed}G leaf uplink "
                f"splits into {leaf_to_spine_fanout} x {inp.spine_speed}G links."
            )
        if spine_to_leaf_fanout > 1:
            notes.append(
                f"Spine-to-leaf breakout: each {inp.spine_speed}G spine port "
                f"splits into {spine_to_leaf_fanout} x {inp.leaf_speed}G links."
            )
    notes.append(
        f"Leaf port split: {downlink_ports} downlinks + {uplink_ports} uplinks = "
        f"{downlink_ports + uplink_ports} used ports (leaf-to-spine {inp.leaf_spine_ratio})."
    )
    notes.append(
        f"GPUs per leaf (per plan): {gpus_per_leaf} "
        f"({downlink_ports} ports x {leaf_breakout} breakout)."
    )
    total_nodes = math.ceil(inp.num_gpus / inp.gpus_per_node)
    notes.append(
        f"Nodes: {total_nodes} total (each with {inp.gpus_per_node} GPUs and "
        f"{inp.nics_per_gpu} x {inp.nic_speed}G NIC(s), split as "
        f"{f'{inp.plans_per_nic} x {inp.nic_speed // max(1, inp.plans_per_nic)}G per NIC' if inp.plans_per_nic > 0 else 'single-plan mode (no per-NIC split)'})."
    )
    if num_planes > 1:
        notes.append(
            f"Totals are {num_planes} x per-plan counts (parallel physical fabrics). "
            f"Every GPU is in every plan at {inp.nic_speed // inp.plans_per_nic}G per plan leg "
            f"from NIC breakout — not fewer GPUs per plan."
        )


# ---------------------------------------------------------------------------
# SVG diagram
# ---------------------------------------------------------------------------


def _fmt_speed(gbps: int) -> str:
    if gbps >= 1000:
        v = gbps / 1000
        return f"{int(v)}T" if v == int(v) else f"{v:.1f}T"
    return f"{gbps}G"


# A "slot" is either a real item or an ellipsis placeholder.
ELLIPSIS = object()


def _slots(count: int, max_items: int = 9, head: int = 5, tail: int = 3) -> list:
    """Return a list of slots for drawing.

    If count <= max_items: returns [0, 1, ..., count-1]
    Else: returns first `head` indices, then ELLIPSIS, then last `tail` indices.
    """
    if count <= max_items:
        return list(range(count))
    return list(range(head)) + [ELLIPSIS] + list(range(count - tail, count))


def _slots_for_zoom(count: int, zoom: DiagramZoomMode) -> list:
    """Detail mode uses ellipsis for large counts; fit mode lists every index."""
    if count <= 0:
        return []
    if zoom == "fit":
        return list(range(count))
    return _slots(count)


def _xs(n: int, width: int, margin: int = 90) -> list[float]:
    if n == 0:
        return []
    if n == 1:
        return [width / 2]
    step = (width - 2 * margin) / (n - 1)
    return [margin + i * step for i in range(n)]


# Layer labels sit just left of row graphics; reserve horizontal space so
# text (esp. "SUPER-SPINE") does not overlap rects and is not clipped.
_DIAGRAM_MAX_NODE_ICONS = 24
_LAYER_LABEL_GAP = 12.0
_LAYER_LABEL_EST_WIDTH = 128.0


def _layer_label_anchor_x(content_left_edge: float) -> float:
    return content_left_edge - _LAYER_LABEL_GAP


def _rail_line_segments(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    split_x: Optional[float],
) -> list[tuple[float, float, float, float, float]]:
    """Return (x1, y1, x2, y2, opacity) segments; fade after crossing split_x."""
    if split_x is None or abs(bx - ax) < 1e-9:
        return [(ax, ay, bx, by, 0.92)]
    lo, hi = (ax, bx) if ax <= bx else (bx, ax)
    if not (lo + 1e-6 < split_x < hi - 1e-6):
        return [(ax, ay, bx, by, 0.92)]
    t = (split_x - ax) / (bx - ax)
    if t <= 1e-6 or t >= 1.0 - 1e-6:
        return [(ax, ay, bx, by, 0.92)]
    sx = ax + t * (bx - ax)
    sy = ay + t * (by - ay)
    return [(ax, ay, sx, sy, 0.92), (sx, sy, bx, by, 0.16)]


def _rail_ellipsis_fan_offsets(count: int, spread: float = 22.0) -> list[float]:
    """Horizontal offsets so multiple rail lines to the '...' slot do not stack."""
    if count <= 0:
        return []
    if count == 1:
        return [0.0]
    half = spread / 2
    return [half * (2 * i / (count - 1) - 1) for i in range(count)]


def render_svg(result: DesignResult, diagram_zoom: DiagramZoomMode = "detail") -> str:
    plane = result.plane
    if not result.feasible or plane.leaves_per_plane == 0:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 120">'
            '<text x="300" y="60" text-anchor="middle" fill="#b91c1c" '
            'font-family="sans-serif" font-size="16">'
            "Design not feasible with given inputs.</text></svg>"
        )

    width = 1260
    has_super = plane.uses_super_spine
    single_switch = result.topology == "single-switch"
    # Row y-positions
    if has_super:
        super_y = 70
        spine_y = 220
        leaf_y = 370
        node_y = 520
        height = 640
    elif single_switch:
        super_y = None
        spine_y = None
        leaf_y = 120
        node_y = 280
        height = 440
    else:
        super_y = None
        spine_y = 80
        leaf_y = 260
        node_y = 470
        height = 620

    # Determine what to draw. In 3-tier we draw ONE pod of leaves/spines
    # plus the super-spine row, with ellipsis to indicate more pods.
    if has_super:
        draw_leaves_count = plane.leaves_per_pod
        draw_spines_count = plane.spines_per_pod
    elif single_switch:
        draw_leaves_count = 1
        draw_spines_count = 0
    else:
        draw_leaves_count = plane.leaves_per_plane
        draw_spines_count = plane.spines_per_plane

    zoom_fit = diagram_zoom == "fit"

    spine_slots = _slots_for_zoom(draw_spines_count, diagram_zoom) if draw_spines_count else []
    leaf_slots = _slots_for_zoom(draw_leaves_count, diagram_zoom)

    spine_xs = _xs(len(spine_slots), width)
    leaf_xs = _xs(len(leaf_slots), width)

    sspine_slots: list = []
    sspine_xs: list[float] = []
    if has_super:
        sspine_slots = _slots_for_zoom(plane.super_spines_per_plane, diagram_zoom)
        sspine_xs = _xs(len(sspine_slots), width)

    def _row_step(xs: list[float]) -> float:
        if len(xs) <= 1:
            return 120.0
        return xs[1] - xs[0]

    step_leaf = _row_step(leaf_xs)
    step_spine = _row_step(spine_xs) if spine_xs else 120.0
    step_ss = _row_step(sspine_xs) if sspine_xs else 120.0

    if zoom_fit:
        fs = 8
        fs_sm = 7
        fs_mid = 8
        fs_bot = 9
        fs_cable = 7
        spine_hw = min(62.0, max(10.0, step_spine * 0.38))
        leaf_hw = min(62.0, max(10.0, step_leaf * 0.38))
        ss_hw = min(64.0, max(10.0, step_ss * 0.38))
        spine_hh = max(14.0, 36.0 * (spine_hw / 62.0))
        leaf_hh = max(14.0, 36.0 * (leaf_hw / 62.0))
        ss_hh = max(14.0, 36.0 * (ss_hw / 64.0))
        node_w = min(82.0, max(20.0, step_leaf * 0.78))
        node_h = max(14.0, min(50.0, node_w * 0.55))
        node_gap = max(2.0, 6.0 * (node_w / 82.0))
        ell_r = 2.0
    else:
        fs = 12
        fs_sm = 11
        fs_mid = 12
        fs_bot = 13
        fs_cable = 12
        spine_hw = 62.0
        leaf_hw = 62.0
        ss_hw = 64.0
        spine_hh = 36.0
        leaf_hh = 36.0
        ss_hh = 36.0
        node_w = 82.0
        node_h = 50.0
        node_gap = 6.0
        ell_r = 3.0

    # Keep link strokes and opacity aligned with detail view so fit mode stays vivid.
    stroke_super = 1.0
    stroke_spine_leaf = 1.0
    stroke_leaf_node = 1.2
    link_op_super = 0.95 if zoom_fit else 0.6
    link_op_sl = 0.95 if zoom_fit else 0.7

    spine_rect_w = spine_hw * 2
    leaf_rect_w = leaf_hw * 2
    ss_rect_w = ss_hw * 2
    box_rx = min(6.0, max(2.0, leaf_hh * 0.17))

    # Node row geometry (needed for viewBox + layer labels before `parts`).
    gpus_per_node = result.inputs.gpus_per_node
    drawn_leaf_count = sum(1 for s in leaf_slots if s is not ELLIPSIS) or 1
    real_nodes_per_leaf = max(1, math.ceil(plane.gpus_per_leaf / gpus_per_node))
    if zoom_fit:
        node_nic_cap = max(6, min(real_nodes_per_leaf, max(1, 5200 // drawn_leaf_count)))
    else:
        node_nic_cap = _DIAGRAM_MAX_NODE_ICONS // drawn_leaf_count
    nodes_per_leaf_draw = max(1, min(real_nodes_per_leaf, node_nic_cap))
    show_node_ellipsis = real_nodes_per_leaf > nodes_per_leaf_draw
    leaf_nic_dash = ' stroke-dasharray="4 3"' if plane.leaf_breakout > 1 else ""

    min_node_row_left = float(width)
    for slot, lx in zip(leaf_slots, leaf_xs):
        if slot is ELLIPSIS:
            continue
        slots_in_group = nodes_per_leaf_draw + (1 if show_node_ellipsis else 0)
        group_width = node_w * slots_in_group + node_gap * (slots_in_group - 1)
        start_x = lx - group_width / 2
        min_node_row_left = min(min_node_row_left, start_x)
    if min_node_row_left >= float(width):
        min_node_row_left = 90.0

    label_rights: list[float] = []
    if has_super and sspine_xs:
        label_rights.append(_layer_label_anchor_x(min(sspine_xs) - ss_hw))
    if not single_switch and spine_xs:
        label_rights.append(_layer_label_anchor_x(min(spine_xs) - spine_hw))
    if leaf_xs:
        label_rights.append(_layer_label_anchor_x(min(leaf_xs) - leaf_hw))
    label_rights.append(_layer_label_anchor_x(min_node_row_left))

    vb_x0 = 0.0
    if label_rights:
        vb_x0 = min(0.0, min(label_rights) - _LAYER_LABEL_EST_WIDTH)
    vb_x0_i = int(math.floor(vb_x0))
    view_w = width - vb_x0_i

    super_label_rx = (
        _layer_label_anchor_x(min(sspine_xs) - ss_hw) if (has_super and sspine_xs) else None
    )
    spine_label_rx = (
        _layer_label_anchor_x(min(spine_xs) - spine_hw)
        if (not single_switch and spine_xs)
        else None
    )
    leaf_label_rx = _layer_label_anchor_x(min(leaf_xs) - leaf_hw) if leaf_xs else None
    nodes_label_rx = _layer_label_anchor_x(min_node_row_left)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb_x0_i} 0 {view_w} {height}" '
        f'font-family="Inter, system-ui, sans-serif" font-size="{fs}">',
        "<defs>"
        '<linearGradient id="sspineGrad" x1="0" x2="0" y1="0" y2="1">'
        '<stop offset="0" stop-color="#4a1d96"/><stop offset="1" stop-color="#7c3aed"/>'
        "</linearGradient>"
        '<linearGradient id="spineGrad" x1="0" x2="0" y1="0" y2="1">'
        '<stop offset="0" stop-color="#1e3a8a"/><stop offset="1" stop-color="#2563eb"/>'
        "</linearGradient>"
        '<linearGradient id="leafGrad" x1="0" x2="0" y1="0" y2="1">'
        '<stop offset="0" stop-color="#065f46"/><stop offset="1" stop-color="#10b981"/>'
        "</linearGradient>"
        '<linearGradient id="nodeGrad" x1="0" x2="0" y1="0" y2="1">'
        '<stop offset="0" stop-color="#7c2d12"/><stop offset="1" stop-color="#ea580c"/>'
        "</linearGradient>"
        "</defs>",
    ]

    # ---------- Super-spine row -------------------------------------------
    if has_super:
        # spine <-> super-spine links
        for sx_idx, sx in enumerate(sspine_xs):
            if sspine_slots[sx_idx] is ELLIPSIS:
                continue
            for sp_idx, spx in enumerate(spine_xs):
                if spine_slots[sp_idx] is ELLIPSIS:
                    continue
                parts.append(
                    f'<line x1="{sx}" y1="{super_y + ss_hh}" x2="{spx}" y2="{spine_y}" '
                    f'stroke="#a78bfa" stroke-width="{stroke_super}" opacity="{link_op_super}"'
                    + (
                        ' stroke-dasharray="4 3"'
                        if plane.spine_to_super_fanout > 1
                        or plane.super_to_spine_fanout > 1
                        else ""
                    )
                    + "/>"
                )

        # super-spine boxes / ellipsis
        for slot, sx in zip(sspine_slots, sspine_xs):
            if slot is ELLIPSIS:
                parts.append(_ellipsis_dots(sx, super_y + ss_hh / 2, "#4a1d96", ell_r))
                continue
            label = f"S{slot + 1}" if zoom_fit else f"S-Spine {slot + 1}"
            ty1 = super_y + ss_hh * 0.44
            ty2 = super_y + ss_hh * 0.82
            parts.append(
                f'<rect x="{sx - ss_hw}" y="{super_y}" width="{ss_rect_w}" height="{ss_hh}" '
                f'rx="{box_rx}" fill="url(#sspineGrad)" stroke="#4a1d96"/>'
                f'<text x="{sx}" y="{ty1}" text-anchor="middle" fill="white" '
                f'font-weight="600" font-size="{fs}">{label}</text>'
                f'<text x="{sx}" y="{ty2}" text-anchor="middle" fill="#ddd6fe" '
                f'font-size="{fs_sm}">{result.inputs.super_spine_ports}-p @ '
                f"{_fmt_speed(plane.super_spine_speed)}</text>"
            )

    # ---------- Spine <-> leaf links --------------------------------------
    if not single_switch:
        sl_dash = (
            ' stroke-dasharray="4 3"'
            if plane.leaf_to_spine_fanout > 1 or plane.spine_to_leaf_fanout > 1
            else ""
        )
        for sp_idx, spx in enumerate(spine_xs):
            if spine_slots[sp_idx] is ELLIPSIS:
                continue
            for lf_idx, lx in enumerate(leaf_xs):
                if leaf_slots[lf_idx] is ELLIPSIS:
                    continue
                parts.append(
                    f'<line x1="{spx}" y1="{spine_y + spine_hh}" x2="{lx}" y2="{leaf_y}" '
                    f'stroke="#60a5fa" stroke-width="{stroke_spine_leaf}" opacity="{link_op_sl}"{sl_dash}/>'
                )

        # ---------- Spine row ---------------------------------------------
        for slot, sx in zip(spine_slots, spine_xs):
            if slot is ELLIPSIS:
                parts.append(_ellipsis_dots(sx, spine_y + spine_hh / 2, "#1e3a8a", ell_r))
                continue
            sty1 = spine_y + spine_hh * 0.44
            sty2 = spine_y + spine_hh * 0.82
            sp_lbl = f"Sp{slot + 1}" if zoom_fit else f"Spine {slot + 1}"
            parts.append(
                f'<rect x="{sx - spine_hw}" y="{spine_y}" width="{spine_rect_w}" height="{spine_hh}" '
                f'rx="{box_rx}" fill="url(#spineGrad)" stroke="#1e3a8a"/>'
                f'<text x="{sx}" y="{sty1}" text-anchor="middle" fill="white" '
                f'font-weight="600" font-size="{fs}">{sp_lbl}</text>'
                f'<text x="{sx}" y="{sty2}" text-anchor="middle" fill="#bfdbfe" '
                f'font-size="{fs_sm}">{result.inputs.spine_ports}-p @ '
                f"{_fmt_speed(plane.spine_speed)}</text>"
            )

    # ---------- Leaf row --------------------------------------------------
    for slot, lx in zip(leaf_slots, leaf_xs):
        if slot is ELLIPSIS:
            parts.append(_ellipsis_dots(lx, leaf_y + leaf_hh / 2, "#065f46", ell_r))
            continue
        lty1 = leaf_y + leaf_hh * 0.44
        lty2 = leaf_y + leaf_hh * 0.82
        lf_lbl = f"Lf{slot + 1}" if zoom_fit else f"Leaf {slot + 1}"
        parts.append(
            f'<rect x="{lx - leaf_hw}" y="{leaf_y}" width="{leaf_rect_w}" height="{leaf_hh}" '
            f'rx="{box_rx}" fill="url(#leafGrad)" stroke="#065f46"/>'
            f'<text x="{lx}" y="{lty1}" text-anchor="middle" fill="white" '
            f'font-weight="600" font-size="{fs}">{lf_lbl}</text>'
            f'<text x="{lx}" y="{lty2}" text-anchor="middle" fill="#a7f3d0" '
            f'font-size="{fs_sm}">{result.inputs.leaf_ports}-p @ '
            f"{_fmt_speed(plane.leaf_speed)}</text>"
        )

    # ---------- Node row --------------------------------------------------
    # Icon cap depends on zoom mode (see nodes_per_leaf_draw above).
    rail_node_cx: Optional[float] = None
    for slot, lx in zip(leaf_slots, leaf_xs):
        if slot is ELLIPSIS:
            parts.append(_ellipsis_dots(lx, node_y + node_h / 2, "#7c2d12", ell_r))
            continue
        slots_in_group = nodes_per_leaf_draw + (1 if show_node_ellipsis else 0)
        group_width = node_w * slots_in_group + node_gap * (slots_in_group - 1)
        start_x = lx - group_width / 2
        for j in range(nodes_per_leaf_draw):
            nx = start_x + j * (node_w + node_gap)
            ncx = nx + node_w / 2
            if result.inputs.rail_design and slot == 0 and j == 0:
                rail_node_cx = ncx
            nty1 = node_y + node_h * 0.34
            nty2 = node_y + node_h * 0.62
            nty3 = node_y + node_h * 0.88
            nic_fs = max(6, fs_sm - 2) if zoom_fit else 10
            node_lbl = "Node 1" if slot == 0 and j == 0 else "Node"
            draw_leaf_downlink = not (
                result.inputs.rail_design
                and not single_switch
                and slot == 0
                and j == 0
            )
            line_el = ""
            if draw_leaf_downlink:
                line_el = (
                    f'<line x1="{lx}" y1="{leaf_y + leaf_hh}" x2="{ncx}" y2="{node_y}" '
                    f'stroke="#f97316" stroke-width="{stroke_leaf_node}"{leaf_nic_dash}/>'
                )
            parts.append(
                line_el
                + f'<rect x="{nx}" y="{node_y}" width="{node_w}" height="{node_h}" rx="{box_rx}" '
                + f'fill="url(#nodeGrad)" stroke="#7c2d12"/>'
                + f'<text x="{ncx}" y="{nty1}" text-anchor="middle" fill="white" '
                + f'font-weight="600" font-size="{fs}">{node_lbl}</text>'
                + f'<text x="{ncx}" y="{nty2}" text-anchor="middle" fill="#fed7aa" '
                + f'font-size="{fs_sm}">{gpus_per_node} GPUs</text>'
                + f'<text x="{ncx}" y="{nty3}" text-anchor="middle" fill="#fed7aa" '
                + f'font-size="{nic_fs}">{result.inputs.nics_per_gpu}x{_fmt_speed(result.inputs.nic_speed)} NIC ({"single-plan" if result.inputs.plans_per_nic == 0 else f"{result.inputs.plans_per_nic}x{_fmt_speed(plane.nic_speed)}"})</text>'
            )
        if show_node_ellipsis:
            ex = start_x + nodes_per_leaf_draw * (node_w + node_gap) + node_w / 2
            parts.append(_ellipsis_dots(ex, node_y + node_h / 2, "#7c2d12", ell_r))

    # ---------- Layer labels & annotations --------------------------------
    ly_super = super_y + ss_hh * 0.55 if has_super else 0
    ly_spine = spine_y + spine_hh * 0.55 if not single_switch else 0
    ly_leaf = leaf_y + leaf_hh * 0.55
    ly_nodes = node_y + node_h * 0.48
    if has_super and super_label_rx is not None:
        parts.append(
            f'<text x="{super_label_rx}" y="{ly_super}" text-anchor="end" '
            f'fill="#4a1d96" font-weight="700" font-size="{fs_mid}">SUPER-SPINE</text>'
        )
    if not single_switch and spine_label_rx is not None:
        parts.append(
            f'<text x="{spine_label_rx}" y="{ly_spine}" text-anchor="end" '
            f'fill="#1e3a8a" font-weight="700" font-size="{fs_mid}">SPINE</text>'
        )
    if leaf_label_rx is not None:
        parts.append(
            f'<text x="{leaf_label_rx}" y="{ly_leaf}" text-anchor="end" '
            f'fill="#065f46" font-weight="700" font-size="{fs_mid}">LEAF</text>'
        )
    parts.append(
        f'<text x="{nodes_label_rx}" y="{ly_nodes}" text-anchor="end" '
        f'fill="#7c2d12" font-weight="700" font-size="{fs_mid}">NODES</text>'
    )

    if has_super:
        e2e = min(plane.spine_speed, plane.super_spine_speed)
        bk = ""
        if plane.spine_to_super_fanout > 1:
            bk = f" (spine {plane.spine_to_super_fanout}:1 breakout)"
        elif plane.super_to_spine_fanout > 1:
            bk = f" (super-spine {plane.super_to_spine_fanout}:1 breakout)"
        parts.append(
            f'<text x="{width / 2}" y="{(super_y + spine_y) / 2}" text-anchor="middle" '
            f'fill="#4a1d96" font-size="{fs_mid}">{_fmt_speed(e2e)} super-spine &#8596; spine{bk}</text>'
        )

    if not single_switch:
        e2e_spine_leaf = min(plane.leaf_speed, plane.spine_speed)
        if plane.leaf_to_spine_fanout > 1:
            sl_note = f" (leaf {plane.leaf_to_spine_fanout}:1 breakout)"
        elif plane.spine_to_leaf_fanout > 1:
            sl_note = f" (spine {plane.spine_to_leaf_fanout}:1 breakout)"
        else:
            sl_note = ""
        parts.append(
            f'<text x="{width / 2}" y="{(spine_y + leaf_y) / 2}" text-anchor="middle" '
            f'fill="#1e3a8a" font-size="{fs_mid}">{_fmt_speed(e2e_spine_leaf)} spine &#8596; leaf{sl_note}</text>'
        )

    leaf_nic_note = (
        f" (leaf {plane.leaf_breakout}:1 breakout)" if plane.leaf_breakout > 1 else ""
    )
    plan_note = (
        f" &#183; plan 1 of {result.num_planes}" if result.num_planes > 1 else ""
    )
    parts.append(
        f'<text x="{width / 2}" y="{(leaf_y + node_y) / 2 + 10}" text-anchor="middle" '
        f'fill="#7c2d12" font-size="{fs_mid}">{_fmt_speed(plane.nic_speed)} to GPU NICs'
        f"{leaf_nic_note}{plan_note}</text>"
    )

    # Pod annotation for 3-tier
    if has_super and plane.pods_per_plane > 1:
        parts.append(
            f'<text x="{width / 2}" y="{leaf_y - 50}" text-anchor="middle" '
            f'fill="#065f46" font-size="{fs_mid}" font-weight="600">'
            f"Showing 1 of {plane.pods_per_plane} pods "
            f"({plane.leaves_per_pod} leaves + {plane.spines_per_pod} spines each)"
            f"</text>"
        )

    # Bottom summary strip: counts + cables
    ss_part = (
        f" &#183; {result.total_super_spines} super-spines"
        if result.total_super_spines
        else ""
    )
    sp_part = f" &#183; {result.total_spines} spines" if result.total_spines else ""
    parts.append(
        f'<text x="{width / 2}" y="{height - 36}" text-anchor="middle" '
        f'fill="#334155" font-size="{fs_bot}" font-weight="600">'
        f"{result.total_nodes} nodes &#183; {result.inputs.num_gpus} GPUs "
        f"&#183; {result.total_leaves} leaves"
        f"{sp_part}{ss_part} &#183; {result.num_planes} plan(s)</text>"
    )
    if result.cables:
        cable_text = " &#183; ".join(
            f"{c.end_a}&#8596;{c.end_b} x {c.count:,} ({c.label})"
            for c in result.cables
        )
        parts.append(
            f'<text x="{width / 2}" y="{height - 14}" text-anchor="middle" '
            f'fill="#475569" font-size="{fs_cable}">Cables: {cable_text}</text>'
        )

    if (
        result.inputs.rail_design
        and rail_node_cx is not None
        and not single_switch
        and leaf_slots
        and leaf_slots[0] == 0
    ):
        leaf_cx_by_index: dict[int, float] = {}
        leaf_ellipsis_cx: Optional[float] = None
        for slot, lx in zip(leaf_slots, leaf_xs):
            if slot is ELLIPSIS:
                leaf_ellipsis_cx = lx
                continue
            if isinstance(slot, int):
                leaf_cx_by_index[slot] = lx

        phantom_leaf_xs = _xs(draw_leaves_count, width)
        rail_n = min(gpus_per_node, draw_leaves_count)
        ay1 = node_y
        ay2 = leaf_y + leaf_hh
        missing_for_ellipsis: list[int] = [
            ri for ri in range(rail_n) if ri not in leaf_cx_by_index
        ]
        ellipsis_offsets = _rail_ellipsis_fan_offsets(len(missing_for_ellipsis))
        if rail_n >= 1:
            parts.append('<g class="rail-links" pointer-events="none">')
            sw_rail = max(0.75, stroke_leaf_node * 0.9)
            for ri in range(rail_n):
                if ri in leaf_cx_by_index:
                    tx = leaf_cx_by_index[ri]
                    split_x = leaf_ellipsis_cx
                elif leaf_ellipsis_cx is not None:
                    k = missing_for_ellipsis.index(ri)
                    tx = leaf_ellipsis_cx + ellipsis_offsets[k]
                    split_x = None
                else:
                    tx = phantom_leaf_xs[ri]
                    split_x = leaf_ellipsis_cx
                for x1, y1, x2, y2, op in _rail_line_segments(
                    rail_node_cx, ay1, tx, ay2, split_x
                ):
                    parts.append(
                        f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#9a3412" '
                        f'stroke-width="{sw_rail}" stroke-dasharray="3 5" opacity="{op}" />'
                    )
            parts.append("</g>")

    parts.append("</svg>")
    return "".join(parts)


def _ellipsis_dots(cx: float, cy: float, color: str, r: float = 3) -> str:
    gap = max(r * 3.5, 8.0)
    return (
        f'<g fill="{color}">'
        f'<circle cx="{cx - gap}" cy="{cy}" r="{r}"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}"/>'
        f'<circle cx="{cx + gap}" cy="{cy}" r="{r}"/>'
        "</g>"
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

DEFAULTS = dict(
    num_gpus=1024,
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
    leaf_spine_ratio="1:1",
    spine_super_ratio="1:1",
    match_interface_speed_to_nic=False,
)

LEAF_SPINE_RATIOS = ("1:1", "1:1.1", "1:1.16", "1:1.20")
PLANS_PER_NIC_OPTIONS = (0, 1, 2, 4)


def _build_plan_comparison(form: dict) -> list[dict[str, str | int]]:
    """Compare plans and match-to-NIC for the selected leaf-to-spine ratio."""
    rows: list[dict[str, str | int]] = []
    for match_option, match_value in ((1, False), (2, True)):
        for plans_per_nic in PLANS_PER_NIC_OPTIONS:
            compare_form = dict(form)
            compare_form["plans_per_nic"] = plans_per_nic
            compare_form["match_interface_speed_to_nic"] = match_value
            compare_input = DesignInputs(**compare_form)
            compare_result = design_fabric(compare_input)
            total_cables = sum(c.count for c in compare_result.cables)
            cable_breakdown = (
                ", ".join(
                    f"{c.end_a}-{c.end_b}: {c.count:,}"
                    for c in compare_result.cables
                )
                or "-"
            )
            rows.append(
                {
                    "option": match_option,
                    "match_interface_speed_to_nic": (
                        "Yes" if match_value else "No"
                    ),
                    "plans_per_nic": plans_per_nic,
                    "feasible": "Yes" if compare_result.feasible else "No",
                    "topology": compare_result.topology,
                    "leaf_switches": compare_result.total_leaves,
                    "spine_switches": compare_result.total_spines,
                    "super_spine_switches": compare_result.total_super_spines,
                    "total_cables": total_cables,
                    "cable_breakdown": cable_breakdown,
                }
            )
    return rows


@app.route("/", methods=["GET", "POST"])
def index():
    form = dict(DEFAULTS)
    result = None
    svg_detail = None
    svg_fit = None
    error = None
    compare_rows = None
    selected_action = "design"

    if request.method == "POST":
        try:
            selected_action = request.form.get("action", "design")
            form = dict(
                num_gpus=int(request.form["num_gpus"]),
                gpus_per_node=int(request.form["gpus_per_node"]),
                nics_per_gpu=int(request.form["nics_per_gpu"]),
                spine_ports=int(request.form["spine_ports"]),
                super_spine_ports=int(
                    request.form.get("super_spine_ports", request.form["spine_ports"])
                ),
                leaf_ports=int(request.form["leaf_ports"]),
                nic_speed=int(request.form["nic_speed"]),
                leaf_speed=int(request.form["leaf_speed"]),
                spine_speed=int(request.form["spine_speed"]),
                super_spine_speed=int(request.form.get("super_spine_speed", 0)),
                # Accept legacy name "plans" from older cached HTML
                plans_per_nic=int(
                    request.form.get("plans_per_nic") or request.form.get("plans") or 0
                ),
                rail_design=request.form.get("rail_design") == "on",
                leaf_spine_ratio=request.form.get("leaf_spine_ratio", "1:1"),
                spine_super_ratio=request.form.get("spine_super_ratio", "1:1"),
                match_interface_speed_to_nic=(
                    request.form.get("match_interface_speed_to_nic", "no") == "yes"
                ),
            )
            if form["num_gpus"] <= 0:
                raise ValueError("Number of GPUs must be positive.")
            if form["gpus_per_node"] <= 0:
                raise ValueError("GPUs per node must be positive.")
            if form["nics_per_gpu"] not in (1, 2, 3):
                raise ValueError("NICs per GPU must be 1, 2 or 3.")
            if (
                form["spine_ports"] <= 1
                or form["super_spine_ports"] <= 1
                or form["leaf_ports"] <= 1
            ):
                raise ValueError("Port counts must be > 1.")
            if form["nic_speed"] not in (400, 800):
                raise ValueError("NIC speed must be 400G or 800G.")
            if form["leaf_speed"] not in (400, 800, 1600):
                raise ValueError("Leaf port speed must be 400G, 800G or 1.6T.")
            if form["spine_speed"] not in (400, 800, 1600):
                raise ValueError("Spine port speed must be 400G, 800G or 1.6T.")
            if form["super_spine_speed"] not in (0, 800, 1600):
                raise ValueError(
                    "Super-spine speed must be 800G or 1.6T (or disabled)."
                )
            if form["plans_per_nic"] not in (0, 1, 2, 4):
                raise ValueError("Plans per NIC must be 0, 1, 2, or 4.")
            allowed_ratios = set(LEAF_SPINE_RATIOS)
            if form["leaf_spine_ratio"] not in allowed_ratios:
                raise ValueError(
                    "Leaf-to-spine ratio must be one of: 1:1, 1:1.1, 1:1.16, 1:1.20."
                )
            if form["spine_super_ratio"] not in allowed_ratios:
                raise ValueError(
                    "Spine-to-super-spine ratio must be one of: 1:1, 1:1.1, 1:1.16, 1:1.20."
                )
            if not isinstance(form["match_interface_speed_to_nic"], bool):
                raise ValueError("Match interface speed to NIC must be yes or no.")
            if (
                form["plans_per_nic"] > 0
                and form["nic_speed"] % form["plans_per_nic"] != 0
            ):
                raise ValueError("NIC speed must be divisible by plans per NIC.")

            inp = DesignInputs(**form)
            result = design_fabric(inp)
            svg_detail = render_svg(result, "detail")
            svg_fit = render_svg(result, "fit")
            if selected_action == "compare":
                compare_rows = _build_plan_comparison(form)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

    return render_template(
        "index.html",
        form=form,
        result=result,
        svg_detail=svg_detail,
        svg_fit=svg_fit,
        error=error,
        compare_rows=compare_rows,
        selected_action=selected_action,
    )


if __name__ == "__main__":
    host = "0.0.0.0"
    listening_port = "5000"
    debug = False
    browser_only = "False"

    def run_flask():
        app.run(
            host="0.0.0.0",
            port=listening_port,
            debug=debug,
            use_reloader=False,
            threaded=True,
        )

    if browser_only == "True":
        app.run(host=host, port=listening_port, debug=debug, use_reloader=False)
    else:
        threading.Thread(target=run_flask, daemon=True).start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{listening_port}/", timeout=0.25
                )
                break
            except OSError:
                time.sleep(0.05)
        webview.create_window(
            "AI Cable Calculator", f"http://127.0.0.1:{listening_port}"
        )
        webview.start()
