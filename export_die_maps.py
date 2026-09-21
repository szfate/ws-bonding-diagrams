"""Export per-design die render PNG + pad-map JSON for the test-result viewer.

Reads tmp/<reticle>/pads.json (produced by extract_dies.py — no GDS pad
re-extraction), renders each design at DIE_EXPORT_MAX_PX via the vendored
make_diagrams renderer, and writes, per design:

    tmp/<reticle>/die_exports/<design>.png
    tmp/<reticle>/die_exports/<design>.data.json

The JSON is self-contained: pad centers and boxes in both µm (die frame,
copied from pads.json) and px (image space, origin top-left, y flipped).
The image covers the die bounding box exactly, so px = (um - die origin)
* width_px / die_width_um. Coordinates are intended for overlaying die
test results in the separate results-viewer repo; nothing viewer-related
lives here.

Usage:
    uv run export_die_maps.py [--reticle ws-run1] [--designs NAME ...]
                              [--all] [--force]
"""

from __future__ import annotations

from collections import Counter

DIE_EXPORT_MAX_PX = 2048


def pad_px_coords(die_bb: tuple[float, float, float, float],
                  width_px: int, height_px: int,
                  *, cx: float, cy: float,
                  x0: float, x1: float, y0: float, y1: float) -> dict:
    """Map a pad's µm center/box to image px (origin top-left, y flipped)."""
    x0d, y0d, x1d, y1d = die_bb
    sx = width_px / (x1d - x0d)
    sy = height_px / (y1d - y0d)
    return {
        "cx_px": (cx - x0d) * sx,
        "cy_px": (y1d - cy) * sy,
        "x0_px": (x0 - x0d) * sx,
        "x1_px": (x1 - x0d) * sx,
        "y0_px": (y1d - y1) * sy,
        "y1_px": (y1d - y0) * sy,
    }


def build_die_json(record: dict, width_px: int, height_px: int) -> dict:
    """Build the per-design export JSON from a pads.json design record."""
    die_bb = tuple(record["die_bb_um"])
    x0d, y0d = die_bb[0], die_bb[1]
    sx = width_px / (die_bb[2] - x0d)
    per_edge = Counter(p["edge"] for p in record["pads"])
    return {
        "design": record["name"],
        "reticle": record["reticle"],
        "image": {
            "file": f"{record['code']}.png",
            "width_px": width_px,
            "height_px": height_px,
            "px_per_um": sx,
        },
        "die": {
            "width_um": die_bb[2] - x0d,
            "height_um": die_bb[3] - y0d,
        },
        "slot_size": record.get("slot_size"),
        "pad_count": {
            "total": len(record["pads"]),
            "top": per_edge["T"],
            "bottom": per_edge["B"],
            "left": per_edge["L"],
            "right": per_edge["R"],
            "labelled": sum(1 for p in record["pads"] if p["net"]),
        },
        "pads": [
            {
                "number": p["n"],
                "edge": p["edge"],
                "net": p["net"],
                **pad_px_coords(die_bb, width_px, height_px,
                                cx=p["cx_um"], cy=p["cy_um"],
                                x0=p["x0_um"], x1=p["x1_um"],
                                y0=p["y0_um"], y1=p["y1_um"]),
                "cx_um": p["cx_um"], "cy_um": p["cy_um"],
                "x0_um": p["x0_um"], "x1_um": p["x1_um"],
                "y0_um": p["y0_um"], "y1_um": p["y1_um"],
            }
            for p in record["pads"]
        ],
    }


import argparse
import json
import time
from pathlib import Path

import klayout.db as kdb
from PIL import Image

import make_diagrams
import extract_dies
from make_diagrams import render_gds_background, setup_layout_view
from runs import run_config

REPO = Path(__file__).resolve().parent
TMP_ROOT = REPO / "tmp"


