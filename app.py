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
import webview
import threading
import time
import urllib.request
import math
from dataclasses import dataclass, field
from flask import Flask, render_template, request

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Design logic
# ---------------------------------------------------------------------------

@dataclass
class DesignInputs:
    num_gpus: int
    gpus_per_node: int
    nics_per_gpu: int            # 1, 2, 3 -> NICs per GPU
    spine_ports: int
    leaf_ports: int
    nic_speed: int               # 400 or 800
    leaf_speed: int              # 400, 800 or 1600
    spine_speed: int             # 400, 800 or 1600
    super_spine_speed: int = 0   # 0 = not used, else 800 or 1600
    plans_per_nic: int = 1       # 1, 2, or 4; NIC breakout: 1x, 2x, or 4x


@dataclass
class PlaneDesign:
    # Speeds and breakouts
    nic_speed: int               # Effective NIC link speed per plan
    nic_speed_raw: int           # Physical NIC port speed before plans_per_nic split
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
    gpus_per_plane: int
    leaves_per_plane: int

    # 2-tier specifics (also used inside each pod in 3-tier)
    spines_per_plane: int                 # total spines in the plane
    links_per_leaf_to_each_spine: int
    spine_ports_used_for_leaves: int      # per spine

    # 3-tier specifics
    uses_super_spine: bool = False
    leaves_per_pod: int = 0
    spines_per_pod: int = 0
    pods_per_plane: int = 0
    spine_ports_up_to_super: int = 0      # per spine
    super_spines_per_plane: int = 0
    ports_used_per_super_spine: int = 0

    oversubscription: str = "1:1"


@dataclass
class CableGroup:
    count: int            # total physical cables (summed across planes)
    label: str            # e.g. "800G-2x400G" or "800G-800G"
    end_a: str            # e.g. "Leaf"
    end_b: str            # e.g. "Node"


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
    topology: str = "spine-leaf"     # "single-switch", "spine-leaf", "3-tier"


