"""Extract per-design die pad data + two-tone GDS renders from reticle.oas.

Reuses the extraction core of the vendored make_diagrams.py (originally
wafer-space-die-pad-diagrams; same pipeline order as its main(), so
numbers match the sibling diagrams exactly): pads from layer 37/0, net
labels from 81/10 + 53/10, peripheral filter, 180° display rotation,
CCW numbering from the QR.

Output coordinates are in the *display frame* — the GDS frame rotated 180°
so the QR sits top-right — with the origin at the die bbox corner. The die
is physically placed on the COB rotated 180° relative to GDS as well, so
display-frame coordinates map straight onto the padring frame (both
rotations cancel; see PAD_MAPPING.md in the sibling repo).

Outputs:
    tmp/pads.json                    — per-design pad data
    tmp/gds_renders/<cell>.png       — two-tone die render, display orientation

Usage:
    uv run extract_dies.py [--designs GD03_chip_top_16_4 ...] [--all] [--force]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import klayout.db as kdb

REPO = Path(__file__).resolve().parent

from make_diagrams import (  # noqa: E402  (vendored, see module docstring)
    OAS,
    _classify_edge,
    _is_peripheral,
    _number_pads_ccw,
    _project_code,
    _rotate_image_180,
    _rotate_pad_180,
    assign_net_names,
    computed_slot_size,
    extract_labels,
    extract_pads,
    render_gds_background,
    setup_layout_view,
)

DEFAULT_OUT = REPO / "tmp" / "pads.json"
BG_DIR = REPO / "tmp" / "gds_renders"

# Long-edge pixels for the two-tone die render. The bonding pages show
# the die ~12x life size, and the placement page's QR zoom inset crops a
# fixed 0.28 mm window and prints it ~18.5 mm wide — at the sibling
# repo's 3200 px that inset lands at only ~240 dpi for the largest dies.
# 8000 px puts the inset at ~600 dpi and the die itself at ~3400 dpi.
DIE_RENDER_MAX_PX = 8000

# Fast default subset: the PAD_MAPPING.md worked example, the clean
# reference full-slot design, and the smallest die.
DEFAULT_DESIGNS = [
    "GD03_chip_top_6_6",     # 1x1 Racquet r2p0 — worked example in PAD_MAPPING.md
    "WSLG_chip_top_10_2",    # 1x1 wafer.space logo — cleanest render
    "TQVA_chip_top_14_8",    # 0.5x0.5 TinyQV — smallest padframe
]


def extract_design(layout: kdb.Layout, lv, name: str, force: bool) -> dict:
    """Run the sibling pipeline for one design cell and return its data dict."""
    cell = layout.cell(name)
    if cell is None:
        raise SystemExit(f"design cell {name!r} not found in reticle.oas")

    # GDS-native frame first: net labels must be matched before rotation.
    pads = extract_pads(cell, layout)
    labels = extract_labels(cell, layout)
    assign_net_names(pads, labels)

    bb = cell.bbox()
    die_bb = (
        bb.left * layout.dbu,
        bb.bottom * layout.dbu,
        bb.right * layout.dbu,
        bb.top * layout.dbu,
    )

    pads = [p for p in pads if _is_peripheral(p, *die_bb)]
    # 180° display rotation — after this, coordinates are in the frame that
    # maps 1:1 onto the COB padring (both 180° rotations cancel).
    pads = [_rotate_pad_180(p, die_bb) for p in pads]
    _number_pads_ccw(pads, die_bb)

    slot = computed_slot_size(die_bb[2] - die_bb[0], die_bb[3] - die_bb[1])

    # Two-tone die render, cached by cell name like the sibling repo.
    BG_DIR.mkdir(parents=True, exist_ok=True)
    bg_png = BG_DIR / f"{name}.png"
    if force or not bg_png.exists():
        render_gds_background(lv, name, layout, die_bb, bg_png,
                              max_px=DIE_RENDER_MAX_PX)
        # KLayout renders in GDS-native orientation; rotate to display frame.
        _rotate_image_180(bg_png)

    return {
        "name": name,
        "code": _project_code(name),
        "slot_size": slot,
        "die_bb_um": list(die_bb),
        "die_w_um": die_bb[2] - die_bb[0],
        "die_h_um": die_bb[3] - die_bb[1],
        "bg_png": str(bg_png.relative_to(REPO)),
        "pads": [
            {
                "n": p.num,
                "edge": _classify_edge(p, *die_bb),
                "net": p.net,
                "x0_um": p.x0, "y0_um": p.y0,
                "x1_um": p.x1, "y1_um": p.y1,
                "cx_um": p.cx, "cy_um": p.cy,
            }
            for p in sorted(pads, key=lambda p: p.num)
        ],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--designs", nargs="*", default=DEFAULT_DESIGNS,
                    help="design cell names (default: smoke subset)")
    ap.add_argument("--all", action="store_true",
                    help="extract every design on the reticle")
    ap.add_argument("--force", action="store_true",
                    help="re-render cached GDS backgrounds")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    layout = kdb.Layout()
    print(f"Reading {OAS} ...")
    t0 = time.time()
    layout.read(str(OAS))
    print(f"  loaded in {time.time() - t0:.1f}s — {layout.cells()} cells")

    designs = args.designs
    if args.all:
        top = next(iter(layout.top_cells()))
        skip = {"RETICLE_FILL", "TEXT"}
        designs = sorted({inst.cell.name for inst in top.each_inst()
                          if inst.cell.name not in skip})

    lv = setup_layout_view(layout)

    out = []
    for i, name in enumerate(designs, start=1):
        t0 = time.time()
        d = extract_design(layout, lv, name, args.force)
        out.append(d)
        labelled = sum(1 for p in d["pads"] if p["net"])
        print(f"  [{i}/{len(designs)}] {name}: slot {d['slot_size']}  "
              f"{d['die_w_um']:.0f}x{d['die_h_um']:.0f} um  "
              f"{len(d['pads'])} pads, {labelled} labelled  "
              f"({time.time() - t0:.1f}s)")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1))
    print(f"wrote {args.out} ({len(out)} designs)")


if __name__ == "__main__":
    main()