def export_design(lv, layout, name: str, record: dict, out_dir: Path,
                  force: bool) -> Path:
    """Render <name> at DIE_EXPORT_MAX_PX and write PNG + JSON.

    Files are named by the 4-char project code (<code>.png / <code>.json):
    at test time slots are unknown, and designs sharing a code are the
    same layout in different slots (checked by the caller).
    """
    die_bb = tuple(record["die_bb_um"])
    code = record["code"]
    out_png = out_dir / f"{code}.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    if force or not out_png.exists():
        render_gds_background(lv, name, layout, die_bb, out_png,
                              max_px=DIE_EXPORT_MAX_PX)
        # Match the stored pads.json frame exactly as extract_dies.py does:
        # rotate the render 180° only when the stored frame is the display
        # frame (see extract_dies.extract_design).
        if run_config(record["reticle"])["page1_die_rotation_deg"] == 180:
            make_diagrams._rotate_image_180(out_png)

    with Image.open(out_png) as img:
        width_px, height_px = img.size

    out_json = out_dir / f"{code}.json"
    out_json.write_text(
        json.dumps(build_die_json(record, width_px, height_px), indent=1))
    return out_json


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reticle", default="ws-run1",
                    help="sibling checkout dir name or path containing "
                         "layout/reticle.oas (default: ws-run1)")
    ap.add_argument("--designs", nargs="*", default=None,
                    help="design cell names (default: --all)")
    ap.add_argument("--all", action="store_true",
                    help="export every design on the reticle (default)")
    ap.add_argument("--force", action="store_true",
                    help="re-render cached PNGs")
    args = ap.parse_args()

    reticle_dir, oas = extract_dies.resolve_reticle(args.reticle)
    reticle = reticle_dir.name
    make_diagrams.LYP = reticle_dir / "lyp" / "gf180mcu.lyp"

    pads_path = TMP_ROOT / reticle / "pads.json"
    if not pads_path.exists():
        raise SystemExit(
            f"{pads_path} not found — run `uv run extract_dies.py "
            f"--reticle {reticle} --all` first")
    records = json.loads(pads_path.read_text())
    by_name = {r["name"]: r for r in records}

    layout = kdb.Layout()
    print(f"Reading {oas} ...")
    t0 = time.time()
    layout.read(str(oas))
    print(f"  loaded in {time.time() - t0:.1f}s")

    names = args.designs
    if args.all or not names:
        top = next(iter(layout.top_cells()))
        names = sorted({inst.cell.name for inst in top.each_inst()
                        if inst.cell.name != "RETICLE_FILL"
                        and not inst.cell.name.startswith("TEXT")})
    missing = [n for n in names if n not in by_name]
    if missing:
        raise SystemExit(
            f"designs missing from {pads_path}: {missing} — re-run "
            f"extract_dies.py --all")

    # One export per project code: a design in several slots is the same
    # layout, so export it once (from the first slot's record) — but fail
    # loudly if same-code records actually differ.
    def pad_sig(r):
        return [(p["n"], p["net"], p["x0_um"], p["y0_um"],
                 p["x1_um"], p["y1_um"]) for p in r["pads"]]

    exports: list[tuple[str, dict]] = []
    seen: dict[str, dict] = {}
    for n in names:
        r = by_name[n]
        prev = seen.get(r["code"])
        if prev is None:
            seen[r["code"]] = r
            exports.append((n, r))
        elif (prev["die_bb_um"] != r["die_bb_um"]
              or pad_sig(prev) != pad_sig(r)):
            raise SystemExit(
                f"same code {r['code']!r}, different layout: "
                f"{prev['name']} vs {r['name']} — cannot dedupe")

    lv = setup_layout_view(layout)
    out_dir = TMP_ROOT / reticle / "die_exports"

    for i, (name, record) in enumerate(exports, start=1):
        t0 = time.time()
        out_json = export_design(lv, layout, name, record, out_dir,
                                 args.force)
        j = json.loads(out_json.read_text())
        print(f"  [{i}/{len(exports)}] {name} -> {j['image']['file']}: "
              f"{j['image']['width_px']}x{j['image']['height_px']} px, "
              f"{len(j['pads'])} pads ({time.time() - t0:.1f}s)")

    print(f"wrote {len(exports)} exports to {out_dir}")


if __name__ == "__main__":
    main()
