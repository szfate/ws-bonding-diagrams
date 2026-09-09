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
  3. bonding — page 2 plus the bond wires, die pad n → COB pad n,
     one black wire per pad. COB pads are numbered 0-based on the
     drawing to match the die (physical PCB pads are +1).

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
from matplotlib.lines import Line2D
from matplotlib.patches import Circle, Patch, Polygon, Rectangle
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

# Photoreal board look, front-layer view, in pastels: the board is
# background context, so everything PCB is light and desaturated and the
# saturated colors are reserved for what matters (pads, wires, die).
# Copper under the soldermask is muted; the F.Mask opening (the padring
# area) is drawn as exposed board with the same copper re-drawn on top,
# clipped to the opening.
PCB_STYLE = {
    "mask": "#cfe0d8",    # soldermask substrate (pastel mint)
    "pour": "#bdd6ca",    # GND pour under mask
    "trace": "#a5c3b5",   # front traces under mask
    "bare": "#e9e2d4",    # exposed laminate in the mask opening
    "cu": "#d9b38c",      # bare copper in the mask opening
    "via": "#c6cdcf",     # plated via barrel
    "drill": "#8b938f",   # via drill hole
    "pad": "#d4a94a",     # ENIG bond pad (kept saturated — focal)
    "pad_num": "#1a1a1a",
    "silk": "#faf9f4",
}

# Bond wire diameter (25 µm gold) — drawn true to page scale. One color
# for all wires: class-colored wires blended into the die/PCB palette.
WIRE_DIAMETER_MM = 0.025
WIRE_COLOR = "#111111"

# Wires land on the die-side edge of the PCB pad (real bonds land near
# the inner edge, and the center-set pad number stays legible), pushed
# this fraction of the pad's long-axis length further inside (min 0.03
# mm so the round wire cap sits fully on the gold).
LANDING_INSET_FRACTION = 0.10
LANDING_INSET_MIN_MM = 0.03


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


def data_pt_per_mm(ax: plt.Axes) -> float:
    """Page points per data mm, after aspect-equal box adjustment.

    Callers must have set the final limits first; the canvas draw lets
    the equal-aspect machinery shrink the axes box so the extent is the
    true applied scale.
    """
    fig = ax.figure
    fig.canvas.draw()  # idempotent; backend is Agg in this pipeline
    bb = ax.get_window_extent()
    x0, x1 = ax.get_xlim()
    return bb.width / (x1 - x0) * 72.0 / fig.dpi


def _draw_shapes(ax: plt.Axes, shapes: list, style: dict, zorder: float,
                 ox: float = 0.0, oy: float = 0.0,
                 pt_per_mm: float | None = None) -> None:
    """Outline-style graphics (line/rect/poly/circle) in global mm.

    With pt_per_mm, each shape's own KiCad stroke width is drawn true to
    scale; otherwise the linewidth in `style` is used as-is.
    """
    for shape in shapes:
        call_style = dict(style)
        if pt_per_mm is not None and shape.get("stroke_mm"):
            call_style["lw"] = max(shape["stroke_mm"] * pt_per_mm, 0.3)
        if shape["type"] == "line" and "start" in shape:
            (x0, y0), (x1, y1) = shape["start"], shape["end"]
            ax.plot([x0 - ox, x1 - ox], [-(y0 - oy), -(y1 - oy)], zorder=zorder, **call_style)
        elif shape["type"] == "rect" and "start" in shape:
            (x0, y0), (x1, y1) = shape["start"], shape["end"]
            ax.add_patch(Rectangle(
                (min(x0, x1) - ox, -max(y0, y1) + oy), abs(x1 - x0), abs(y1 - y0),
                fill=False, zorder=zorder, **call_style))
        elif shape["type"] == "poly" or "pts" in shape:
            ax.add_patch(Polygon([(px - ox, -(py - oy)) for px, py in shape["pts"]],
                                 closed=True, fill=False, zorder=zorder, **call_style))


