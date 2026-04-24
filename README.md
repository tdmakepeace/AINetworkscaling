# AI Spine-Leaf Network Designer

A **Flask** application that sizes a **non-blocking (1:1)** spine-leaf fabric for AI/GPU clusters, optionally adds a **super-spine** tier when radix limits require it, estimates **physical cable groups**, and renders an **SVG** topology diagram. By default it opens in a **native window** via **pywebview** while serving on `http://127.0.0.1:5000/`.

## Features

- **Topologies**: single leaf switch (when a plane fits on one switch), classic two-tier spine-leaf, or three-tier spine / super-spine when spines cannot fan out to enough leaves within radix.
- **Parallel fabrics**: combines **NICs per GPU** (1–3) with **plans per NIC** (1, 2, or 4) so each logical “plane” is sized independently; totals are scaled across planes.
- **Independent port speeds**: **NIC** (400G / 800G), **leaf** (400G / 800G / 1.6T), **spine** (400G / 800G / 1.6T), optional **super-spine** (off, 800G, or 1.6T). Faster ports assume **breakout** when connecting to slower endpoints.
- **Cluster shape**: total **GPUs**, **GPUs per node** (for node counts and cabling to hosts).

## Inputs

| Field | Meaning |
|--------|--------|
| Number of GPUs | Total GPUs in the cluster |
| GPUs per node | Drives server/node counts in outputs |
| NICs per GPU | 1, 2, or 3; multiplies parallel planes with plans |
| Ports per spine / leaf | Spine and leaf switch radix |
| NIC / leaf / spine speed | As validated in the form (see `app.py`) |
| Super-spine speed | Disabled, or 800G / 1.6T when a third tier is needed |
| Plans per NIC | 1, 2, or 4 — logical fabrics per physical NIC (e.g. breakout); NIC speed must divide evenly |

## Outputs

- Counts of **leaf**, **spine**, and **super-spine** switches (when used), plus **nodes**
- Per-plane port usage, link bundling, and design **notes**
- **Cable group** summary (counts and types, e.g. mixed-speed breakout labels)
- **Feasibility** warnings when radix or speed rules cannot be met
- **SVG** topology sketch

## Design assumptions

- Two-tier: every leaf connects to every spine in a plane; the tool picks a **downlink/uplink port split** on leaves and may **bundle** multiple links per leaf–spine pair to reduce spine count within spine radix.
- **1:1**: aggregate bandwidth from GPUs to the fabric is not oversubscribed on the uplink path (see in-app notes for the specific inequality used).
- Speed relationships: leaf speed must be at least the effective per-plan NIC speed and an integer multiple of it; allowed speeds are consistent with **400G** base rates.
- If a design cannot fit on one spine layer, **super-spine** is used only when configured and the logic in `design_fabric` can place a third tier; otherwise the result is marked **infeasible** with explanatory notes.

## Requirements

- Python 3 with `pip` (or use your usual venv workflow)

Dependencies are listed in `requirements.txt` (Flask, pywebview).

## Running

```bash
pip install -r requirements.txt
python app.py
```

- A desktop window titled **AI Cable Calculator** should open once the server is ready.
- You can also browse to **http://127.0.0.1:5000/** (or from another machine to `http://<host>:5000/` — the dev server binds to `0.0.0.0`).

To run **without** the native window, start the Flask app from your own entrypoint or temporarily adjust the `if __name__ == "__main__"` block in `app.py` to call `app.run(...)` only (see comments around `browser_only` in `app.py`).

## Example

With **1024** GPUs, **8** GPUs per node, **1** NIC per GPU, **64**-port leaves and spines, **400G** NICs, **800G** leaf and spine links, **1** plan per NIC: you get a two-tier design with per-plane GPU split, leaf/spine counts, and cable breakdown in the UI—similar in spirit to the classic “maximize GPU-facing ports while keeping uplinks non-blocking” goal described in earlier revisions of this tool.

For **rail-style** scaling, increase **NICs per GPU** and/or **plans per NIC** so the workload is split across more parallel fabrics before each plane is sized.