def _compute_fanouts(a: int, b: int) -> tuple[int, int]:
    """Given two port speeds a, b (Gbps), return (a_to_b_fanout, b_to_a_fanout)
    where the faster side is assumed to use breakout cables.
    Only one of the two values will be >1.
    """
    if a >= b:
        return max(1, a // b), 1
    return 1, max(1, b // a)


def _cable_label(speed_a: int, fanout_a_to_b: int,
                 speed_b: int, fanout_b_to_a: int) -> str:
    """Build a cable-type label like '800G-800G' or '800G-2x400G'."""
    a = _fmt_speed(speed_a)
    b = _fmt_speed(speed_b)
    if fanout_a_to_b > 1:
        return f"{a}-{fanout_a_to_b}x{b}"
    if fanout_b_to_a > 1:
        return f"{fanout_b_to_a}x{a}-{b}"
    return f"{a}-{b}"


def _cable_count(total_links: int, fanout_a_to_b: int, fanout_b_to_a: int) -> int:
    """Number of physical cables carrying `total_links` logical links,
    given breakout fanouts between the two ends.
    """
    per_cable = max(1, fanout_a_to_b, fanout_b_to_a)
    return math.ceil(total_links / per_cable)


def design_fabric(inp: DesignInputs) -> DesignResult:
    notes: list[str] = []

    num_planes = inp.plans_per_nic * inp.nics_per_gpu
    nic_plan_speed = inp.nic_speed // inp.plans_per_nic

    if inp.nic_speed % inp.plans_per_nic != 0:
        return _infeasible(
            inp,
            notes + [
                f"NIC speed ({inp.nic_speed}G) must be divisible by plans_per_nic ({inp.plans_per_nic})."
            ],
        )

    # --- Validate speeds --------------------------------------------------
    if inp.leaf_speed < nic_plan_speed or inp.leaf_speed % nic_plan_speed != 0:
        return _infeasible(
            inp,
            notes + ["Leaf port speed must be >= NIC speed and an integer multiple of it."],
        )

    # Distribute GPUs across parallel fabrics first, then size each.
    gpus_per_plane = math.ceil(inp.num_gpus / num_planes)

    # Breakouts
    leaf_breakout = inp.leaf_speed // nic_plan_speed
    leaf_to_spine_fanout, spine_to_leaf_fanout = _compute_fanouts(
        inp.leaf_speed, inp.spine_speed
    )

    # --- Plans note -------------------------------------------------------
    notes.append(
        f"Parallel fabrics: plans_per_nic x NICs_per_GPU = "
        f"{inp.plans_per_nic} x {inp.nics_per_gpu} = {num_planes} plan(s). "
        f"NIC breakout per GPU NIC: 1x{inp.nic_speed}G -> "
        f"{inp.plans_per_nic}x{nic_plan_speed}G. "
        f"GPUs split before sizing: {inp.num_gpus} total / {num_planes} = "
        f"{gpus_per_plane} GPUs per plan (rounded up)."
    )

    # --- Single-switch short-circuit ------------------------------------
    # If all GPU NICs in a plane fit on one leaf switch using every port as a
    # downlink (with breakout), a spine layer is unnecessary - a single leaf
    # terminates the whole plane.
    max_gpus_one_switch = inp.leaf_ports * leaf_breakout
    if gpus_per_plane <= max_gpus_one_switch:
        return _single_switch_result(inp, num_planes, gpus_per_plane,
                                     leaf_breakout, notes)

    # --- Leaf port split for 1:1 ----------------------------------------
    downlink_ports = inp.leaf_ports // 2
    uplink_ports = inp.leaf_ports - downlink_ports
    if downlink_ports == 0 or uplink_ports == 0:
        return _infeasible(inp, notes + ["Leaf radix too small to split."])

    gpus_per_leaf = downlink_ports * leaf_breakout
    leaves_per_plane = math.ceil(gpus_per_plane / gpus_per_leaf)

    # --- 2-tier sizing (first-pass) -------------------------------------
    links_per_leaf = uplink_ports * leaf_to_spine_fanout

    # Classic: one link per (leaf, spine) pair -> `links_per_leaf` spines
    spines_per_plane = links_per_leaf
    links_per_leaf_to_each_spine = 1
    spine_ports_used = math.ceil(leaves_per_plane / spine_to_leaf_fanout)

    # Try link bundling to reduce spine count while respecting spine radix
    for b in range(links_per_leaf, 1, -1):
        if links_per_leaf % b:
            continue
        candidate = math.ceil((leaves_per_plane * b) / spine_to_leaf_fanout)
        if candidate <= inp.spine_ports:
            if b > 1:
                links_per_leaf_to_each_spine = b
                spines_per_plane = links_per_leaf // b
                spine_ports_used = candidate
            break

    two_tier_ok = spine_ports_used <= inp.spine_ports
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
            f"(leaves={leaves_per_plane}, each spine uses {spine_ports_used}/"
            f"{inp.spine_ports} ports)."
        )
        if inp.super_spine_speed:
            notes.append(
                f"Super-spine ({inp.super_spine_speed}G) not required at this "
                "scale - not included in the design."
            )
        _add_common_notes(notes, inp, leaf_breakout, leaf_to_spine_fanout,
                          spine_to_leaf_fanout, downlink_ports, uplink_ports,
                          gpus_per_leaf, num_planes)
        plane = PlaneDesign(**plane_kwargs)
        return DesignResult(
            inputs=inp, num_planes=num_planes, plane=plane,
            total_leaves=leaves_per_plane * num_planes,
            total_spines=spines_per_plane * num_planes,
            total_super_spines=0,
            total_nodes=math.ceil(inp.num_gpus / inp.gpus_per_node),
            cables=_compute_cables(inp, plane, num_planes),
            notes=notes, feasible=True,
            topology="spine-leaf",
        )

    # 2-tier not sufficient.
    if inp.super_spine_speed == 0:
        notes.append(
            f"2-tier infeasible: a single spine would need {spine_ports_used} "
            f"ports to accept one link from each of {leaves_per_plane} leaves "
            f"(after {spine_to_leaf_fanout}:1 breakout), but spines only have "
            f"{inp.spine_ports} ports. Enable a super-spine tier to continue."
        )
        _add_common_notes(notes, inp, leaf_breakout, leaf_to_spine_fanout,
                          spine_to_leaf_fanout, downlink_ports, uplink_ports,
                          gpus_per_leaf, num_planes)
        plane = PlaneDesign(**plane_kwargs)
        return DesignResult(
            inputs=inp, num_planes=num_planes, plane=plane,
            total_leaves=leaves_per_plane * num_planes,
            total_spines=0,
            total_super_spines=0,
            total_nodes=math.ceil(inp.num_gpus / inp.gpus_per_node),
            notes=notes, feasible=False,
        )

    # --- 3-tier (super-spine) sizing ------------------------------------
    # Fat-tree-style: each spine uses half its ports down to leaves, half up
    # to the super-spine layer (1:1 bandwidth across the spine).
    spine_ports_down = inp.spine_ports // 2
    spine_ports_up = inp.spine_ports - spine_ports_down
    leaves_per_pod = spine_ports_down * spine_to_leaf_fanout

    # Each pod uses a full spine-leaf mesh without bundling.
    spines_per_pod = links_per_leaf   # = uplink_ports * leaf_to_spine_fanout
    pods_per_plane = math.ceil(leaves_per_plane / leaves_per_pod)
    spines_per_plane_3t = spines_per_pod * pods_per_plane

    # Super-spine layer: size by aggregate bandwidth (fat-tree Clos). Each
    # super-spine fully uses its ports toward the spine layer; we compute the
    # number of super-spines needed to absorb all spine uplinks. Spines do
    # not need to fully mesh with every super-spine - Clos non-blocking
    # holds as long as aggregate capacity and path diversity are sufficient.
    spine_to_super_fanout, super_to_spine_fanout = _compute_fanouts(
        inp.spine_speed, inp.super_spine_speed
    )
    total_spine_super_links = spines_per_plane_3t * spine_ports_up * spine_to_super_fanout
    links_absorbed_per_super = inp.spine_ports * super_to_spine_fanout
    super_spines_per_plane = max(1, math.ceil(
        total_spine_super_links / links_absorbed_per_super
    ))
    ports_used_per_super = inp.spine_ports  # all super-spine ports used toward spine layer

    # Each spine should be able to reach at least `super_spines_per_plane`
    # super-spines (one link each) for path diversity; fewer works with
    # bundling. We flag infeasibility only if a spine can't even fan out
    # at one link per super-spine that holds it.
    spine_reach = spine_ports_up * spine_to_super_fanout
    feasible = spine_reach >= 1 and super_spines_per_plane >= 1

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
        f"{super_spines_per_plane} super-spines @ {inp.super_spine_speed}G."
    )
    notes.append(
        f"Each spine splits its {inp.spine_ports} ports as "
        f"{spine_ports_down} down (to pod leaves) + {spine_ports_up} up "
        "(to super-spines) for 1:1 through the spine."
    )
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
    links_per_spine_super_pair = max(
        1, spine_reach // super_spines_per_plane
    ) if super_spines_per_plane else 0
    notes.append(
        f"Super-spine sizing: {spines_per_plane_3t} spines x {spine_ports_up} "
        f"uplink ports = {total_spine_super_links} links absorbed by "
        f"{super_spines_per_plane} super-spine(s) at "
        f"{links_absorbed_per_super} links each "
        f"(~{links_per_spine_super_pair} link(s) per spine-super pair)."
    )

    _add_common_notes(notes, inp, leaf_breakout, leaf_to_spine_fanout,
                      spine_to_leaf_fanout, downlink_ports, uplink_ports,
                      gpus_per_leaf, num_planes)

    return DesignResult(
        inputs=inp, num_planes=num_planes, plane=plane,
        total_leaves=leaves_per_plane * num_planes,
        total_spines=spines_per_plane_3t * num_planes,
        total_super_spines=super_spines_per_plane * num_planes,
        total_nodes=math.ceil(inp.num_gpus / inp.gpus_per_node),
        cables=_compute_cables(inp, plane, num_planes),
        notes=notes, feasible=feasible,
        topology="3-tier",
    )