def draw_board(ax: plt.Axes, cob: dict, fs: float, show_numbers: bool = True) -> None:
    """COB furniture: substrate, front copper, mask opening, bond pads.

    Front-layer view: soldermask substrate, muted F.Cu traces/pour/vias
    under the mask, and the padring's F.Mask opening rendered as exposed
    board with the copper beneath re-drawn bright, clipped to the opening.

    Linewidths that carry geometry (traces) must be in data scale, so
    callers set the axes limits first and pass pt_per_mm().
    """
    ox, oy = cob["padring"]["at_mm"]
    pt_per_mm = data_pt_per_mm(ax)

    # Substrate from Edge.Cuts (fill + outline).
    for shape in cob["edge_cuts"]:
        if shape["type"] == "rect" and "start" in shape:
            (x0, y0), (x1, y1) = shape["start"], shape["end"]
            ax.add_patch(Rectangle(
                (min(x0, x1) - ox, -max(y0, y1) + oy),
                abs(x1 - x0), abs(y1 - y0),
                facecolor=PCB_STYLE["mask"], edgecolor="#6f7a75",
                lw=1.2 * fs, zorder=0.5))
        elif "pts" in shape:
            ax.add_patch(Polygon([(px - ox, -(py - oy)) for px, py in shape["pts"]],
                                 closed=True, facecolor=PCB_STYLE["mask"],
                                 edgecolor="#6f7a75", lw=1.2 * fs, zorder=0.5))

    # Front copper under the mask: pour islands + traces (muted) + vias.
    pour_pts = [p["pts"] for p in cob.get("zone_polygons", [])
                if p["layer"] == "F.Cu"]
    for pts in pour_pts:
        ax.add_patch(Polygon([(px - ox, -(py - oy)) for px, py in pts],
                             closed=True, facecolor=PCB_STYLE["pour"],
                             edgecolor="none", zorder=1))
    f_segs = [(px - ox, -(py - oy))
              for s in cob.get("segments", []) if s["layer"] == "F.Cu"
              for px, py in (s["start"], s["end"])]
    # True-to-scale trace widths: KiCad mm → axes data scale.
    f_widths = [s.get("width", 0.1) * pt_per_mm
                for s in cob.get("segments", []) if s["layer"] == "F.Cu"]
    ax.add_collection(LineCollection(
        [(f_segs[i], f_segs[i + 1]) for i in range(0, len(f_segs), 2)],
        colors=PCB_STYLE["trace"], linewidths=f_widths, capstyle="round",
        zorder=1.2))
    for v in cob.get("vias", []):
        ax.add_patch(Circle((v["x_mm"] - ox, -(v["y_mm"] - oy)), v["size_mm"] / 2,
                            facecolor=PCB_STYLE["via"], edgecolor="none", zorder=1.4))
        if v.get("drill_mm"):
            ax.add_patch(Circle((v["x_mm"] - ox, -(v["y_mm"] - oy)), v["drill_mm"] / 2,
                                facecolor=PCB_STYLE["drill"], edgecolor="none",
                                zorder=1.45))

    # Solder-mask opening (footprint-local = plot frame): exposed board
    # with the copper beneath re-drawn bright, clipped to the opening.
    open_rect = None
    for shape in cob["graphics"].get("F.Mask", []):
        if shape["type"] == "rect" and "start" in shape:
            (x0, y0), (x1, y1) = shape["start"], shape["end"]
            open_rect = Rectangle(
                (min(x0, x1), -max(y0, y1)), abs(x1 - x0), abs(y1 - y0),
                facecolor=PCB_STYLE["bare"], edgecolor=PCB_STYLE["cu"],
                lw=0.5 * fs, zorder=1.8)
            ax.add_patch(open_rect)
    if open_rect is not None:
        for pts in pour_pts:
            p = Polygon([(px, -py) for px, py in pts], closed=True,
                        facecolor=PCB_STYLE["cu"], edgecolor="none", zorder=2)
            ax.add_patch(p)
            p.set_clip_path(open_rect)
        bright = LineCollection(
            [(f_segs[i], f_segs[i + 1]) for i in range(0, len(f_segs), 2)],
            colors=PCB_STYLE["cu"], linewidths=f_widths, capstyle="round",
            zorder=1.9)  # under the die render (2) — these route beneath it
        ax.add_collection(bright)
        bright.set_clip_path(open_rect)

    # Die courtyard ticks (faint) over the substrate.
    _draw_shapes(ax, cob["graphics"].get("Dwgs.User", []),
                 dict(color="#cccccc", lw=0.4 * fs), zorder=3)

    # Non-padring footprint copper (e.g. the logo's copper artwork):
    # filled shapes in global board frame, muted under the mask — then
    # redrawn bright where the same shape opens the mask (KiCad 8 puts
    # `(layers "F.Cu" "F.Mask")` on one poly = exposed copper).
    for shape in cob["board_graphics"].get("F.Cu", []):
        bright = "F.Mask" in shape.get("layers", ())
        fill = PCB_STYLE["cu"] if bright else PCB_STYLE["trace"]
        zo = 2.5 if bright else 1.15  # bright copper sits over the mask
        if shape["type"] == "poly" or "pts" in shape:
            ax.add_patch(Polygon([(px - ox, -(py - oy)) for px, py in shape["pts"]],
                                 closed=True, facecolor=fill, edgecolor="none",
                                 zorder=zo))
        elif shape["type"] == "rect" and "start" in shape:
            (sx0, sy0), (sx1, sy1) = shape["start"], shape["end"]
            ax.add_patch(Rectangle((min(sx0, sx1) - ox, -max(sy0, sy1) + oy),
                                   abs(sx1 - sx0), abs(sy1 - sy0),
                                   facecolor=fill, edgecolor="none", zorder=zo))
        elif shape["type"] == "circle" and "center" in shape:
            cx, cy = shape["center"]
            ex, ey = shape["end"]
            ax.add_patch(Circle((cx - ox, -(cy - oy)), math.hypot(ex - cx, ey - cy),
                                facecolor=fill, edgecolor="none", zorder=zo))

    # Board silkscreen (pin-1 marker, marking box) — global board frame,
    # unlike the footprint-local graphics above; strokes at their KiCad
    # widths. Eco layers are assembly planning, not bonding info — skipped.
    _draw_shapes(ax, cob["board_graphics"].get("F.SilkS", []),
                 dict(color=PCB_STYLE["silk"]), zorder=3, ox=ox, oy=oy,
                 pt_per_mm=pt_per_mm)

    # Bond pads: gold ENIG ring with a class-colored rim, GND extras
    # (paddle + stitches) dashed-outlined.
    for pad in cob["pads"]:
        x, y = pad["x_mm"], pad["y_mm"]
        w, h = pad["size_mm"]
        cls = CLASS_COLORS[net_class(pad)]
        poly = centered_rect(x, -y, w, h, -pad["rot_deg"])
        if pad["num"] in EXTRA_PCB_PADS:
            poly.set_facecolor("none")
            poly.set_edgecolor(PCB_STYLE["pad"])
            poly.set_linestyle("--")
            poly.set_linewidth(0.8 * fs)
        else:
            poly.set_facecolor(PCB_STYLE["pad"])
            poly.set_edgecolor(cls)
            poly.set_linewidth(1.0 * fs)
        poly.set_zorder(4)
        ax.add_patch(poly)
        if show_numbers and pad["num"] not in EXTRA_PCB_PADS:
            # Diagram numbering matches the die (0-based); physical PCB
            # pads are +1 (see the page footer note).
            ax.annotate(str(int(pad["num"]) - 1), (x, -y), fontsize=4.2 * fs,
                        ha="center", va="center", color=PCB_STYLE["pad_num"],
                        zorder=7)


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
    """Wire fan: die pad n → COB pad n (physical pad n+1), die-pad-edge start.

    Returns (segments, lengths) in plot-frame mm. Wires are drawn in a
    single color — class-colored wires blended into the die/PCB palette.
    """
    die_bb = design["die_bb_um"]
    die_pads = {p["n"]: p for p in design["pads"]}
    ring = sorted((p for p in cob["pads"] if p["num"] not in EXTRA_PCB_PADS),
                  key=lambda p: int(p["num"]))

    segments, lengths = [], []
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

        # End at the center of the PCB pad's die-facing edge, pulled
        # LANDING_INSET_MM inside so the tip sits on the gold. Padring
        # pads point radially — the long axis faces the die — so the
        # die-facing edge is the inner end of the long axis, signed by
        # the toward-die-center direction. (A ray-exit test picks the
        # side edge for corner pads, where the toward-die direction is
        # diagonal, scattering the landings mid-pad.)
        rr = math.radians(pcb["rot_deg"])
        dist_c = math.hypot(px, py)
        if dist_c:
            nx, ny = -px / dist_c, -py / dist_c
            # Pad rects are drawn at -rot (KiCad y-down → plot CCW);
            # express the toward-die direction in the pad's local frame.
            lx = nx * math.cos(rr) - ny * math.sin(rr)
            ly = nx * math.sin(rr) + ny * math.cos(rr)
            pw, ph = pcb["size_mm"]
            long_len = max(pw, ph)
            inset = max(LANDING_INSET_FRACTION * long_len, LANDING_INSET_MIN_MM)
            if pw >= ph:
                lpt = (math.copysign(pw / 2 - inset, lx), 0.0)
            else:
                lpt = (0.0, math.copysign(ph / 2 - inset, ly))
            # Local edge center → plot frame (rotation by -rr).
            ca, sa = math.cos(rr), -math.sin(rr)
            px += lpt[0] * ca - lpt[1] * sa
            py += lpt[0] * sa + lpt[1] * ca

        segments.append([(sx, sy), (px, py)])
        # Report the drawn wire's length (die pad edge → landing point),
        # not the center-to-center distance.
        lengths.append(math.hypot(px - sx, py - sy))
    return segments, lengths


