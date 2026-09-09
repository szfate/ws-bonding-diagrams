"""Render multi-page factory bonding diagrams.

Per design, one three-page PDF in bonding/:
  1. die pinout — the existing per-design PDF from the sibling repo
     (wafer-space-die-pad-diagrams/diagrams/), embedded via pypdf and
     scaled onto A4;
  2. die placement — the COB breakout rendered from tmp/cob/<variant>.json
     with the two-tone die render placed in the cavity. The die render is
     in display orientation (QR top-right) and the die is placed rotated
     180° from GDS, so display frame = placement frame (both rotations
     cancel; see PAD_MAPPING.md in the sibling repo);
  3. bonding — page 2 plus the bond wires, die pad n → COB pad n+1,
     colored by die net class (same palette as page 1).

All pages are A4 portrait.

Coordinates: padring frame in mm, plotted math-up — die centered on the
padring origin, KiCad y-down negated on y (see verify_mapping.py).

Per-page PNG previews land in tmp/pages/ for eyeballing.

Usage:
    uv run make_bonding_diagrams.py [--designs WSLG_chip_top_10_2 ...]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import matplotlib.image as mpimg
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.collections import LineCollection
from matplotlib.patches import Patch, Polygon, Rectangle
from pypdf import PdfReader, PdfWriter, PageObject, Transformation

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO.parent / "wafer-space-die-pad-diagrams"))

from make_diagrams import (  # noqa: E402  (sibling repo, path inserted above)
    PAD_COLORS,
    classify_net,
)
from plot_pcb import CLASS_COLORS, centered_rect, net_class  # noqa: E402
from verify_mapping import EXTRA_PCB_PADS  # noqa: E402

DEFAULT_PADS = REPO / "tmp" / "pads.json"
DEFAULT_COB = REPO / "tmp" / "cob" / "1x1.json"
OUT_DIR = REPO / "bonding"
PAGE_DIR = REPO / "tmp" / "pages"
SIBLING_DIAGRAMS = REPO.parent / "wafer-space-die-pad-diagrams" / "diagrams"

DEFAULT_DESIGNS = ["WSLG_chip_top_10_2"]

# Every page is A4 portrait (210×297 mm).
A4_W_IN, A4_H_IN = 210 / 25.4, 297 / 25.4
A4_W_PT, A4_H_PT = A4_W_IN * 72, A4_H_IN * 72

DIE_CLASS_LABELS = {
    "gnd_digital": "DVSS",
    "gnd_analog": "AVSS",
    "gnd": "GND / VSS",
    "pwr_digital": "DVDD",
    "pwr_analog": "AVDD",
    "pwr": "VDD",
    "signal": "signal",
}

PCB_CLASS_LABELS = {
    "GND": "GND",
    "VDD_IO": "VDD_IO",
    "VDD_CORE": "VDD_CORE",
    "PWR_AUX": "PWR_AUX",
    "signal": "signal",
}


def die_pad_mm(pad: dict, die_bb: list[float]) -> tuple[float, float, float, float]:
    """Die display-frame pad → plot-frame mm (cx, cy, w, h).

    Plot frame = padring frame with y flipped to math-up; in that frame
    display-frame +y maps directly to plot +y (see die_pad_to_pcb_mm in
    verify_mapping.py for the KiCad-side transform).
    """
    cx0 = 0.5 * (die_bb[0] + die_bb[2])
    cy0 = 0.5 * (die_bb[1] + die_bb[3])
    cx = (pad["cx_um"] - cx0) / 1000.0
    cy = (pad["cy_um"] - cy0) / 1000.0
    w = (pad["x1_um"] - pad["x0_um"]) / 1000.0
    h = (pad["y1_um"] - pad["y0_um"]) / 1000.0
    return cx, cy, w, h


def pinout_pdf_for(design: dict) -> Path:
    """Locate the sibling repo's committed pinout PDF for this design."""
    stem = f"{design['name']}_{design['slot_size']}"
    exact = SIBLING_DIAGRAMS / f"{stem}.pdf"
    if exact.exists():
        return exact
    matches = sorted(SIBLING_DIAGRAMS.glob(f"{design['name']}*.pdf"))
    if matches:
        return matches[0]
    raise SystemExit(
        f"no pinout PDF for {design['name']} in {SIBLING_DIAGRAMS} — "
        f"generate it in the sibling repo first")


def pinout_page_size(design: dict) -> tuple[float, float]:
    """Page 1's size in inches (informational; pages render on A4)."""
    reader = PdfReader(str(pinout_pdf_for(design)))
    box = reader.pages[0].mediabox
    return float(box.width) / 72.0, float(box.height) / 72.0