def _single_switch_result(inp: DesignInputs, num_planes: int,
                          gpus_per_plane: int, leaf_breakout: int,
                          notes: list[str]) -> DesignResult:
    """All GPU NICs in a plane fit on one leaf. No spine layer required."""
    downlink_ports_used = math.ceil(gpus_per_plane / leaf_breakout)
    plane = PlaneDesign(
        nic_speed_raw=inp.nic_speed,
        nic_speed=inp.nic_speed // inp.plans_per_nic,
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

    leaf_to_nic_cables = _cable_count(inp.num_gpus, leaf_breakout, 1)
    leaf_to_nic_label = _cable_label(inp.leaf_speed, leaf_breakout,
                                     inp.nic_speed, 1)
    cables = [
        CableGroup(leaf_to_nic_cables, leaf_to_nic_label, "Leaf", "Node"),
    ]

    notes.append(
        f"Single-switch (collapsed) design: {gpus_per_plane} GPU NICs per "
        f"plane fit on one {inp.leaf_ports}-port {_fmt_speed(inp.leaf_speed)} "
        f"leaf using {downlink_ports_used} downlink ports "
        f"(breakout {leaf_breakout}:1). No spine layer required."
    )
    if leaf_breakout > 1:
        notes.append(
            f"Leaf-to-NIC breakout: each {inp.leaf_speed}G port splits into "
            f"{leaf_breakout} x {inp.nic_speed // inp.plans_per_nic}G NIC links."
        )
    if inp.plans_per_nic > 1:
        notes.append(
            "Node-side NIC breakout is in use (plans per NIC > 1); a shuffle box is needed per node."
        )
    total_nodes = math.ceil(inp.num_gpus / inp.gpus_per_node)
    notes.append(
        f"Nodes: {total_nodes} total (each with {inp.gpus_per_node} GPUs and "
        f"{inp.nics_per_gpu} x {inp.nic_speed}G NIC(s), split as "
        f"{inp.plans_per_nic} x {inp.nic_speed // inp.plans_per_nic}G per NIC)."
    )
    if num_planes > 1:
        notes.append(
            f"Totals are {num_planes} x per-plan counts "
            "(one independent collapsed leaf per plan)."
        )

    return DesignResult(
        inputs=inp, num_planes=num_planes, plane=plane,
        total_leaves=num_planes,
        total_spines=0,
        total_super_spines=0,
        total_nodes=total_nodes,
        cables=cables,
        notes=notes, feasible=True,
        topology="single-switch",
    )


def _compute_cables(inp: DesignInputs, plane: PlaneDesign,
                    num_planes: int) -> list[CableGroup]:
    """Count physical cables per layer (summed across planes).
    Breakout cables count as one physical cable carrying N logical links.
    """
    cables: list[CableGroup] = []

    # Leaf <-> Node (GPU NIC): one NIC link per GPU per plane
    if plane.leaves_per_plane > 0:
        # GPU endpoints are partitioned across planes; total leaf<->node links
        # therefore tracks total GPUs (not GPUs-per-plan multiplied by plans).
        leaf_nic_links = inp.num_gpus
        leaf_nic_count = _cable_count(leaf_nic_links, plane.leaf_breakout, 1)
        cables.append(CableGroup(
            count=leaf_nic_count,
            label=_cable_label(plane.leaf_speed, plane.leaf_breakout,
                               plane.nic_speed, 1),
            end_a="Leaf", end_b="Node",
        ))

    # Spine <-> Leaf
    if plane.spines_per_plane > 0:
        sl_links = (plane.leaves_per_plane * plane.uplink_ports_per_leaf
                    * plane.leaf_to_spine_fanout)
        sl_count = _cable_count(sl_links, plane.leaf_to_spine_fanout,
                                plane.spine_to_leaf_fanout) * num_planes
        cables.append(CableGroup(
            count=sl_count,
            label=_cable_label(plane.leaf_speed, plane.leaf_to_spine_fanout,
                               plane.spine_speed, plane.spine_to_leaf_fanout),
            end_a="Spine", end_b="Leaf",
        ))

    # Super-spine <-> Spine
    if plane.uses_super_spine and plane.super_spines_per_plane > 0:
        ss_links = (plane.spines_per_plane * plane.spine_ports_up_to_super
                    * plane.spine_to_super_fanout)
        ss_count = _cable_count(ss_links, plane.spine_to_super_fanout,
                                plane.super_to_spine_fanout) * num_planes
        cables.append(CableGroup(
            count=ss_count,
            label=_cable_label(plane.spine_speed, plane.spine_to_super_fanout,
                               plane.super_spine_speed, plane.super_to_spine_fanout),
            end_a="Super-spine", end_b="Spine",
        ))

    return cables


def _infeasible(inp: DesignInputs, notes: list[str]) -> DesignResult:
    plane = PlaneDesign(
        nic_speed=inp.nic_speed // max(1, inp.plans_per_nic), nic_speed_raw=inp.nic_speed,
        leaf_speed=inp.leaf_speed,
        spine_speed=inp.spine_speed, super_spine_speed=inp.super_spine_speed,
        leaf_breakout=0, leaf_to_spine_fanout=0, spine_to_leaf_fanout=0,
        spine_to_super_fanout=0, super_to_spine_fanout=0,
        downlink_ports_per_leaf=0, uplink_ports_per_leaf=0,
        gpus_per_leaf=0,
        gpus_per_plane=math.ceil(
            inp.num_gpus / max(1, inp.plans_per_nic * inp.nics_per_gpu)
        ),
        leaves_per_plane=0, spines_per_plane=0,
        links_per_leaf_to_each_spine=0, spine_ports_used_for_leaves=0,
    )
    return DesignResult(
        inputs=inp,
        num_planes=inp.plans_per_nic * inp.nics_per_gpu,
        plane=plane,
        total_leaves=0, total_spines=0, total_super_spines=0,
        total_nodes=math.ceil(inp.num_gpus / inp.gpus_per_node),
        notes=notes, feasible=False,
    )


def _add_common_notes(notes: list[str], inp: DesignInputs, leaf_breakout: int,
                      leaf_to_spine_fanout: int, spine_to_leaf_fanout: int,
                      downlink_ports: int, uplink_ports: int,
                      gpus_per_leaf: int, num_planes: int) -> None:
    if leaf_breakout > 1:
        notes.append(
            f"Leaf-to-NIC breakout: each {inp.leaf_speed}G leaf port splits "
            f"into {leaf_breakout} x {inp.nic_speed // inp.plans_per_nic}G NIC links."
        )
    if inp.plans_per_nic > 1:
        notes.append(
            "Node-side NIC breakout is in use (plans per NIC > 1); a shuffle box is needed per node."
        )
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
        f"Leaf port split: {downlink_ports} downlinks + {uplink_ports} "
        f"uplinks @ {inp.leaf_speed}G = {inp.leaf_ports} ports (1:1)."
    )
    notes.append(
        f"GPUs per leaf (per plan): {gpus_per_leaf} "
        f"({downlink_ports} ports x {leaf_breakout} breakout)."
    )
    total_nodes = math.ceil(inp.num_gpus / inp.gpus_per_node)
    notes.append(
        f"Nodes: {total_nodes} total (each with {inp.gpus_per_node} GPUs and "
        f"{inp.nics_per_gpu} x {inp.nic_speed}G NIC(s), split as "
        f"{inp.plans_per_nic} x {inp.nic_speed // inp.plans_per_nic}G per NIC)."
    )
    if num_planes > 1:
        notes.append(
            f"Totals are {num_planes} x per-plan counts "
            "(one independent fabric per plan)."
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


def _slots(count: int, max_items: int = 9,
           head: int = 5, tail: int = 3) -> list:
    """Return a list of slots for drawing.

    If count <= max_items: returns [0, 1, ..., count-1]
    Else: returns first `head` indices, then ELLIPSIS, then last `tail` indices.
    """
    if count <= max_items:
        return list(range(count))
    return list(range(head)) + [ELLIPSIS] + list(range(count - tail, count))


def _xs(n: int, width: int, margin: int = 90) -> list[float]:
    if n == 0:
        return []
    if n == 1:
        return [width / 2]
    step = (width - 2 * margin) / (n - 1)
    return [margin + i * step for i in range(n)]


def render_svg(result: DesignResult) -> str:
    plane = result.plane
    if not result.feasible or plane.leaves_per_plane == 0:
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 120">'
            '<text x="300" y="60" text-anchor="middle" fill="#b91c1c" '
            'font-family="sans-serif" font-size="16">'
            'Design not feasible with given inputs.</text></svg>'
        )

    width = 1260
    has_super = plane.uses_super_spine
    single_switch = (result.topology == "single-switch")
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

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'font-family="Inter, system-ui, sans-serif" font-size="12">',
        '<defs>'
        '<linearGradient id="sspineGrad" x1="0" x2="0" y1="0" y2="1">'
        '<stop offset="0" stop-color="#4a1d96"/><stop offset="1" stop-color="#7c3aed"/>'
        '</linearGradient>'
        '<linearGradient id="spineGrad" x1="0" x2="0" y1="0" y2="1">'
        '<stop offset="0" stop-color="#1e3a8a"/><stop offset="1" stop-color="#2563eb"/>'
        '</linearGradient>'
        '<linearGradient id="leafGrad" x1="0" x2="0" y1="0" y2="1">'
        '<stop offset="0" stop-color="#065f46"/><stop offset="1" stop-color="#10b981"/>'
        '</linearGradient>'
        '<linearGradient id="nodeGrad" x1="0" x2="0" y1="0" y2="1">'
        '<stop offset="0" stop-color="#7c2d12"/><stop offset="1" stop-color="#ea580c"/>'
        '</linearGradient>'
        '</defs>',
    ]

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

    spine_slots = _slots(draw_spines_count) if draw_spines_count else []
    leaf_slots = _slots(draw_leaves_count)

    spine_xs = _xs(len(spine_slots), width)
    leaf_xs = _xs(len(leaf_slots), width)

    # ---------- Super-spine row -------------------------------------------
    if has_super:
        sspine_slots = _slots(plane.super_spines_per_plane)
        sspine_xs = _xs(len(sspine_slots), width)

        # spine <-> super-spine links
        for sx_idx, sx in enumerate(sspine_xs):
            if sspine_slots[sx_idx] is ELLIPSIS:
                continue
            for sp_idx, spx in enumerate(spine_xs):
                if spine_slots[sp_idx] is ELLIPSIS:
                    continue
                parts.append(
                    f'<line x1="{sx}" y1="{super_y + 36}" x2="{spx}" y2="{spine_y}" '
                    f'stroke="#a78bfa" stroke-width="1" opacity="0.6"'
                    + (' stroke-dasharray="4 3"'
                       if plane.spine_to_super_fanout > 1 or plane.super_to_spine_fanout > 1
                       else '')
                    + '/>'
                )

        # super-spine boxes / ellipsis
        for slot, sx in zip(sspine_slots, sspine_xs):
            if slot is ELLIPSIS:
                parts.append(_ellipsis_dots(sx, super_y + 18, "#4a1d96"))
                continue
            label = f"S-Spine {slot + 1}"
            parts.append(
                f'<rect x="{sx-64}" y="{super_y}" width="128" height="36" rx="6" '
                f'fill="url(#sspineGrad)" stroke="#4a1d96"/>'
                f'<text x="{sx}" y="{super_y+16}" text-anchor="middle" fill="white" '
                f'font-weight="600">{label}</text>'
                f'<text x="{sx}" y="{super_y+30}" text-anchor="middle" fill="#ddd6fe" '
                f'font-size="11">{result.inputs.spine_ports}-port @ '
                f'{_fmt_speed(plane.super_spine_speed)}</text>'
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
                    f'<line x1="{spx}" y1="{spine_y + 36}" x2="{lx}" y2="{leaf_y}" '
                    f'stroke="#60a5fa" stroke-width="1" opacity="0.7"{sl_dash}/>'
                )

        # ---------- Spine row ---------------------------------------------
        for slot, sx in zip(spine_slots, spine_xs):
            if slot is ELLIPSIS:
                parts.append(_ellipsis_dots(sx, spine_y + 18, "#1e3a8a"))
                continue
            parts.append(
                f'<rect x="{sx-62}" y="{spine_y}" width="124" height="36" rx="6" '
                f'fill="url(#spineGrad)" stroke="#1e3a8a"/>'
                f'<text x="{sx}" y="{spine_y+16}" text-anchor="middle" fill="white" '
                f'font-weight="600">Spine {slot + 1}</text>'
                f'<text x="{sx}" y="{spine_y+30}" text-anchor="middle" fill="#bfdbfe" '
                f'font-size="11">{result.inputs.spine_ports}-port @ '
                f'{_fmt_speed(plane.spine_speed)}</text>'
            )

    # ---------- Leaf row --------------------------------------------------
    for slot, lx in zip(leaf_slots, leaf_xs):
        if slot is ELLIPSIS:
            parts.append(_ellipsis_dots(lx, leaf_y + 18, "#065f46"))
            continue
        parts.append(
            f'<rect x="{lx-62}" y="{leaf_y}" width="124" height="36" rx="6" '
            f'fill="url(#leafGrad)" stroke="#065f46"/>'
            f'<text x="{lx}" y="{leaf_y+16}" text-anchor="middle" fill="white" '
            f'font-weight="600">Leaf {slot + 1}</text>'
            f'<text x="{lx}" y="{leaf_y+30}" text-anchor="middle" fill="#a7f3d0" '
            f'font-size="11">{result.inputs.leaf_ports}-port @ '
            f'{_fmt_speed(plane.leaf_speed)}</text>'
        )

    # ---------- Node row --------------------------------------------------
    # Cap total node icons at MAX_NODE_ICONS (diagram-wide) and add
    # ellipsis dots beneath each leaf when its real node count is larger.
    MAX_NODE_ICONS = 24
    gpus_per_node = result.inputs.gpus_per_node
    drawn_leaf_count = sum(1 for s in leaf_slots if s is not ELLIPSIS) or 1
    real_nodes_per_leaf = max(1, math.ceil(plane.gpus_per_leaf / gpus_per_node))
    nodes_per_leaf_draw = max(1, min(
        real_nodes_per_leaf,
        MAX_NODE_ICONS // drawn_leaf_count,
    ))
    show_node_ellipsis = real_nodes_per_leaf > nodes_per_leaf_draw
    leaf_nic_dash = ' stroke-dasharray="4 3"' if plane.leaf_breakout > 1 else ""

    for slot, lx in zip(leaf_slots, leaf_xs):
        if slot is ELLIPSIS:
            parts.append(_ellipsis_dots(lx, node_y + 24, "#7c2d12"))
            continue
        slots_in_group = nodes_per_leaf_draw + (1 if show_node_ellipsis else 0)
        group_width = 82 * slots_in_group + 6 * (slots_in_group - 1)
        start_x = lx - group_width / 2
        for j in range(nodes_per_leaf_draw):
            nx = start_x + j * (82 + 6)
            parts.append(
                f'<line x1="{lx}" y1="{leaf_y + 36}" x2="{nx + 41}" y2="{node_y}" '
                f'stroke="#f97316" stroke-width="1.2"{leaf_nic_dash}/>'
                f'<rect x="{nx}" y="{node_y}" width="82" height="50" rx="6" '
                f'fill="url(#nodeGrad)" stroke="#7c2d12"/>'
                f'<text x="{nx+41}" y="{node_y+16}" text-anchor="middle" fill="white" '
                f'font-weight="600">Node</text>'
                f'<text x="{nx+41}" y="{node_y+32}" text-anchor="middle" fill="#fed7aa" '
                f'font-size="11">{gpus_per_node} GPUs</text>'
                f'<text x="{nx+41}" y="{node_y+46}" text-anchor="middle" fill="#fed7aa" '
                f'font-size="10">{result.inputs.nics_per_gpu}x{_fmt_speed(result.inputs.nic_speed)} NIC ({result.inputs.plans_per_nic}x{_fmt_speed(plane.nic_speed)})</text>'
            )
        if show_node_ellipsis:
            ex = start_x + nodes_per_leaf_draw * (82 + 6) + 41
            parts.append(_ellipsis_dots(ex, node_y + 24, "#7c2d12"))

    # ---------- Layer labels & annotations --------------------------------
    if has_super:
        parts.append(
            f'<text x="20" y="{super_y+22}" fill="#4a1d96" font-weight="700">SUPER-SPINE</text>'
        )
    if not single_switch:
        parts.append(
            f'<text x="20" y="{spine_y+22}" fill="#1e3a8a" font-weight="700">SPINE</text>'
        )
    parts.append(
        f'<text x="20" y="{leaf_y+22}" fill="#065f46" font-weight="700">LEAF</text>'
        f'<text x="20" y="{node_y+24}" fill="#7c2d12" font-weight="700">NODES</text>'
    )

    if has_super:
        e2e = min(plane.spine_speed, plane.super_spine_speed)
        bk = ""
        if plane.spine_to_super_fanout > 1:
            bk = f" (spine {plane.spine_to_super_fanout}:1 breakout)"
        elif plane.super_to_spine_fanout > 1:
            bk = f" (super-spine {plane.super_to_spine_fanout}:1 breakout)"
        parts.append(
            f'<text x="{width/2}" y="{(super_y+spine_y)/2}" text-anchor="middle" '
            f'fill="#4a1d96">{_fmt_speed(e2e)} super-spine &#8596; spine{bk}</text>'
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
            f'<text x="{width/2}" y="{(spine_y+leaf_y)/2}" text-anchor="middle" '
            f'fill="#1e3a8a">{_fmt_speed(e2e_spine_leaf)} spine &#8596; leaf{sl_note}</text>'
        )

    leaf_nic_note = f" (leaf {plane.leaf_breakout}:1 breakout)" if plane.leaf_breakout > 1 else ""
    plan_note = f" &#183; plan 1 of {result.num_planes}" if result.num_planes > 1 else ""
    parts.append(
        f'<text x="{width/2}" y="{(leaf_y+node_y)/2 + 10}" text-anchor="middle" '
        f'fill="#7c2d12">{_fmt_speed(plane.nic_speed)} to GPU NICs'
        f'{leaf_nic_note}{plan_note}</text>'
    )

    # Pod annotation for 3-tier
    if has_super and plane.pods_per_plane > 1:
        parts.append(
            f'<text x="{width/2}" y="{leaf_y - 50}" text-anchor="middle" '
            f'fill="#065f46" font-size="12" font-weight="600">'
            f'Showing 1 of {plane.pods_per_plane} pods '
            f'({plane.leaves_per_pod} leaves + {plane.spines_per_pod} spines each)'
            f'</text>'
        )

    # Bottom summary strip: counts + cables
    ss_part = (
        f' &#183; {result.total_super_spines} super-spines'
        if result.total_super_spines else ""
    )
    sp_part = (
        f' &#183; {result.total_spines} spines'
        if result.total_spines else ""
    )
    parts.append(
        f'<text x="{width/2}" y="{height - 36}" text-anchor="middle" '
        f'fill="#334155" font-size="13" font-weight="600">'
        f'{result.total_nodes} nodes &#183; {result.inputs.num_gpus} GPUs '
        f'&#183; {result.total_leaves} leaves'
        f'{sp_part}{ss_part} &#183; {result.num_planes} plan(s)</text>'
    )
    if result.cables:
        cable_text = " &#183; ".join(
            f"{c.end_a}&#8596;{c.end_b} x {c.count:,} ({c.label})"
            for c in result.cables
        )
        parts.append(
            f'<text x="{width/2}" y="{height - 14}" text-anchor="middle" '
            f'fill="#475569" font-size="12">Cables: {cable_text}</text>'
        )

    parts.append('</svg>')
    return "".join(parts)


