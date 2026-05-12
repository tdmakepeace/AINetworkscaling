# AI Spine-Leaf Network Designer

A **Flask** application that sizes a **non-blocking (1:1)** spine-leaf fabric for AI/GPU clusters, optionally adds a **super-spine** tier when radix limits require it, estimates **physical cable groups**, and renders an **SVG** topology diagram. By default it opens in a **native window** via **pywebview** while serving on `http://127.0.0.1:5000/`.

## Features

- **Topologies**: single leaf switch (when a plane fits on one switch), classic two-tier spine-leaf, or three-tier spine / super-spine when spines cannot fan out to enough leaves within radix.
- **Parallel fabrics**: combines **NICs per GPU** (1–3) with **plans per NIC** (0, 1, 2, or 4). **0** means a **single fabric plan** (all NIC endpoints in one plane). For **1 / 2 / 4**, each physical NIC is broken into that many logical **plan legs** (e.g. 4×100G from one 400G NIC). **Every GPU participates in every parallel plan** at the per-plan link speed; fabric sizing uses the **full GPU count per plan**, not a split of GPUs across plans. Switch and cable **totals** still scale with the number of parallel planes (independent physical fabrics).
- **Independent port speeds**: **NIC** (400G / 800G), **leaf** (400G / 800G / 1.6T), **spine** (400G / 800G / 1.6T), optional **super-spine** (off, 800G, or 1.6T). Faster ports assume **breakout** when connecting to slower endpoints. Cable labels use a **faster-side-first** breakout form (e.g. **800G-2x400G** for spine–leaf).
- **Cluster shape**: total **GPUs**, **GPUs per node** (for node counts and cabling to hosts).
- **Compare plans**: second submit button runs the design for **plans_per_nic** values **0, 1, 2, and 4** with the same inputs and opens a **modal** with a side-by-side table (feasibility, topology, switch counts, cables).
- **Bill of materials (BOM)**: after each design, a **layered BOM** lists super-spine / spine / leaf **switch counts** (radix and speed) and **cable quantities** with the same optic/breakout labels as the cable summary. Super-spine switch radix is independently configurable from spine radix. **Shuffle boxes** are described as **might be needed** when multi-plane and NIC plan breakout apply; counts are a **planning hint**, not a firm order (real builds depend on cable and optic choices).

## Inputs

| Field | Meaning |
|--------|--------|
| Number of GPUs | Total GPUs in the cluster |
| GPUs per node | Drives server/node counts in outputs |
| NICs per GPU | 1, 2, or 3; when plans per NIC is not 0, multiplies the number of **parallel physical fabrics** (each NIC × each plan leg). |
| Ports per spine switch | Spine switch radix |
| Ports per super-spine switch | Super-spine switch radix (used when a 3-tier design is required) |
| Ports per leaf switch | Leaf switch radix |
| NIC / leaf / spine speed | As validated in the form (see `app.py`) |
| Super-spine speed | Disabled, or 800G / 1.6T when a third tier is needed |
| Plans per NIC | **0** = single plan (all NICs in one fabric); **1, 2, or 4** = that many logical legs per physical NIC (NIC speed must divide evenly). Each plan is sized for **all GPUs** on that leg’s speed (breakout/shuffle from the NIC to the leaf), not “GPUs ÷ number of plans.” |

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
- **Multi-plan sizing**: parallel plans are **separate fabrics**, but the model assumes **each GPU is present in each plan** on its own sub-link (e.g. the same GPU reaches leaf resources in plan 1…N via NIC breakout). Design notes and counts reflect that model.
- If a design cannot fit on one spine layer, **super-spine** is used only when configured and the logic in `design_fabric` can place a third tier; otherwise the result is marked **infeasible** with explanatory notes.

## Requirements

- Python 3 with `pip` (or use your usual venv workflow). If you use **uv**, you can run `uv pip install -r requirements.txt` instead of `pip install`.

Dependencies are listed in `requirements.txt` (Flask, pywebview).

## Running

### Install

```bash
pip install -r requirements.txt
```

For **tests** (optional), install **pytest** in the same environment (for example `pip install pytest`) and run `pytest` from the repository root (see `pytest.ini`).

### Run with Docker or Podman

The container runs **Flask only** on port **10000** (no desktop **pywebview** window inside the image).

1. Build the image:

```bash
docker build -t ainetwork-designer .
```

With **Podman**:

```bash
podman build -t ainetwork-designer .
```

2. Run the container and publish the app port:

```bash
docker run --rm -p 10000:10000 --name ainetwork-designer ainetwork-designer
```

```bash
podman run --rm -p 10000:10000 --name ainetwork-designer ainetwork-designer
```

3. Open the app in your browser:

`http://localhost:10000/`

4. Stop the app:

- Press `Ctrl+C` in the terminal running the container (foreground), or stop the name you chose, for example `docker stop ainetwork-designer` or `podman stop ainetwork-designer`, if you ran detached.

Optional:

- Run detached: `docker run -d -p 10000:10000 --name ainetwork-designer ainetwork-designer` (same pattern with `podman run -d …`).

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

With **1024** GPUs, **8** GPUs per node, **1** NIC per GPU, **64**-port leaves and spines, **400G** NICs with **4** plans per NIC (4×100G legs), **400G** leaf ports, and **800G** spine links: each of the four parallel fabrics is sized for **all 1024 GPUs** on that plan’s 100G leg (not 256 GPUs per plan). Depending on radix and breakout, you may get **multi-leaf / spine-leaf** rather than a single collapsed leaf per plan—the UI shows topology, notes, cable groups, and BOM.

With **plans per NIC = 0** (single fabric), the same cluster uses one plane carrying **all NIC endpoints** at full NIC speed.

For **rail-style** scaling, increase **NICs per GPU** and/or **plans per NIC** to add more **parallel physical fabrics**; each fabric is still sized for the full GPU count at the effective per-plan link rate. Use **Compare plans** to see **0 / 1 / 2 / 4** plans per NIC in one table.
