# AI Spine-Leaf Network Designer

A small Flask app that sizes a **non-blocking (1:1 subscription)** spine-leaf
fabric for AI/GPU clusters and draws a topology diagram.

## Inputs

- **Number of GPUs** – total GPU count in the cluster.
- **Ports per spine switch** – radix of the spine switches.
- **Ports per leaf switch** – radix of the leaf (ToR) switches.
- **Spine ↔ Leaf interface speed** – `400G` or `800G`.
- **Leaf ↔ GPU interface** –
  - `400G` for a classic single-plane design, or
  - `4x100G` for a multi-plane / rail-optimised design (4 independent
    spine-leaf fabrics, one per GPU NIC rail).

## Outputs

- Total number of **leaf** and **spine** switches needed.
- Per-leaf port split between GPU downlinks and spine uplinks.
- Design notes (oversubscription, port budgets, feasibility warnings).
- An SVG diagram of the resulting topology.

## Design assumptions

- Two-tier spine-leaf: every leaf connects to every spine.
- 1:1 subscription: aggregate leaf downlink bandwidth ≤ aggregate uplink
  bandwidth. Port split is chosen to maximise GPU-facing ports while
  preserving this invariant.
- Multi-plane mode treats each of the 4 rails as its own independent
  spine-leaf fabric; the reported totals multiply per-plane counts by 4.
- If the required number of leaves exceeds what a single spine layer can
  fan out to, the tool flags the design as infeasible (suggesting more
  planes, larger spines, or a multi-pod / super-spine design).

## Running

```bash
pip install -r requirements.txt
python app.py
```

Then open <http://127.0.0.1:5000/>.

## Example

With 1024 GPUs, 64-port leaves, 64-port spines, 800G spine-leaf, 400G to
GPUs: each leaf gets 42 downlinks @ 400G and 22 uplinks @ 800G
(42×400 = 16,800G ≤ 22×800 = 17,600G → 1:1). 25 leaves and 11 spines
(each leaf uses 2 links to each spine), single plane.