def _ellipsis_dots(cx: float, cy: float, color: str) -> str:
    return (
        f'<g fill="{color}">'
        f'<circle cx="{cx-12}" cy="{cy}" r="3"/>'
        f'<circle cx="{cx}" cy="{cy}" r="3"/>'
        f'<circle cx="{cx+12}" cy="{cy}" r="3"/>'
        '</g>'
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

DEFAULTS = dict(
    num_gpus=1024,
    gpus_per_node=8,
    nics_per_gpu=1,
    spine_ports=64,
    leaf_ports=64,
    nic_speed=400,
    leaf_speed=800,
    spine_speed=800,
    super_spine_speed=0,
    plans_per_nic=1,
)


@app.route("/", methods=["GET", "POST"])
def index():
    form = dict(DEFAULTS)
    result = None
    svg = None
    error = None

    if request.method == "POST":
        try:
            form = dict(
                num_gpus=int(request.form["num_gpus"]),
                gpus_per_node=int(request.form["gpus_per_node"]),
                nics_per_gpu=int(request.form["nics_per_gpu"]),
                spine_ports=int(request.form["spine_ports"]),
                leaf_ports=int(request.form["leaf_ports"]),
                nic_speed=int(request.form["nic_speed"]),
                leaf_speed=int(request.form["leaf_speed"]),
                spine_speed=int(request.form["spine_speed"]),
                super_spine_speed=int(request.form.get("super_spine_speed", 0)),
                # Accept legacy name "plans" from older cached HTML
                plans_per_nic=int(
                    request.form.get("plans_per_nic")
                    or request.form.get("plans")
                    or 1
                ),
            )
            if form["num_gpus"] <= 0:
                raise ValueError("Number of GPUs must be positive.")
            if form["gpus_per_node"] <= 0:
                raise ValueError("GPUs per node must be positive.")
            if form["nics_per_gpu"] not in (1, 2, 3):
                raise ValueError("NICs per GPU must be 1, 2 or 3.")
            if form["spine_ports"] <= 1 or form["leaf_ports"] <= 1:
                raise ValueError("Port counts must be > 1.")
            if form["nic_speed"] not in (400, 800):
                raise ValueError("NIC speed must be 400G or 800G.")
            if form["leaf_speed"] not in (400, 800, 1600):
                raise ValueError("Leaf port speed must be 400G, 800G or 1.6T.")
            if form["spine_speed"] not in (400, 800, 1600):
                raise ValueError("Spine port speed must be 400G, 800G or 1.6T.")
            if form["super_spine_speed"] not in (0, 800, 1600):
                raise ValueError("Super-spine speed must be 800G or 1.6T (or disabled).")
            if form["plans_per_nic"] not in (1, 2, 4):
                raise ValueError("Plans per NIC must be 1, 2, or 4.")
            if form["nic_speed"] % form["plans_per_nic"] != 0:
                raise ValueError("NIC speed must be divisible by plans per NIC.")

            inp = DesignInputs(**form)
            result = design_fabric(inp)
            svg = render_svg(result)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)

    return render_template(
        "index.html", form=form, result=result, svg=svg, error=error,
    )


if __name__ == "__main__":
    host = "0.0.0.0"
    listening_port = "5000"
    debug = False
    browser_only = "false"

    def run_flask():
        app.run(
            host="0.0.0.0",
            port=listening_port,
            debug=debug,
            use_reloader=False,
            threaded=True,
        )

    if browser_only == "true":
        app.run(host=host, port=listening_port, debug=debug, use_reloader=False)
    else:
        threading.Thread(target=run_flask, daemon=True).start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{listening_port}/", timeout=0.25)
                break
            except OSError:
                time.sleep(0.05)
        webview.create_window("AI Cable Calculator", f"http://127.0.0.1:{listening_port}")
        webview.start()