def two_legends(ax: plt.Axes, die_classes: list[str], pcb_classes: list[str],
                fs: float = 1.0) -> None:
    """Die-net legend and COB-pin legend, stacked below the axes."""
    die = [Patch(facecolor=PAD_COLORS[c][0], edgecolor="black", lw=0.3,
                 label=DIE_CLASS_LABELS[c]) for c in die_classes]
    # COB pins: gold pads rimmed by class, plus the board furniture.
    pcb = [Patch(facecolor=PCB_STYLE["pad"], edgecolor=CLASS_COLORS[c], lw=1.2,
                 label=PCB_CLASS_LABELS[c]) for c in pcb_classes]
    pcb += [
        Patch(facecolor=PCB_STYLE["cu"], edgecolor="none", label="front Cu"),
        Line2D([], [], marker="o", ms=4, mfc=PCB_STYLE["via"], mec="none",
               ls="none", label="via"),
    ]
    # Two legends: add_artist pins the first one before the second call,
    # which would otherwise replace it.
    leg_pcb = ax.legend(handles=pcb, title="COB pin class / board",
                        loc="upper left", bbox_to_anchor=(0, -0.062), ncol=7,
                        fontsize=6 * fs, title_fontsize=6.5 * fs, frameon=False)
    ax.add_artist(leg_pcb)
    ax.legend(handles=die, title="die net class (pads)",
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

    # Final limits first: data-scale linewidths (traces, bond wires)
    # need the applied axes transform.
    x0, y0, x1, y1 = board_bounds(cob)
    ax.set_xlim(x0 - 1.2, x1 + 1.2)
    ax.set_ylim(y0 - 1.2, y1 + 4.3)

    draw_board(ax, cob, fs)
    draw_die(ax, design, fs)
    lengths = []
    if bonding:
        segments, lengths = wire_segments(design, cob)
        ax.add_collection(LineCollection(
            segments, colors=WIRE_COLOR,
            linewidths=WIRE_DIAMETER_MM * data_pt_per_mm(ax), zorder=4.5))

    # Scale bar outside the top-left board corner.
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
                     f"{len(segments)} wires, die pad n → COB pad n, "
                     f"length {min(lengths):.2f}–{max(lengths):.2f} mm",
                     fontsize=11 * fs)
    else:
        ax.set_title(f"{name} — die placement on COB (slot {slot})\n"
                     f"die {design['die_w_um']:.0f}×{design['die_h_um']:.0f} µm, "
                     f"QR top-right (rocket corner), padring origin at (0, 0) mm",
                     fontsize=11 * fs)
    two_legends(ax, die_classes, pcb_classes, fs)
    fig.text(0.01, 0.01, "inputs: reticle.oas (die) · 1x1-mezzanine.kicad_pcb (COB) "
             "· wafer-space-die-pad-diagrams (pinout, page 1) · "
             "COB pads numbered 0–73 to match the die; physical PCB pads are +1",
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
