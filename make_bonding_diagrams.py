"""Render multi-page factory bonding diagrams.

Per design, one three-page PDF in bonding-diagrams/, every page on the shared
document template (page title + wafer.space logo in the header, page
number in the footer):
  1. die pad diagram — the sibling repo's per-design pinout PDF
     (wafer-space-die-pad-diagrams/diagrams/) with its title header and
     info-panel footer cropped away (the crop is located by measuring
     content bands in the committed 180-dpi PNG, which shares the PDF's
     figure geometry), merged into the template page via pypdf;
  2. die placement — the COB breakout rendered from tmp/cob/<variant>.json
     with the two-tone die render placed in the cavity, plus the
     orientation indicator: the die's QR cell highlighted with a
     magnified inset, the board's rocket logo ringed, and the
     bilingual (EN/中文) align-QR-to-rocket note. The die render is in
     display orientation (QR top-right) and the die is placed rotated
     180° from GDS, so display frame = placement frame (both rotations
     cancel; see PAD_MAPPING.md in the sibling repo);
  3. bonding — page 2 plus the bond wires, one black wire per pad.
     COB pads are numbered 0-based on the drawing to match the die
     (physical PCB pads are +1).

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
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import matplotlib.font_manager as font_manager
import matplotlib.image as mpimg
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.collections import LineCollection
from matplotlib.lines import Line2D
from matplotlib.patches import Circle, Patch, PathPatch, Polygon, Rectangle
from matplotlib.path import Path as MplPath
from PIL import Image
from pypdf import PdfReader, PdfWriter, PageObject, Transformation
from pypdf.generic import RectangleObject

REPO = Path(__file__).resolve().parent

from make_diagrams import (  # noqa: E402  (vendored, see its module header)
    PAD_COLORS,
    WSIP_CELL_UM,
    _wsip_corners,
    classify_net,
)
from plot_pcb import CLASS_COLORS, centered_rect, net_class  # noqa: E402
from verify_mapping import extra_pads  # noqa: E402
from boards import find_pads, load_boards  # noqa: E402

DEFAULT_COB = REPO / "tmp" / "cob" / "1x1.json"
OUT_DIR = REPO / "bonding-diagrams"
TMP_ROOT = REPO / "tmp"
SIBLING_DIAGRAMS = REPO.parent / "wafer-space-die-pad-diagrams" / "diagrams"
LOGO_PNG = REPO / "logo-raw.webp"

DEFAULT_DESIGNS = ["WSLG_chip_top_10_2"]

# Every page is A4 portrait (210×297 mm).
A4_W_IN, A4_H_IN = 210 / 25.4, 297 / 25.4
A4_W_PT, A4_H_PT = A4_W_IN * 72, A4_H_IN * 72

# Document template geometry (figure fractions): title + logo in the
# header above a hairline rule, note + page number in the footer.
HEADER_TITLE_Y = 0.968
HEADER_SUB_Y = 0.944
HEADER_RULE_Y = 0.930
LOGO_W_FRAC = 0.125
# Right edge for logo / page counter — mirrors the 0.035 left margin
# (7.4 mm in from the paper edge; the footer counter right-aligns here too).
LOGO_RIGHT_FRAC = 0.965
FOOTER_Y = 0.012
PAGE_TOTAL = 3
# Generation stamp for the footer — one value per run so every page of a
# batch carries the same timestamp.
GENERATED_STAMP = datetime.now().strftime("%Y-%m-%d %H:%M")
# Shared axes rect for the mm frame on pages 2–3 (top edge = 0.900).
# The 0.030 band between the axes top and the header rule carries the
# orientation note — tall enough that the note floats clear of the rule
# instead of hugging it.
AXES_RECT = (0.03, 0.15, 0.94, 0.75)
# Content area below the header rule where page 1's cropped pinout is
# placed (PDF pt, origin bottom-left).
CONTENT_BOX_PT = (30.0, 45.0, A4_W_PT - 30.0, 0.915 * A4_H_PT)

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

# Raster resample target for savefig to PDF. matplotlib embeds imshow
# images resampled to (displayed inches × savefig dpi) — the figure dpi
# default (100) crushed the 8000 px die render to ~90×117 px. At 2400
# the die lands at ~2800–3400 px embedded (still downsampled from the
# 8000 px source, ≈1000+ dpi on paper) and the QR zoom inset at ~1750 px.
# Vector content is unaffected; only raster artists use this grid.
# The die imshows must use interpolation="nearest" alongside this: any
# smoothing filter invents gray values in the two-tone dithered render,
# which flate cannot compress (GD03's die stream alone was 10.9 MB).
PDF_RASTER_DPI = 2400

# Placement-page orientation indicator: dark red fiducial marks — a
# highlight box + magnified inset on the die's QR cell, a ring on the
# board's rocket logo (the red pad rims are brighter orange-red, so the
# maroon still reads as a callout, not a class).
FIDUCIAL_COLOR = "#9b1b1b"
# Factory-facing callouts in English and Chinese (matplotlib falls back
# per glyph to the first installed CJK family — see cjk_families()).
# The die QR aligns to the board's rocket logo — except when the padring
# carries its own copper circle marker next to pad 0 (0.5x1 board, where
# the rocket sits in the opposite corner by design).
FIDUCIAL_NOTE_ROCKET = "ALIGN DIE QR CODE WITH BOARD ROCKET"
FIDUCIAL_NOTE_ROCKET_ZH = "将芯片二维码与板上火箭对齐"
FIDUCIAL_NOTE_CIRCLE = "ALIGN DIE QR CODE WITH COPPER CIRCLE NEXT TO PAD 0"
FIDUCIAL_NOTE_CIRCLE_ZH = "将芯片二维码与焊盘0旁的铜圆点对齐"
QR_LABEL = "die QR"
QR_LABEL_ZH = "芯片二维码"
ROCKET_LABEL = "board rocket"
ROCKET_LABEL_ZH = "板上火箭"
CIRCLE_LABEL = "copper circle"
CIRCLE_LABEL_ZH = "铜圆点"
QR_ZOOM_VIEW_MM = 0.28   # inset view width (plot mm), centred on the QR cell
QR_ZOOM_W_FRAC = 0.088   # inset width as figure fraction (square on paper)


def cjk_families() -> list[str]:
    """Installed font families that cover Chinese, for per-glyph fallback.

    Matplotlib ≥3.6 walks a fontfamily list per glyph, so Latin text
    keeps the document font while the Chinese picks the first available
    family here (macOS system faces first, then Noto).
    """
    installed = {f.name for f in font_manager.fontManager.ttflist}
    return [n for n in ("Hiragino Sans GB", "PingFang SC", "Noto Sans SC",
                        "Arial Unicode MS", "Heiti TC", "Songti SC")
            if n in installed]


CJK_FAMILIES = cjk_families()

def board_extras(cob: dict) -> set[str]:
    """Mechanical/non-bond pads on this board (paddle + thru-holes).

    Board-specific: the run-1 boards number the extras 75..81, TQVA 57..63.
    Stored per-board in the JSON as "extra_nums"; legacy exports (1x1)
    without the field fall back to the run-1 set.
    """
    return set(cob.get("extra_nums") or {"75", "76", "77", "78", "79", "80", "81"})


# Wires land on the die-side edge of the PCB pad (real bonds land near
# the inner edge, and the center-set pad number stays legible), pushed
# this fraction of the pad's long-axis length further inside (min 0.03
# mm so the round wire cap sits fully on the gold).
LANDING_INSET_FRACTION = 0.10
LANDING_INSET_MIN_MM = 0.03


def die_pad_mm(pad: dict, die_bb: list[float]) -> tuple[float, float, float, float]:
    """Die display-frame pad → plot-frame mm (cx, cy, w, h).

    The die display frame (GDS rotated 180°, QR top-right) maps 1:1
    onto the board frame: the die is placed 180°-rotated relative to
    GDS in the cavity, which cancels the y flip into the y-up plot
    frame (see PAD_MAPPING.md §4 in the sibling repo).
    """
    cx0 = 0.5 * (die_bb[0] + die_bb[2])
    cy0 = 0.5 * (die_bb[1] + die_bb[3])
    cx = (pad["cx_um"] - cx0) / 1000.0
    cy = (pad["cy_um"] - cy0) / 1000.0
    w = (pad["x1_um"] - pad["x0_um"]) / 1000.0
    h = (pad["y1_um"] - pad["y0_um"]) / 1000.0
    return cx, cy, w, h


def min_pad_pitch_mm(design: dict) -> float:
    """Smallest centre-to-centre pitch between adjacent same-edge pads, mm.

    Drives the die pad-label font: labels sit one per pad along each die
    edge, so the pitch — not the page size — is the hard width budget.
    """
    die_bb = design["die_bb_um"]
    by_edge: dict[str, list[float]] = {}
    for pad in design["pads"]:
        cx, cy, _, _ = die_pad_mm(pad, die_bb)
        along = cx if pad.get("edge") in ("T", "B") else cy
        by_edge.setdefault(pad.get("edge") or "T", []).append(along)
    pitch = min((b - a for pts in by_edge.values() for a, b in zip(sorted(pts), sorted(pts)[1:])), default=0.1)
    return max(pitch, 1e-3)


def pinout_pdf_for(design: dict) -> Path:
    """Locate the page-1 pinout PDF for this design.

    Generated per reticle in tmp/<reticle>/pinouts/ (see make_pinouts.py)
    takes precedence; the sibling repo's committed run-1 PDFs are the
    fallback. Only matches carrying the design's exact slot suffix count:
    design names repeat across reticles with different padframes (run-1
    CAFE is 1x1, run-2 CAFE is 0.5x0.5), so a bare name match could
    silently embed the other die's pinout.
    """
    reticle = design.get("reticle", "ws-run1")
    suffix = f"_{design['slot_size']}.pdf"
    for folder in (TMP_ROOT / reticle / "pinouts", SIBLING_DIAGRAMS):
        matches = sorted(p for p in folder.glob(f"{design['name']}*{suffix}"))
        if matches:
            return matches[0]
    raise SystemExit(
        f"no pinout PDF for {design['name']} (slot {design['slot_size']}) — "
        f"run: uv run make_pinouts.py --reticle {reticle}")


_logo_arr = None


def _logo_img() -> np.ndarray:
    """wafer.space logo, cropped to its alpha bbox (loaded once)."""
    global _logo_arr
    if _logo_arr is None:
        if not LOGO_PNG.exists():
            raise SystemExit(f"missing {LOGO_PNG} — needed for the page header")
        img = mpimg.imread(LOGO_PNG)
        ys, xs = np.where(img[..., 3] > 2)
        _logo_arr = img[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    return _logo_arr


def add_header(fig: plt.Figure, title: str, subtitle: str) -> None:
    """Document template header: page title left, wafer.space logo right."""
    fig.text(0.035, HEADER_TITLE_Y, title, fontsize=13, fontweight="bold",
             ha="left", va="center")
    if subtitle:
        fig.text(0.035, HEADER_SUB_Y, subtitle, fontsize=7.5, color="#666666",
                 ha="left", va="center")
    fig.add_artist(Line2D([0.035, LOGO_RIGHT_FRAC], [HEADER_RULE_Y] * 2,
                          transform=fig.transFigure, color="#d8d8d8", lw=0.7))
    img = _logo_img()
    h_frac = LOGO_W_FRAC * (img.shape[0] / img.shape[1]) * (A4_W_IN / A4_H_IN)
    ax = fig.add_axes((LOGO_RIGHT_FRAC - LOGO_W_FRAC, HEADER_TITLE_Y - h_frac / 2,
                       LOGO_W_FRAC, h_frac))
    ax.imshow(img, aspect="auto", interpolation="bilinear")
    ax.set_axis_off()


def add_footer(fig: plt.Figure, page_num: int) -> None:
    """Document template footer: generation stamp left, page number right."""
    fig.text(0.035, FOOTER_Y, f"generated on {GENERATED_STAMP}",
             fontsize=6.5, color="#888888", ha="left", va="bottom")
    fig.text(LOGO_RIGHT_FRAC, FOOTER_Y, f"page {page_num} / {PAGE_TOTAL}",
             fontsize=6.5, color="#888888", ha="right", va="bottom")


def pinout_crop_pt(design: dict) -> tuple[float, float, float, float]:
    """(left, bottom, right, top) crop rect in PDF pt, isolating the die shot.

    The sibling diagram carries a title header and an info-panel footer
    that this document replaces with its own template. Rather than
    duplicating the sibling's layout constants, measure content bands
    from the committed 180-dpi PNG (same figure geometry as the PDF) and
    cut midway through the first and last whitespace gaps. Falls back to
    the full page (with a warning) if the bands don't read as
    header / plot / footer.
    """
    pdf_path = pinout_pdf_for(design)
    png = pdf_path.with_suffix(".png")
    if not png.exists():
        raise SystemExit(f"missing {png.name} next to the pinout PDF — "
                         f"the crop locates the header/footer bands in it")
    box = PdfReader(str(pdf_path)).pages[0].mediabox
    im = np.asarray(Image.open(png).convert("L"))
    dpi = im.shape[0] / (float(box.height) / 72.0)

    dark_rows = (im < 128).sum(axis=1) >= 2
    gap = int(0.07 * dpi)  # blank run long enough to split content bands
    bands, start, blank = [], None, 0
    for i, has in enumerate(dark_rows):
        if has:
            if start is None:
                start = i
            blank = 0
        elif start is not None:
            blank += 1
            if blank >= gap:
                bands.append((start, i - blank + 1))
                start = None
    if start is not None:
        bands.append((start, im.shape[0]))

    if (len(bands) < 2
            or bands[0][1] - bands[0][0] > 1.5 * dpi   # title, ≤ a few lines
            or bands[-1][1] - bands[-1][0] > 3.3 * dpi):  # info panel
        print(f"WARN: {png.name}: unexpected content bands — embedding full page")
        return 0.0, 0.0, float(box.width), float(box.height)

    top_row = (bands[0][1] + bands[1][0]) // 2 if len(bands) >= 3 else bands[0][1]
    bottom_row = (bands[-2][1] + bands[-1][0]) // 2 if len(bands) >= 3 else bands[-1][0]
    pt_per_px = float(box.height) / im.shape[0]
    top = float(box.height) - top_row * pt_per_px
    bottom = float(box.height) - bottom_row * pt_per_px
    return float(box.left), bottom, float(box.right), top


def build_pinout_page(design: dict) -> PageObject:
    """Page 1: template page with the cropped pinout PDF merged into it.

    pypdf clips the merged page to its cropbox (page_merge_box defaults
    to cropbox), so setting the crop rect is the whole crop.
    """
    fig = plt.figure(figsize=(A4_W_IN, A4_H_IN))
    code = design["name"].split("_")[0]
    add_header(fig, f"{code} · die pad diagram",
               f"{design['name']} · slot {design['slot_size']}")
    add_footer(fig, 1)
    _, page_dir = out_dirs(design)
    template = page_dir / f"{design['name']}_{design['slot_size']}_p1_template.pdf"
    with PdfPages(template) as pdf:
        # Same raster resample target as the other pages — the default
        # figure dpi (100) crushes the header logo to ~100 dpi.
        pdf.savefig(fig, dpi=PDF_RASTER_DPI)
    plt.close(fig)

    page = PageObject.create_blank_page(width=A4_W_PT, height=A4_H_PT)
    page.merge_page(PdfReader(str(template)).pages[0])

    src = PdfReader(str(pinout_pdf_for(design))).pages[0]
    left, bottom, right, top = pinout_crop_pt(design)
    src.cropbox = RectangleObject([left, bottom, right, top])
    w, h = right - left, top - bottom
    cx0, cy0, cx1, cy1 = CONTENT_BOX_PT
    s = min((cx1 - cx0) / w, (cy1 - cy0) / h)
    tx = cx0 + ((cx1 - cx0) - w * s) / 2 - left * s
    ty = cy0 + ((cy1 - cy0) - h * s) / 2 - bottom * s
    page.merge_transformed_page(src, Transformation().scale(s, s).translate(tx, ty))
    return page


def to_plot(cob: dict, gx: float, gy: float) -> tuple[float, float]:
    """KiCad board-global mm → plot-frame mm (padring-centred, y up).

    The plot frame is pcbnew's front view: file millimetres translated
    to the padring origin, y negated to math-up (small file y = board
    top = plot top). Everything global — edge cuts, pours, traces,
    vias, silks, the rocket, and the bond pads' gx/gy — goes through
    here; the padring footprint's own local shapes go through
    local_to_plot instead.
    """
    ox, oy = cob["padring"]["at_mm"]
    return gx - ox, -(gy - oy)


def local_to_plot(cob: dict, lx: float, ly: float) -> tuple[float, float]:
    """Padring-footprint-local mm → plot frame.

    Forward placement rotation (same matrix as parse_pcb.py's global
    placement) followed by to_plot — the footprint-local shapes (mask
    opening) sit in the padring's own frame, which is rotated by
    rot_deg on the board.
    """
    ox, oy = cob["padring"]["at_mm"]
    r = math.radians(cob["padring"]["rot_deg"])
    gx = ox + lx * math.cos(r) - ly * math.sin(r)
    gy = oy + lx * math.sin(r) + ly * math.cos(r)
    return gx - ox, -(gy - oy)


def board_bounds(cob: dict) -> tuple[float, float, float, float]:
    """Board outline extents in plot-frame mm (padring origin, y up)."""
    xs, ys = [], []
    for shape in cob["edge_cuts"]:
        if "start" in shape and "end" in shape:
            pts = [shape["start"], shape["end"]]
        elif "pts" in shape:
            pts = shape["pts"]
        else:
            continue
        for px, py in pts:
            xs.append(to_plot(cob, px, py)[0])
            ys.append(to_plot(cob, px, py)[1])
    return min(xs), min(ys), max(xs), max(ys)


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


def _arc_points(start: tuple, mid: tuple, end: tuple, n: int = 33) -> list:
    """Sample an arc through three plot-frame points, sweeping the way the
    mid point lies (KiCad arcs are defined by start/mid/end with no explicit
    direction). Returned as n (x, y) points."""

    def circumcenter(p1, p2, p3):
        d = 2 * (p1[0] * (p2[1] - p3[1]) + p2[0] * (p3[1] - p1[1])
                 + p3[0] * (p1[1] - p2[1]))
        ux = ((p1[0]**2 + p1[1]**2) * (p2[1] - p3[1])
              + (p2[0]**2 + p2[1]**2) * (p3[1] - p1[1])
              + (p3[0]**2 + p3[1]**2) * (p1[1] - p2[1])) / d
        uy = ((p1[0]**2 + p1[1]**2) * (p3[0] - p2[0])
              + (p2[0]**2 + p2[1]**2) * (p1[0] - p3[0])
              + (p3[0]**2 + p3[1]**2) * (p2[0] - p1[0])) / d
        return ux, uy

    ux, uy = circumcenter(start, mid, end)
    r = math.hypot(start[0] - ux, start[1] - uy)
    a1 = math.atan2(start[1] - uy, start[0] - ux)
    a_mid = (math.atan2(mid[1] - uy, mid[0] - ux) - a1) % (2 * math.pi)
    a_end = (math.atan2(end[1] - uy, end[0] - ux) - a1) % (2 * math.pi)
    # Sweep CCW to end only if mid lies on the CCW half of that path —
    # otherwise the arc goes clockwise past mid the other way around.
    sweep = a_end if a_mid <= a_end else a_end - 2 * math.pi
    return [(ux + r * math.cos(a1 + sweep * i / (n - 1)),
             uy + r * math.sin(a1 + sweep * i / (n - 1)))
            for i in range(n)]


def _draw_shapes(ax: plt.Axes, shapes: list, style: dict, zorder: float,
                 cob: dict | None = None,
                 pt_per_mm: float | None = None,
                 outline_only: tuple = (),
                 xf=None) -> None:
    """Outline-style graphics (line/rect/poly/circle/arc) in global mm.

    With pt_per_mm, each shape's own KiCad stroke width is drawn true to
    scale; otherwise the linewidth in `style` is used as-is. Shapes KiCad
    marks fill=yes (e.g. the logo's silk artwork) are drawn filled with
    the style color and no edge — except `outline_only` types, which
    always draw as outlines (the fab's serial-number marking box is
    fill=yes in KiCad but is a placeholder frame, not painted silk). `xf`
    overrides the point transform (footprint-local graphics).
    """

    def pt(p: tuple) -> tuple:
        if xf:
            return xf(p)
        return to_plot(cob, *p) if cob else (p[0], -p[1])

    for shape in shapes:
        call_style = dict(style)
        if pt_per_mm is not None and shape.get("stroke_mm"):
            call_style["lw"] = max(shape["stroke_mm"] * pt_per_mm, 0.3)
        filled = bool(shape.get("fill")) and shape["type"] not in outline_only
        if shape["type"] == "line" and "start" in shape:
            (x0, y0), (x1, y1) = pt(shape["start"]), pt(shape["end"])
            ax.plot([x0, x1], [y0, y1], zorder=zorder, **call_style)
        elif shape["type"] == "rect" and "start" in shape:
            (x0, y0) = pt(shape["start"])
            (x1, y1) = pt(shape["end"])
            if filled:
                ax.add_patch(Rectangle(
                    (min(x0, x1), min(y0, y1)), abs(x1 - x0), abs(y1 - y0),
                    facecolor=call_style.pop("color"), edgecolor="none",
                    zorder=zorder, **call_style))
            else:
                ax.add_patch(Rectangle(
                    (min(x0, x1), min(y0, y1)), abs(x1 - x0), abs(y1 - y0),
                    fill=False, zorder=zorder, **call_style))
        elif shape["type"] == "poly" or "pts" in shape:
            if filled:
                ax.add_patch(Polygon([pt(p) for p in shape["pts"]],
                                     closed=True, facecolor=call_style.pop("color"),
                                     edgecolor="none", zorder=zorder, **call_style))
            else:
                ax.add_patch(Polygon([pt(p) for p in shape["pts"]],
                                     closed=True, fill=False, zorder=zorder, **call_style))
        elif shape["type"] == "circle" and "center" in shape:
            (cx, cy) = pt(shape["center"])
            (ex, ey) = pt(shape["end"])
            r = math.hypot(ex - cx, ey - cy)
            if filled:
                ax.add_patch(Circle((cx, cy), r,
                                    facecolor=call_style.pop("color"),
                                    edgecolor="none", zorder=zorder,
                                    **call_style))
            else:
                ax.add_patch(Circle((cx, cy), r, fill=False,
                                    zorder=zorder, **call_style))
        elif shape["type"] == "arc" and "mid" in shape:
            pts = _arc_points(pt(shape["start"]), pt(shape["mid"]),
                              pt(shape["end"]))
            ax.plot([p[0] for p in pts], [p[1] for p in pts],
                    zorder=zorder, **call_style)


def draw_board(ax: plt.Axes, cob: dict, fs: float, show_numbers: bool = True,
               extras: set[str] = frozenset(),
               bond_labels: dict[str, str] | None = None) -> None:
    """COB furniture: substrate, front copper, mask opening, bond pads.

    Front-layer view: soldermask substrate, muted F.Cu traces/pour/vias
    under the mask, and the padring's F.Mask opening rendered as exposed
    board with the copper beneath re-drawn bright, clipped to the opening.

    Linewidths that carry geometry (traces) must be in data scale, so
    callers set the axes limits first and pass pt_per_mm().
    """
    pt_per_mm = data_pt_per_mm(ax)

    # Substrate from Edge.Cuts (fill + outline).
    for shape in cob["edge_cuts"]:
        if shape["type"] == "rect" and "start" in shape:
            (x0, y0) = to_plot(cob, *shape["start"])
            (x1, y1) = to_plot(cob, *shape["end"])
            ax.add_patch(Rectangle(
                (min(x0, x1), min(y0, y1)),
                abs(x1 - x0), abs(y1 - y0),
                facecolor=PCB_STYLE["mask"], edgecolor="#6f7a75",
                lw=1.2 * fs, zorder=0.5))
        elif "pts" in shape:
            ax.add_patch(Polygon([to_plot(cob, px, py) for px, py in shape["pts"]],
                                 closed=True, facecolor=PCB_STYLE["mask"],
                                 edgecolor="#6f7a75", lw=1.2 * fs, zorder=0.5))

    # Front copper under the mask: pour islands + traces (muted) + vias.
    # The padring's GND stitch "pads" (76-81) are plated thru-holes like
    # any via — KiCad models them as circle pads, but they render as
    # holes: barrel + drill, both here and in the exposed-mask pass.
    # The via loops take board-global coords; parse_pcb already resolved
    # the padring pads' global placement into gx/gy.
    stitch_vias = [{"x_mm": p["gx_mm"], "y_mm": p["gy_mm"],
                    "size_mm": p["size_mm"][0], "drill_mm": p.get("drill_mm")}
                   for p in cob["pads"]
                   if p["num"] in extras and p["type"] == "thru_hole"]
    pour_pts = [p["pts"] for p in cob.get("zone_polygons", [])
                if p["layer"] == "F.Cu"]
    for pts in pour_pts:
        ax.add_patch(Polygon([to_plot(cob, px, py) for px, py in pts],
                             closed=True, facecolor=PCB_STYLE["pour"],
                             edgecolor="none", zorder=1))
    f_segs = [to_plot(cob, px, py)
              for s in cob.get("segments", []) if s["layer"] == "F.Cu"
              for px, py in (s["start"], s["end"])]
    # True-to-scale trace widths: KiCad mm → axes data scale.
    f_widths = [s.get("width", 0.1) * pt_per_mm
                for s in cob.get("segments", []) if s["layer"] == "F.Cu"]
    ax.add_collection(LineCollection(
        [(f_segs[i], f_segs[i + 1]) for i in range(0, len(f_segs), 2)],
        colors=PCB_STYLE["trace"], linewidths=f_widths, capstyle="round",
        zorder=1.2))
    for v in [*cob.get("vias", []), *stitch_vias]:
        (vx, vy) = to_plot(cob, v["x_mm"], v["y_mm"])
        ax.add_patch(Circle((vx, vy), v["size_mm"] / 2,
                            facecolor=PCB_STYLE["via"], edgecolor="none", zorder=1.4))
        if v.get("drill_mm"):
            ax.add_patch(Circle((vx, vy), v["drill_mm"] / 2,
                                facecolor=PCB_STYLE["drill"], edgecolor="none",
                                zorder=1.45))

    # Solder-mask opening: exposed board with the copper beneath re-drawn
    # bright, clipped to the opening. The opening shapes are padring-
    # footprint-local, unlike the global board data around them. Boards
    # open the cavity in several shapes (1x0.5: inner die rect + beveled
    # cavity poly) — clip to the union via a compound path.
    open_polys = []
    for shape in cob["graphics"].get("F.Mask", []):
        if shape["type"] == "rect" and "start" in shape:
            (x0, y0) = local_to_plot(cob, *shape["start"])
            (x1, y1) = local_to_plot(cob, *shape["end"])
            open_polys.append([(min(x0, x1), min(y0, y1)),
                               (max(x0, x1), min(y0, y1)),
                               (max(x0, x1), max(y0, y1)),
                               (min(x0, x1), max(y0, y1))])
        elif shape["type"] == "poly" and shape.get("pts"):
            open_polys.append([local_to_plot(cob, px, py)
                               for px, py in shape["pts"]])
    if open_polys:
        for poly in open_polys:
            ax.add_patch(Polygon(poly, closed=True,
                                 facecolor=PCB_STYLE["bare"],
                                 edgecolor=PCB_STYLE["cu"], lw=0.5 * fs,
                                 zorder=1.8))
        verts, codes = [], []
        for poly in open_polys:
            verts.extend(poly)
            codes.extend([MplPath.MOVETO] + [MplPath.LINETO] * (len(poly) - 1))
        open_clip = PathPatch(MplPath(verts, codes), facecolor="none",
                              edgecolor="none", zorder=-1)
        ax.add_patch(open_clip)
    else:
        open_clip = None
    if open_clip is not None:
        for pts in pour_pts:
            p = Polygon([to_plot(cob, px, py) for px, py in pts], closed=True,
                        facecolor=PCB_STYLE["cu"], edgecolor="none", zorder=2)
            ax.add_patch(p)
            p.set_clip_path(open_clip)
        bright = LineCollection(
            [(f_segs[i], f_segs[i + 1]) for i in range(0, len(f_segs), 2)],
            colors=PCB_STYLE["cu"], linewidths=f_widths, capstyle="round",
            zorder=1.9)  # under the die render (2) — these route beneath it
        ax.add_collection(bright)
        bright.set_clip_path(open_clip)
        # Vias inside the opening are exposed too (the mask is open
        # there) — the bare-laminate fill above would otherwise hide
        # them. Gold-plated barrel, dark drill, clipped to the opening
        # and still under the die render.
        for v in [*cob.get("vias", []), *stitch_vias]:
            vx, vy = to_plot(cob, v["x_mm"], v["y_mm"])
            barrel = Circle((vx, vy), v["size_mm"] / 2, facecolor=PCB_STYLE["cu"],
                            edgecolor="none", zorder=1.95)
            ax.add_patch(barrel)
            barrel.set_clip_path(open_clip)
            if v.get("drill_mm"):
                drill = Circle((vx, vy), v["drill_mm"] / 2, facecolor=PCB_STYLE["drill"],
                               edgecolor="none", zorder=1.96)
                ax.add_patch(drill)
                drill.set_clip_path(open_clip)

    # Non-padring footprint copper (e.g. the logo's copper artwork):
    # filled shapes in global board frame, muted under the mask — then
    # redrawn bright where the same shape opens the mask (KiCad 8 puts
    # `(layers "F.Cu" "F.Mask")` on one poly = exposed copper).
    for shape in cob["board_graphics"].get("F.Cu", []):
        bright = "F.Mask" in shape.get("layers", ())
        fill = PCB_STYLE["cu"] if bright else PCB_STYLE["trace"]
        zo = 2.5 if bright else 1.15  # bright copper sits over the mask
        if shape["type"] == "poly" or "pts" in shape:
            ax.add_patch(Polygon([to_plot(cob, px, py) for px, py in shape["pts"]],
                                 closed=True, facecolor=fill, edgecolor="none",
                                 zorder=zo))
        elif shape["type"] == "rect" and "start" in shape:
            (sx0, sy0) = to_plot(cob, *shape["start"])
            (sx1, sy1) = to_plot(cob, *shape["end"])
            ax.add_patch(Rectangle((min(sx0, sx1), min(sy0, sy1)),
                                   abs(sx1 - sx0), abs(sy1 - sy0),
                                   facecolor=fill, edgecolor="none", zorder=zo))
        elif shape["type"] == "circle" and "center" in shape:
            (cx, cy) = to_plot(cob, *shape["center"])
            (ex, ey) = to_plot(cob, *shape["end"])
            ax.add_patch(Circle((cx, cy), math.hypot(ex - cx, ey - cy),
                                facecolor=fill, edgecolor="none", zorder=zo))

    # Padring-footprint copper: the QR-alignment circle next to pad 0
    # (0.5x1 board) and the corner fiducial squares — footprint-local,
    # sitting in the open cavity, so drawn as exposed copper.
    for shape in cob["graphics"].get("F.Cu", []):
        if shape["type"] == "circle" and "center" in shape:
            (cx, cy) = local_to_plot(cob, *shape["center"])
            (ex, ey) = local_to_plot(cob, *shape["end"])
            ax.add_patch(Circle((cx, cy), math.hypot(ex - cx, ey - cy),
                                facecolor=PCB_STYLE["cu"], edgecolor="none",
                                zorder=2.5))
        elif shape["type"] == "poly" or "pts" in shape:
            ax.add_patch(Polygon([local_to_plot(cob, px, py)
                                  for px, py in shape["pts"]],
                                 closed=True, facecolor=PCB_STYLE["cu"],
                                 edgecolor="none", zorder=2.5))

    # Padring-footprint silk (filled corner dot, edge marks) — footprint-
    # local like the copper above; MOSB's board is the first with any.
    # Silk belongs under the mask-opening fill: manufacturers suppress
    # silkscreen over mask cutouts, so the opening clips it realistically.
    _draw_shapes(ax, cob["graphics"].get("F.SilkS", []),
                 dict(color=PCB_STYLE["silk"]), zorder=1.7, pt_per_mm=pt_per_mm,
                 xf=lambda p: local_to_plot(cob, *p), outline_only=("rect",))

    # Board silkscreen (pin-1 marker, marking box) — global board frame,
    # unlike the footprint-local graphics above; strokes at their KiCad
    # widths. The marking box reserves space for the fab's serial number,
    # so it reads as a frame, not painted silk. Eco layers are assembly
    # planning, not bonding info — skipped.
    _draw_shapes(ax, cob["board_graphics"].get("F.SilkS", []),
                 dict(color=PCB_STYLE["silk"]), zorder=1.7, cob=cob,
                 pt_per_mm=pt_per_mm, outline_only=("rect",))

    # Bond pads: gold ENIG ring with a class-colored rim; the mechanical
    # extras (paddle + mounting thru-holes) are drawn dashed / as holes.
    frot = cob["padring"]["rot_deg"]
    for pad in cob["pads"]:
        x, y = to_plot(cob, pad["gx_mm"], pad["gy_mm"])
        w, h = pad["size_mm"]
        cls = CLASS_COLORS[net_class(pad)]
        if pad["num"] in extras and pad["type"] == "thru_hole":
            continue  # drawn with the vias above
        # Drawn rotation: KiCad pad rot is CCW on screen (relative to the
        # footprint), so the plot-frame angle is footprint + pad rot.
        poly = centered_rect(x, y, w, h, (frot + pad["rot_deg"]) % 360)
        if pad["num"] in extras:
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
        if show_numbers and pad["num"] not in extras:
            # Diagram numbering matches the die: the wafer-space boards
            # label physical pad N with die pad N-1; boards with a pinned
            # ring_map label each pad with the die pad it actually bonds.
            ax.annotate(bond_labels[pad["num"]] if bond_labels else
                        str(int(pad["num"]) - 1), (x, y), fontsize=4.2 * fs,
                        ha="center", va="center", color=PCB_STYLE["pad_num"],
                        zorder=7)


def draw_die(ax: plt.Axes, design: dict, fs: float, show_numbers: bool = True,
             img: np.ndarray | None = None) -> None:
    """Die render in the cavity + pad rectangles colored by die net class."""
    die_bb = design["die_bb_um"]
    w_mm, h_mm = design["die_w_um"] / 1000.0, design["die_h_um"] / 1000.0

    if img is None:
        img = mpimg.imread(REPO / design["bg_png"])
    # origin="upper": PNG top row = display-frame top = plot-frame top.
    # interpolation="nearest": the two-tone dithered render must survive
    # the PDF resample (displayed inches × PDF_RASTER_DPI) pixel-for-pixel —
    # bilinear creates gray values that explode the flate stream (GD03's
    # die alone was 10.9 MB compressed).
    ax.imshow(img, extent=(-w_mm / 2, w_mm / 2, -h_mm / 2, h_mm / 2),
              origin="upper", zorder=2, interpolation="nearest")
    ax.add_patch(Rectangle((-w_mm / 2, -h_mm / 2), w_mm, h_mm, fill=False,
                           edgecolor="black", lw=0.8 * fs, zorder=2.5))

    if show_numbers:
        # Labels are sized to the pad pitch, not the page: fs is ~constant
        # across boards, but the dense TQVA padframe (14 pads per 0.5 mm
        # edge, ~33 um pitch) turns a 3 pt font into an overlapping blob.
        # Two digits are ~1.27 em wide, so 0.7 x pitch keeps a margin;
        # the 1x1 board's pitch still allows the full 3.2 * fs.
        pitch = min_pad_pitch_mm(design)
        nfs = min(3.2 * fs, 0.7 * pitch * data_pt_per_mm(ax))
        gap = min(0.08, 1.2 * pitch)
        for pad in design["pads"]:
            cx, cy, w, h = die_pad_mm(pad, die_bb)
            # Number just inside the pad, straight in from the pad's edge
            # (perpendicular to the die edge — pushing toward the die
            # center would drag corner numbers diagonally off their pads),
            # aligned away from the pad so the gap stays clear.
            # _classify_edge returns single letters T/L/B/R.
            e = pad.get("edge")
            if e == "B":
                lx, ly, ha, va = cx, cy + h / 2 + gap, "center", "bottom"
            elif e == "L":
                lx, ly, ha, va = cx + w / 2 + gap, cy, "left", "center"
            elif e == "R":
                lx, ly, ha, va = cx - w / 2 - gap, cy, "right", "center"
            else:  # "T"
                lx, ly, ha, va = cx, cy - h / 2 - gap, "center", "top"
            ax.annotate(str(pad["n"]), (lx, ly), fontsize=nfs,
                        ha=ha, va=va, color="#111111", zorder=7)

    for pad in design["pads"]:
        cx, cy, w, h = die_pad_mm(pad, die_bb)
        fill = PAD_COLORS[classify_net(pad["net"])][0]
        ax.add_patch(Rectangle((cx - w / 2, cy - h / 2), w, h,
                               facecolor=fill, edgecolor="black", lw=0.25 * fs,
                               zorder=4))


def die_qr_mm(design: dict) -> tuple[float, float, float]:
    """Die QR cell centre + half-size in plot-frame mm.

    _wsip_corners returns the QR bbox in the die's display frame (QR
    top-right); the same centring as die_pad_mm maps it to plot mm.
    """
    bb = design["die_bb_um"]
    qx0, qy0, qx1, qy1 = _wsip_corners(tuple(bb))[0]
    return ((qx0 + qx1) / 2 - (bb[0] + bb[2]) / 2) / 1000.0, \
           ((qy0 + qy1) / 2 - (bb[1] + bb[3]) / 2) / 1000.0, \
           WSIP_CELL_UM / 2000.0


def board_rocket_mm(cob: dict) -> tuple[float, float, float] | None:
    """Board rocket artwork (wafer.space logo) centre + ring radius, plot mm.

    The logo is board-level artwork: every poly in board F.SilkS/F.Cu
    (the F.SilkS rect is the fab's serial-number marking box, excluded
    the same way _draw_shapes' outline_only excludes it).
    """
    xs, ys = [], []
    for lay in ("F.SilkS", "F.Cu"):
        for s in cob["board_graphics"].get(lay, []):
            if s.get("type") != "poly" or "pts" not in s:
                continue
            for px, py in s["pts"]:
                X, Y = to_plot(cob, px, py)
                xs.append(X)
                ys.append(Y)
    if not xs:
        return None
    cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    r = math.hypot(max(xs) - min(xs), max(ys) - min(ys)) / 2 + 0.08
    return cx, cy, r


def qr_fiducial_target(cob: dict) -> dict | None:
    """What the die QR aligns to on this board, plot-frame mm + callout text.

    boards.json declares the convention (qr_alignment on the parsed JSON:
    "copper-circle" = the padring footprint's F.Cu circle next to pad 0,
    "rocket" = the board's rocket logo); the geometry is derived here as
    before and must match the declaration — a mismatch means the kicad
    file or boards.json drifted, and the factory-facing callout must not
    silently change.
    """
    declared = cob.get("qr_alignment")
    target = None
    for s in cob["graphics"].get("F.Cu", []):
        if s.get("type") == "circle" and "center" in s:
            cx, cy = local_to_plot(cob, *s["center"])
            ex, ey = local_to_plot(cob, *s["end"])
            target = {"kind": "copper-circle", "cx": cx, "cy": cy,
                      "r": math.hypot(ex - cx, ey - cy) + 0.25,
                      "note": FIDUCIAL_NOTE_CIRCLE, "note_zh": FIDUCIAL_NOTE_CIRCLE_ZH,
                      "label": CIRCLE_LABEL, "label_zh": CIRCLE_LABEL_ZH,
                      "anchor": "left"}
            break
    if target is None:
        rocket = board_rocket_mm(cob)
        if rocket is not None:
            rx, ry, rr = rocket
            target = {"kind": "rocket", "cx": rx, "cy": ry, "r": rr,
                      "note": FIDUCIAL_NOTE_ROCKET, "note_zh": FIDUCIAL_NOTE_ROCKET_ZH,
                      "label": ROCKET_LABEL, "label_zh": ROCKET_LABEL_ZH}
    if target is None:
        return None
    if declared and declared != target["kind"]:
        raise SystemExit(
            f"board {cob.get('board', cob['source'])}: boards.json declares "
            f"qr_alignment={declared!r} but the geometry shows "
            f"{target['kind']!r} — the kicad file or the knowledgebase "
            f"drifted")
    return target


def draw_fiducials(ax: plt.Axes, cob: dict, fs: float, y_top: float,
                   target: dict | None = None) -> None:
    """Placement-page fiducials: ring the board's QR-alignment marker and
    label it above the board (the die's QR cell gets the circle + zoom
    inset of draw_qr_zoom, so the header note reads as QR circle ↔
    marker ring).
    The circle marker sits beside the die QR (and the QR inset), so its
    label anchors right of the leader to stay clear of the inset box.
    """
    if target is None:
        print("WARN: no board rocket artwork found — fiducial ring skipped")
        return
    cx, cy, rr = target["cx"], target["cy"], target["r"]
    lab_y = y_top + 1.45
    ax.add_patch(Circle((cx, cy), rr, fill=False, edgecolor=FIDUCIAL_COLOR,
                        lw=1.5 * fs, zorder=8))
    ax.plot([cx, cx], [cy + rr + 0.06, lab_y - 0.05], color=FIDUCIAL_COLOR,
            lw=0.7 * fs, zorder=8)
    ax.annotate(f"{target['label']} / {target['label_zh']}",
                (cx + 0.12 if target.get("anchor") == "left" else cx, lab_y),
                ha=target.get("anchor", "center"), va="bottom",
                fontsize=6.5 * fs, fontweight="bold", color=FIDUCIAL_COLOR,
                zorder=8, fontfamily=["DejaVu Sans", *CJK_FAMILIES])


def draw_qr_zoom(fig: plt.Figure, ax: plt.Axes, design: dict, fs: float,
                 img: np.ndarray, y_top: float) -> None:
    """Placement-page QR callout: maroon circle on the die's QR cell, leader
    up to a magnified inset of that cell in the band above the board —
    the fiducial the assembler aligns the board rocket to. The inset is
    positioned in figure space (centred on the QR's figure-fraction x),
    so the QR's data position is mapped through the settled
    equal-aspect transform.
    """
    qx, qy, qh = die_qr_mm(design)
    # Circle highlight on the die end of the leader line (the inset box
    # is the other end) — the same simple ring as the board fiducial,
    # no fill, so it stays crisp in print.
    qr_r = 1.65 * (qh + 0.05)
    ax.add_patch(Circle((qx, qy), qr_r, fill=False,
                        edgecolor=FIDUCIAL_COLOR, lw=1.5 * fs, zorder=8))

    fig.canvas.draw()  # settle the equal-aspect box before fig-space mapping

    def to_fig(x: float, y: float) -> tuple[float, float]:
        px, py = ax.transData.transform((x, y))
        return (px / (fig.get_figwidth() * fig.dpi),
                py / (fig.get_figheight() * fig.dpi))

    cx_f, box_top_f = to_fig(qx, qy + qr_r + 0.05)
    _, board_top_f = to_fig(0.0, y_top)
    inset_h = QR_ZOOM_W_FRAC * A4_W_IN / A4_H_IN  # square on paper
    y0f = board_top_f + 0.006
    axz = fig.add_axes((cx_f - QR_ZOOM_W_FRAC / 2, y0f,
                        QR_ZOOM_W_FRAC, inset_h))
    w_mm = design["die_w_um"] / 1000.0
    h_mm = design["die_h_um"] / 1000.0
    half = QR_ZOOM_VIEW_MM / 2
    axz.imshow(img, extent=(-w_mm / 2, w_mm / 2, -h_mm / 2, h_mm / 2),
               origin="upper", interpolation="nearest")
    axz.set_xlim(qx - half, qx + half)
    axz.set_ylim(qy - half, qy + half)
    axz.set_xticks(())
    axz.set_yticks(())
    axz.set_facecolor("white")
    for sp in axz.spines.values():
        sp.set_visible(True)
        sp.set_edgecolor(FIDUCIAL_COLOR)
        sp.set_linewidth(1.6 * fs)

    fig.add_artist(Line2D([cx_f, cx_f], [box_top_f, y0f],
                          transform=fig.transFigure, color=FIDUCIAL_COLOR,
                          lw=0.9 * fs, zorder=8))
    fig.text(cx_f, y0f + inset_h + 0.002, f"{QR_LABEL} / {QR_LABEL_ZH}",
             ha="center", va="bottom", fontsize=5.5 * fs,
             fontweight="bold", color="white",
             fontfamily=["DejaVu Sans", *CJK_FAMILIES],
             bbox=dict(facecolor=FIDUCIAL_COLOR, edgecolor="none", pad=1.6))


def wire_segments(design: dict, cob: dict):
    """Wire fan: die pad n → COB pad n (physical pad n+1), die-pad-edge start.

    Boards with a pinned ring_map (stamped by parse_pcb) bond a different
    die pad per COB pad — the map wins over the +1 convention.

    Returns (segments, lengths) in plot-frame mm. Wires are drawn in a
    single color — class-colored wires blended into the die/PCB palette.
    """
    die_bb = design["die_bb_um"]
    frot = cob["padring"]["rot_deg"]
    die_pads = {p["n"]: p for p in design["pads"]}
    ring = sorted((p for p in cob["pads"] if p["num"] not in board_extras(cob)),
                  key=lambda p: int(p["num"]))
    ring_map = cob.get("ring_map")

    segments, lengths = [], []
    for i, pcb in enumerate(ring):
        dp = die_pads.get(ring_map[i] if ring_map else int(pcb["num"]) - 1)
        if dp is None:
            continue
        cx, cy, w, h = die_pad_mm(dp, die_bb)
        px, py = to_plot(cob, pcb["gx_mm"], pcb["gy_mm"])

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
        # Pads are drawn at (frot + pad rot) CCW in the plot frame; the
        # landing math wants rr = −(drawn rot): stage 1 rotates the
        # toward-die direction into the pad's local frame with R(rr),
        # stage 2 rotates the landing point back with R(−rr). Invariant
        # under rr → rr + 180 (pad-frame half-turn cancels), so the sign
        # convention of the stored KiCad rot only shifts phase by 180°.
        rr = math.radians(-(frot + pcb["rot_deg"]))
        dist_c = math.hypot(px, py)
        if dist_c:
            nx, ny = -px / dist_c, -py / dist_c
            # Express the toward-die direction in the pad's local frame.
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
    """Page 2 (placement) or page 3 (bonding) on the shared A4 template.

    fs scales fonts and line widths so the 8.5×10 in reference layout
    shrinks proportionally onto A4.
    """
    die_classes = sorted({classify_net(p["net"]) for p in design["pads"]},
                         key=list(DIE_CLASS_LABELS).index)
    pcb_classes = sorted({net_class(p) for p in cob["pads"]
                          if p["num"] not in board_extras(cob)},
                         key=list(CLASS_COLORS).index)

    pw, ph = A4_W_IN, A4_H_IN
    fs = min(pw / 8.5, ph / 10.0)

    fig = plt.figure(figsize=(pw, ph))
    ax = fig.add_axes(AXES_RECT)
    ax.set_aspect("equal")

    # Final limits first: data-scale linewidths (traces, bond wires)
    # need the applied axes transform, and the draw inside draw_board
    # must see the limits the page is rendered with.
    x0, y0, x1, y1 = board_bounds(cob)
    ax.set_xlim(x0 - 1.2, x1 + 1.2)
    ax.set_ylim(y0 - 1.2, y1 + 2.2)

    img = mpimg.imread(REPO / design["bg_png"])
    # Bond labels: ring_map boards get the die pad each COB pad bonds to;
    # the convention boards label physical pad N with die pad N-1.
    ring_map = cob.get("ring_map")
    bond_labels = None
    if ring_map:
        ring = sorted((p for p in cob["pads"] if p["num"] not in board_extras(cob)),
                      key=lambda p: int(p["num"]))
        bond_labels = {p["num"]: str(ring_map[i]) for i, p in enumerate(ring)}
    draw_board(ax, cob, fs, extras=board_extras(cob), bond_labels=bond_labels)
    draw_die(ax, design, fs, img=img)
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

    ax.set_axis_off()

    name, slot = design["name"], design["slot_size"]
    code = name.split("_")[0]
    # Source PCB, for traceability — page 1 comes from the GDS, pages 2-3
    # from this board file.
    pcb_file = Path(cob["source"]).name
    if bonding:
        add_header(fig, f"{code} · bonding diagram",
                   f"{name} · slot {slot} · {pcb_file} · "
                   f"{len(segments)} wires, "
                   f"length {min(lengths):.2f}–{max(lengths):.2f} mm")
        page_num = 3
    else:
        add_header(fig, f"{code} · die placement on PCB",
                   f"{name} · slot {slot} · {pcb_file} · "
                   f"die {design['die_w_um']:.0f}×{design['die_h_um']:.0f} µm")
        # Orientation note in the band between the header rule and the
        # diagram, echoing the fiducial rings drawn below.
        target = qr_fiducial_target(cob)
        note, note_zh = (target["note"], target["note_zh"]) if target \
            else (FIDUCIAL_NOTE_ROCKET, FIDUCIAL_NOTE_ROCKET_ZH)
        fig.text(0.5, (HEADER_RULE_Y + (AXES_RECT[1] + AXES_RECT[3])) / 2,
                 f"{note} / {note_zh}", ha="center",
                 va="center", fontsize=8.5, fontweight="bold",
                 color=FIDUCIAL_COLOR, fontfamily=["DejaVu Sans", *CJK_FAMILIES])
        draw_fiducials(ax, cob, fs, y1, target)
        draw_qr_zoom(fig, ax, design, fs, img, y1)
        page_num = 2
    add_footer(fig, page_num)
    two_legends(ax, die_classes, pcb_classes, fs)
    return fig


def out_dirs(design: dict) -> tuple[Path, Path]:
    """Per-reticle outputs: bonding-diagrams/<reticle>/ PDFs, tmp/<reticle>/pages/ previews.

    Designs extracted before the reticle was stamped default to ws-run1.
    """
    reticle = design.get("reticle", "ws-run1")
    return OUT_DIR / reticle, TMP_ROOT / reticle / "pages"


def render_design(design: dict, cob: dict) -> Path:
    """Write bonding-diagrams/<reticle>/<name>_<slot>.pdf (3 pages) + PNG previews."""
    out_dir, page_dir = out_dirs(design)
    out_dir.mkdir(parents=True, exist_ok=True)
    page_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{design['name']}_{design['slot_size']}"

    pages_pdf = page_dir / f"{stem}_pages23.pdf"
    with PdfPages(pages_pdf) as pdf:
        for bonding in (False, True):
            fig = build_page(design, cob, bonding)
            pdf.savefig(fig, dpi=PDF_RASTER_DPI)
            tag = "p3" if bonding else "p2"
            fig.savefig(page_dir / f"{stem}_{tag}.png", dpi=600)
            plt.close(fig)

    page1 = build_pinout_page(design)

    out = out_dir / f"{stem}.pdf"
    writer = PdfWriter()
    writer.add_page(page1)
    writer.append(str(pages_pdf))
    with open(out, "wb") as f:
        writer.write(f)

    if shutil.which("pdftoppm"):  # p1 preview; p2/p3 save PNGs directly above
        subprocess.run(
            ["pdftoppm", "-png", "-r", "600", "-f", "1", "-l", "1", "-singlefile",
             str(out), str(page_dir / f"{stem}_p1")], check=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--designs", nargs="*", default=DEFAULT_DESIGNS,
                    help="design cell names (default: WSLG)")
    ap.add_argument("--board", help="board id in boards.json — shorthand "
                                    "for --cob tmp/cob/<board>.json")
    ap.add_argument("--pads", type=Path, default=None,
                    help="pads.json (default: the single tmp/<reticle>/"
                         "pads.json; required when several exist)")
    ap.add_argument("--cob", type=Path, default=DEFAULT_COB)
    args = ap.parse_args()

    if args.board:
        args.cob = REPO / "tmp" / "cob" / f"{args.board}.json"
    cob = json.loads(args.cob.read_text())
    pads_path = find_pads(args.pads)
    designs = json.loads(pads_path.read_text())
    if args.designs:
        designs = [d for d in designs if d["name"] in args.designs]
    if not designs:
        raise SystemExit(f"no matching designs in {pads_path} "
                         "— run extract_dies.py first")

    ring = [p for p in cob["pads"] if p["num"] not in board_extras(cob)]
    # Die must physically fit the board's die site: parse stamps the
    # asserted boards.json site; fall back to the smallest F.Mask opening
    # rect (the cavity the die drops into) for un-stamped COB JSONs. Pad
    # count alone can't catch the wrong board — the 1x0.5 and 0.5x1 rings
    # both have 72 pads; only the cavity shape differs (wide vs tall).
    site = cob.get("die_site_mm")
    if site:
        cavity = (site[0], site[1])
    else:
        cavity = None
        for s in cob["graphics"].get("F.Mask", []):
            if s["type"] == "rect" and "start" in s:
                w = abs(s["end"][0] - s["start"][0])
                h = abs(s["end"][1] - s["start"][1])
                if cavity is None or w * h < cavity[0] * cavity[1]:
                    cavity = (w, h)
    # Knowledgebase routing: a die claimed by name in another board's
    # "designs" renders only there; otherwise only the board serving its
    # slot may take it. Geometry alone can't disambiguate overlapping
    # boards (MOSB's 74-pad die fits the 1x1 site too).
    boards_kb = load_boards()
    this_board = cob.get("board")
    for design in designs:
        claims = [bid for bid, e in boards_kb.items()
                  if design["name"] in e.get("designs", ())]
        if claims and this_board not in claims:
            print(f"SKIP {design['name']}: claimed by board {claims[0]}")
            continue
        if not claims:
            slot_claims = [bid for bid, e in boards_kb.items()
                           if e.get("slot") == design["slot_size"]]
            if slot_claims and this_board not in slot_claims:
                print(f"SKIP {design['name']}: slot {design['slot_size']} "
                      f"served by board {slot_claims[0]}")
                continue
        if len(design["pads"]) != len(ring):
            print(f"SKIP {design['name']}: pad count mismatch "
                  f"(die {len(design['pads'])} vs COB {len(ring)})")
            continue
        try:
            pinout_pdf_for(design)
        except SystemExit as e:
            print(f"SKIP {design['name']}: {e}")
            continue
        dw, dh = design["die_w_um"] / 1000.0, design["die_h_um"] / 1000.0
        if cavity and (dw > cavity[0] + 0.2 or dh > cavity[1] + 0.2):
            print(f"SKIP {design['name']}: slot {design['slot_size']} die "
                  f"{dw:.2f}×{dh:.2f} mm does not fit die site "
                  f"{cavity[0]:.2f}×{cavity[1]:.2f} mm — wrong board")
            continue
        out = render_design(design, cob)
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