def a4_letterbox(page: PageObject) -> PageObject:
    """Scale a page uniformly to fit and center it on a blank A4 portrait."""
    box = page.mediabox
    w, h = float(box.width), float(box.height)
    s = min(A4_W_PT / w, A4_H_PT / h)
    tx = (A4_W_PT - w * s) / 2 - float(box.left) * s
    ty = (A4_H_PT - h * s) / 2 - float(box.bottom) * s
    out = PageObject.create_blank_page(width=A4_W_PT, height=A4_H_PT)
    out.merge_transformed_page(page, Transformation().scale(s, s).translate(tx, ty))
    return out


def board_bounds(cob: dict) -> tuple[float, float, float, float]:
    """Board outline extents in plot-frame mm (padring origin, y up)."""
    ox, oy = cob["padring"]["at_mm"]
    xs, ys = [], []
    for shape in cob["edge_cuts"]:
        if "start" in shape and "end" in shape:
            pts = [shape["start"], shape["end"]]
        elif "pts" in shape:
            pts = shape["pts"]
        else:
            continue
        for px, py in pts:
            xs.append(px - ox)
            ys.append(py - oy)
    return min(xs), -max(ys), max(xs), -min(ys)


def draw_board(ax: plt.Axes, cob: dict, fs: float, show_numbers: bool = True) -> None:
    """COB furniture: outline, courtyard, mask opening, bond pads."""
    ox, oy = cob["padring"]["at_mm"]

    # Board outline.
    for shape in cob["edge_cuts"]:
        if shape["type"] == "rect" and "start" in shape:
            (x0, y0), (x1, y1) = shape["start"], shape["end"]
            ax.add_patch(Rectangle(
                (min(x0, x1) - ox, -max(y0, y1) + oy),
                abs(x1 - x0), abs(y1 - y0),
                fill=False, edgecolor="black", lw=1.2 * fs, zorder=1))
        elif "pts" in shape:
            ax.add_patch(Polygon([(px - ox, -(py - oy)) for px, py in shape["pts"]],
                                 closed=True, fill=False, edgecolor="black",
                                 lw=1.2 * fs, zorder=1))

    # Footprint-local graphics: die courtyard ticks faint, solder-mask
    # opening as a dashed magenta outline (the keep-out the bonder cares
    # about). Wire-guide comments are verification-only — not on the pages.
    for layer, style in (("Dwgs.User", dict(color="#cccccc", lw=0.4 * fs)),
                         ("F.Mask", dict(color="#cc44aa", lw=0.7 * fs, ls="--"))):
        for shape in cob["graphics"].get(layer, []):
            if shape["type"] == "line" and "start" in shape:
                (x0, y0), (x1, y1) = shape["start"], shape["end"]
                ax.plot([x0, x1], [-y0, -y1], zorder=1, **style)
            elif shape["type"] == "rect" and "start" in shape:
                (x0, y0), (x1, y1) = shape["start"], shape["end"]
                ax.add_patch(Rectangle(
                    (min(x0, x1), -max(y0, y1)), abs(x1 - x0), abs(y1 - y0),
                    fill=False, zorder=1, **style))
            elif shape["type"] == "poly" or "pts" in shape:
                ax.add_patch(Polygon([(px, -py) for px, py in shape["pts"]],
                                     closed=True, fill=False, zorder=1, **style))

    # Bond pads: ring filled by pin class, GND extras (paddle + stitches)
    # dashed-outlined.
    for pad in cob["pads"]:
        x, y = pad["x_mm"], pad["y_mm"]
        w, h = pad["size_mm"]
        extra = pad["num"] in EXTRA_PCB_PADS
        poly = centered_rect(x, -y, w, h, -pad["rot_deg"])
        if extra:
            poly.set_facecolor("none")
            poly.set_linestyle("--")
            poly.set_linewidth(0.8 * fs)
        else:
            poly.set_facecolor(CLASS_COLORS[net_class(pad)])
            poly.set_alpha(0.95)
            poly.set_linewidth(0)
        poly.set_edgecolor(CLASS_COLORS[net_class(pad)])
        poly.set_zorder(4)
        ax.add_patch(poly)
        if show_numbers and not extra:
            ax.annotate(pad["num"], (x, -y), fontsize=4.2 * fs, ha="center",
                        va="center", color="black", zorder=7)


