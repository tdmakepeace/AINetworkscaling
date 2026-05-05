# AI Spine-Leaf Network Designer

A **Flask** application that sizes a **non-blocking (1:1)** spine-leaf fabric for AI/GPU clusters, optionally adds a **super-spine** tier when radix limits require it, estimates **physical cable groups**, and renders an **SVG** topology diagram. By default it opens in a **native window** via **pywebview** while serving on `http://127.0.0.1:5000/`.

## Features

- **Topologies**: single leaf switch (when a plane fits on one switch), classic two-tier spine-leaf, or three-tier spine / super-spine when spines cannot fan out to enough leaves within radix.
- **Parallel fabrics**: combines **NICs per GPU** (1–3) with **plans per NIC** (0, 1, 2, or 4). **0** means a **single fabric plan** (all NIC endpoints in one plane); values **1 / 2 / 4** split each physical NIC into that many logical plans before sizing. Totals scale across planes.
- **Independent port speeds**: **NIC** (400G / 800G), **leaf** (400G / 800G / 1.6T), **spine** (400G / 800G / 1.6T), optional **super-spine** (off, 800G, or 1.6T). Faster ports assume **breakout** when connecting to slower endpoints. Cable labels use a **faster-side-first** breakout form (e.g. **800G-2x400G** for spine–leaf).
- **Cluster shape**: total **GPUs**, **GPUs per node** (for node counts and cabling to hosts).
- **Compare plans**: second submit button runs the design for **plans_per_nic** values **0, 1, 2, and 4** with the same inputs and opens a **modal** with a side-by-side table (feasibility, topology, switch counts, cables).
- **Bill of materials (BOM)**: after each design, a **layered BOM** lists super-spine / spine / leaf **switch counts** (radix and speed) and **cable quantities** with the same optic/breakout labels as the cable summary. Super-spine switch radix is independently configurable from spine radix. **Shuffle boxes** are described as **might be needed** when multi-plane and NIC plan breakout apply; counts are a **planning hint**, not a firm order (real builds depend on cable and optic choices).

## Inputs

| Field | Meaning |
|--------|--------|
| Number of GPUs | Total GPUs in the cluster |
| GPUs per node | Drives server/node counts in outputs |
| NICs per GPU | 1, 2, or 3; multiplies parallel planes with plans (when plans per NIC is not 0) |
| Ports per spine switch | Spine switch radix |
| Ports per super-spine switch | Super-spine switch radix (used when a 3-tier design is required) |
| Ports per leaf switch | Leaf switch radix |
| NIC / leaf / spine speed | As validated in the form (see `app.py`) |
| Super-spine speed | Disabled, or 800G / 1.6T when a third tier is needed |
| Plans per NIC | **0** = single plan (all NICs in one fabric); **1, 2, or 4** = logical fabrics per physical NIC; for 1 / 2 / 4, NIC speed must divide evenly |

## Outputs

- Counts of **leaf**, **spine**, and **super-spine** switches (when used), plus **nodes**
- Per-plane port usage, link bundling, and design **notes**
- **Cable group** summary (counts and types, e.g. mixed-speed breakout labels)
- **Bill of materials** by layer (switches + cables), plus optional shuffle-box guidance
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

### Install

```bash
pip install -r requirements.txt
```

### Windows

**Option A — packaged executable**

- Run **`output\AIScaling.exe`** from the repository (or copy that folder elsewhere and run the `.exe` there).

**Option B — from source**

Activate your virtual environment, then start the app, for example:

```powershell
cd "C:\path\to\AInetworkingscaling"
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Or without activating the venv:

```powershell
cd "C:\path\to\AInetworkingscaling"
.\.venv\Scripts\python.exe app.py
```

- With the default settings, a desktop window titled **AI Cable Calculator** should open once the server is ready.
- You can also open **http://127.0.0.1:5000/** in a browser (the dev server binds to `0.0.0.0`).

### macOS and Linux

**Browser-only (simplest on many systems)**

- In `app.py`, in the `if __name__ == "__main__":` block, set:

  `browser_only = "True"`

  (The code compares this variable to the string **`"True"`**.)

- Then run `python app.py` and use your browser at **http://127.0.0.1:5000/**.

**Native pywebview window on Linux**

- If you want the embedded window instead of a browser, install GTK/WebKit pieces that **pywebview** can use, for example on Debian/Ubuntu:

  ```bash
  sudo apt install python3-gi python3-gi-cairo gir1.2-webkit2-4.0
  ```

- Keep `browser_only = "False"` (or any value other than `"True"`) and run `python app.py` again.

## Example

With **1024** GPUs, **8** GPUs per node, **1** NIC per GPU, **64**-port leaves/spines/super-spines, **400G** NICs, **800G** leaf and spine links, **1** plan per NIC: you get a two-tier design with per-plane GPU split, leaf/spine counts, cable breakdown, and BOM in the UI—similar in spirit to the classic “maximize GPU-facing ports while keeping uplinks non-blocking” goal described in earlier revisions of this tool.

For **rail-style** scaling, increase **NICs per GPU** and/or **plans per NIC** so the workload is split across more parallel fabrics before each plane is sized. Use **Compare plans** to see **0 / 1 / 2 / 4** plans per NIC in one table.
