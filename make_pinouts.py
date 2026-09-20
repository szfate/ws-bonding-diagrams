"""Generate page-1 pinout PDFs for a reticle via the vendored renderer.

The sibling repo commits pinout PDFs only for run-1. For any other
reticle (or a design missing there) this regenerates them with the
vendored make_diagrams.render(), reusing extract_dies' cached GDS
backgrounds. Output mirrors the sibling naming so the bonding pipeline
can treat both sources uniformly:

    tmp/<reticle>/pinouts/<name>_<slot>.pdf  (+ .png/.svg)

Usage:
    uv run make_pinouts.py --reticle ws-run2 [--designs NAME ...] [--force]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import klayout.db as kdb

REPO = Path(__file__).resolve().parent

import make_diagrams  # noqa: E402  (vendored, see its module docstring)
from make_diagrams import (  # noqa: E402
    _is_peripheral,
    _number_pads_ccw,
    _rotate_image_180,
    _rotate_pad_180,
    assign_net_names,
    computed_slot_size,
    extract_labels,
    extract_pads,
    render,
    render_gds_background,
    setup_layout_view,
)
from extract_dies import (DIE_RENDER_MAX_PX, TMP_ROOT, resolve_reticle,
                          _rotate_pad_180_inplace)  # noqa: E402
from runs import run_config  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reticle", default="ws-run1",
                    help="sibling checkout dir name or path (default: ws-run1)")
    ap.add_argument("--designs", nargs="*", default=None,
                    help="subset of design names (default: all in pads.json)")
    ap.add_argument("--force", action="store_true",
                    help="re-render even if the PDF already exists")
    args = ap.parse_args()

    reticle_dir, oas = resolve_reticle(args.reticle)
    reticle = reticle_dir.name
    cfg = run_config(reticle)
    display_rot = cfg["page1_die_rotation_deg"] == 180
    # The vendored module resolves its layer props from a module-level
    # path; point it at this reticle before building the layout view.
    make_diagrams.LYP = reticle_dir / "lyp" / "gf180mcu.lyp"

    pads_path = TMP_ROOT / reticle / "pads.json"
    if not pads_path.exists():
        raise SystemExit(f"no {pads_path} — run extract_dies.py "
                         f"--all --reticle {reticle} first")
    designs = json.loads(pads_path.read_text())
    if args.designs:
        wanted = set(args.designs)
        designs = [d for d in designs if d["name"] in wanted]

    out_dir = TMP_ROOT / reticle / "pinouts"
    out_dir.mkdir(parents=True, exist_ok=True)
    bg_dir = TMP_ROOT / reticle / "gds_renders"

    layout = kdb.Layout()
    print(f"Reading {oas} ...")
    t0 = time.time()
    layout.read(str(oas))
    print(f"  loaded in {time.time() - t0:.1f}s — {layout.cells()} cells")
    lv = setup_layout_view(layout)

    for i, d in enumerate(designs, start=1):
        name = d["name"]
        cell = layout.cell(name)
        if cell is None:
            print(f"  [{i}/{len(designs)}] {name}: cell not found, skipping")
            continue

        # Same pipeline as the vendored module's main(): pads native →
        # peripheral filter → display rotation (if configured) → CCW
        # numbering; numbering is QR-anchored and only knows the display
        # frame, so GDS-frame output rotates back after numbering.
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
        if display_rot:
            pads = [_rotate_pad_180(p, die_bb) for p in pads]
        _number_pads_ccw(pads, die_bb)
        if not display_rot:
            for p in pads:
                _rotate_pad_180_inplace(p, die_bb)
        slot = computed_slot_size(die_bb[2] - die_bb[0], die_bb[3] - die_bb[1])

        stem = f"{name}_{slot}"
        out_pdf = out_dir / f"{stem}.pdf"
        if out_pdf.exists() and not args.force:
            print(f"  [{i}/{len(designs)}] {name}: cached {out_pdf.name}")
            continue

        bg_png = bg_dir / f"{name}.png"
        if not bg_png.exists():
            render_gds_background(lv, name, layout, die_bb, bg_png,
                                  max_px=DIE_RENDER_MAX_PX)
            if display_rot:
                _rotate_image_180(bg_png)

        t0 = time.time()
        render(name, pads, die_bb, out_dir / f"{stem}.png",
               out_dir / f"{stem}.svg", out_pdf, background_image=bg_png,
               rotated=display_rot)
        labelled = sum(1 for p in pads if p.net)
        print(f"  [{i}/{len(designs)}] {name}: {len(pads)} pads, "
              f"{labelled} labelled  slot {slot}  ({time.time() - t0:.1f}s)")

    print(f"wrote {len(list(out_dir.glob('*.pdf')))} pinout PDFs to {out_dir}")


if __name__ == "__main__":
    main()