def draw_die(ax: plt.Axes, design: dict, fs: float, show_numbers: bool = True) -> None:
    """Die render in the cavity + pad rectangles colored by die net class."""
    die_bb = design["die_bb_um"]
    w_mm, h_mm = design["die_w_um"] / 1000.0, design["die_h_um"] / 1000.0

    img = mpimg.imread(REPO / design["bg_png"])
    # origin="upper": PNG top row = display-frame top = plot-frame top.
    ax.imshow(img, extent=(-w_mm / 2, w_mm / 2, -h_mm / 2, h_mm / 2),
              origin="upper", zorder=2, interpolation="bilinear")
    ax.add_patch(Rectangle((-w_mm / 2, -h_mm / 2), w_mm, h_mm, fill=False,
                           edgecolor="black", lw=0.8 * fs, zorder=2.5))

    for pad in design["pads"]:
        cx, cy, w, h = die_pad_mm(pad, die_bb)
        fill = PAD_COLORS[classify_net(pad["net"])][0]
        ax.add_patch(Rectangle((cx - w / 2, cy - h / 2), w, h,
                               facecolor=fill, edgecolor="black", lw=0.25 * fs,
                               zorder=4))
        if show_numbers:
            # Number just inside the pad, straight in from the pad's edge
            # (perpendicular to the die edge — pushing toward the die
            # center would drag corner numbers diagonally off their pads),
            # aligned away from the pad so the gap stays clear.
            # _classify_edge returns single letters T/L/B/R.
            gap = 0.13
            e = pad.get("edge")
            if e == "B":
                lx, ly, ha, va = cx, cy + h / 2 + gap, "center", "bottom"
            elif e == "L":
                lx, ly, ha, va = cx + w / 2 + gap, cy, "left", "center"
            elif e == "R":
                lx, ly, ha, va = cx - w / 2 - gap, cy, "right", "center"
            else:  # "T"
                lx, ly, ha, va = cx, cy - h / 2 - gap, "center", "top"
            ax.annotate(str(pad["n"]), (lx, ly), fontsize=3.2 * fs,
                        ha=ha, va=va, color="#111111", zorder=7)


def wire_segments(design: dict, cob: dict):
    """Wire fan: die pad n → COB pad n+1, start clipped to the die pad edge.

    Returns (segments, colors, lengths) in plot-frame mm.
    """
    die_bb = design["die_bb_um"]
    die_pads = {p["n"]: p for p in design["pads"]}
    ring = sorted((p for p in cob["pads"] if p["num"] not in EXTRA_PCB_PADS),
                  key=lambda p: int(p["num"]))

    segments, colors, lengths = [], [], []
    for pcb in ring:
        dp = die_pads.get(int(pcb["num"]) - 1)
        if dp is None:
            continue
        cx, cy, w, h = die_pad_mm(dp, die_bb)
        px, py = pcb["x_mm"], -pcb["y_mm"]

        # Start where the center-to-center ray exits the die pad rectangle.
        dx, dy = px - cx, py - cy
        ts = [abs(half / d) for half, d in ((w / 2, dx), (h / 2, dy)) if d]
        sx, sy = (cx, cy) if not ts else (cx + dx * min(ts), cy + dy * min(ts))

        segments.append([(sx, sy), (px, py)])
        lengths.append(math.hypot(dx, dy))
        colors.append(PAD_COLORS[classify_net(dp["net"])][0])
    return segments, colors, lengths


def two_legends(ax: plt.Axes, die_classes: list[str], pcb_classes: list[str],
                fs: float = 1.0) -> None:
    """Die-net legend and COB-pin legend, stacked below the axes."""
    die = [Patch(facecolor=PAD_COLORS[c][0], edgecolor="black", lw=0.3,
                 label=DIE_CLASS_LABELS[c]) for c in die_classes]
    pcb = [Patch(facecolor=CLASS_COLORS[c], label=PCB_CLASS_LABELS[c])
           for c in pcb_classes]
    # Two legends: add_artist pins the first one before the second call,
    # which would otherwise replace it.
    leg_pcb = ax.legend(handles=pcb, title="COB pin class", loc="upper left",
                        bbox_to_anchor=(0, -0.062), ncol=5, fontsize=6 * fs,
                        title_fontsize=6.5 * fs, frameon=False)
    ax.add_artist(leg_pcb)
    ax.legend(handles=die, title="die net class (pads + wires)",
              loc="upper left", bbox_to_anchor=(0, -0.006), ncol=7,
              fontsize=6 * fs, title_fontsize=6.5 * fs, frameon=False)


def build_page(design: dict, cob: dict, bonding: bool) -> plt.Figure:
    """Page 2 (placement) or page 3 (bonding): the shared mm frame on A4.

    fs scales fonts and line widths so the 8.5×10 in reference layout
    shrinks proportionally onto A4.
    """
    die_classes = sorted({classify_net(p["net"]) for p in design["pads"]},
                         key=list(DIE_CLASS_LABELS).index)
    pcb_classes = sorted({net_class(p) for p in cob["pads"]
                          if p["num"] not in EXTRA_PCB_PADS},
                         key=list(CLASS_COLORS).index)

    pw, ph = A4_W_IN, A4_H_IN
    fs = min(pw / 8.5, ph / 10.0)

    fig = plt.figure(figsize=(pw, ph))
    ax = fig.add_axes((0.03, 0.16, 0.94, 0.73))
    ax.set_aspect("equal")

    draw_board(ax, cob, fs)
    draw_die(ax, design, fs)
    lengths = []
    if bonding:
        segments, colors, lengths = wire_segments(design, cob)
        ax.add_collection(LineCollection(segments, colors=colors,
                                         linewidths=0.8 * fs, zorder=4.5))

    # Scale bar outside the top-left board corner.
    x0, y0, x1, y1 = board_bounds(cob)
    sb_x, sb_y = x0, y1 + 0.9
    ax.plot([sb_x, sb_x + 2], [sb_y, sb_y], color="black", lw=1.5 * fs)
    ax.annotate("2 mm", (sb_x + 1, sb_y), textcoords="offset points",
                xytext=(0, 4), ha="center", fontsize=6.5 * fs)

    ax.set_xlim(x0 - 1.2, x1 + 1.2)
    ax.set_ylim(y0 - 1.2, y1 + 2.2)
    ax.set_axis_off()

    name, slot = design["name"], design["slot_size"]
    if bonding:
        ax.set_title(f"{name} — bonding diagram (slot {slot})\n"
                     f"{len(segments)} wires, die pad n → COB pad n+1, "
                     f"length {min(lengths):.2f}–{max(lengths):.2f} mm",
                     fontsize=11 * fs)
    else:
        ax.set_title(f"{name} — die placement on COB (slot {slot})\n"
                     f"die {design['die_w_um']:.0f}×{design['die_h_um']:.0f} µm, "
                     f"QR top-right (rocket corner), padring origin at (0, 0) mm",
                     fontsize=11 * fs)
    two_legends(ax, die_classes, pcb_classes, fs)
    fig.text(0.01, 0.01, "inputs: reticle.oas (die) · 1x1-mezzanine.kicad_pcb (COB) "
             "· wafer-space-die-pad-diagrams (pinout, page 1)",
             fontsize=5.5 * fs, color="#888888")
    return fig


def render_design(design: dict, cob: dict) -> Path:
    """Write bonding/<name>_<slot>.pdf (3 pages) + per-page PNG previews."""
    OUT_DIR.mkdir(exist_ok=True)
    PAGE_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{design['name']}_{design['slot_size']}"

    pages_pdf = PAGE_DIR / f"{stem}_pages23.pdf"
    with PdfPages(pages_pdf) as pdf:
        for bonding in (False, True):
            fig = build_page(design, cob, bonding)
            pdf.savefig(fig)
            tag = "p3" if bonding else "p2"
            fig.savefig(PAGE_DIR / f"{stem}_{tag}.png", dpi=200)
            plt.close(fig)

    out = OUT_DIR / f"{stem}.pdf"
    writer = PdfWriter()
    writer.add_page(a4_letterbox(PdfReader(str(pinout_pdf_for(design))).pages[0]))
    writer.append(str(pages_pdf))
    with open(out, "wb") as f:
        writer.write(f)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--designs", nargs="*", default=DEFAULT_DESIGNS,
                    help="design cell names (default: WSLG)")
    ap.add_argument("--pads", type=Path, default=DEFAULT_PADS)
    ap.add_argument("--cob", type=Path, default=DEFAULT_COB)
    args = ap.parse_args()

    cob = json.loads(args.cob.read_text())
    designs = json.loads(args.pads.read_text())
    if args.designs:
        designs = [d for d in designs if d["name"] in args.designs]
    if not designs:
        raise SystemExit("no matching designs in tmp/pads.json — run extract_dies.py first")

    ring = [p for p in cob["pads"] if p["num"] not in EXTRA_PCB_PADS]
    for design in designs:
        if len(design["pads"]) != len(ring):
            print(f"SKIP {design['name']}: pad count mismatch "
                  f"(die {len(design['pads'])} vs COB {len(ring)})")
            continue
        out = render_design(design, cob)
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
