"""Vendored from wafer-space-die-pad-diagrams/make_diagrams.py (@ sibling
repo HEAD) — the sibling can no longer take changes, so this copy is
authoritative for the bonding-diagrams pipeline. The only delta from the
sibling file is the max_px parameter on render_gds_background (see
extract_dies.DIE_RENDER_MAX_PX). Module-level data paths resolve
relative to this file's grandparent, i.e. they still expect the ws-run1
checkout as a sibling of this repo. The generated per-design pinout
PDFs/PNGs in the sibling repo's diagrams/ remain a data input (see
make_bonding_diagrams.SIBLING_DIAGRAMS), not code.

Original module docstring follows, verbatim:
---

Generate annotated pad diagrams for each design in the ws-run1 reticle.

For every top-level instance in reticle.oas, emit a PNG and SVG showing:
  - The die outline (the design cell's bounding box)
  - Each IO pad drawn as a filled rectangle (Pad layer 37/0, filtered by size)
  - The net name for each pad, pulled from Metal5_Label (81/10) or
    MetalTop_Label (53/10) texts whose position falls inside the pad

The layout is read once; all designs are rendered from that single load.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import klayout.db as kdb
import klayout.lay as klay
import matplotlib.image as mpimg
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import segno

REPO = Path(__file__).resolve().parent.parent / "ws-run1"
OAS = REPO / "layout" / "reticle.oas"
LYP = REPO / "lyp" / "gf180mcu.lyp"
OUT_DIR = Path(__file__).resolve().parent / "diagrams"
# Dimensions for the GDS render used as a background. The main plot
# targets ~14" × 180 dpi = ~2520 px, so 3200 px on the long edge lets
# it downsample slightly (crisper than a 1:1 or upsampled source).
# Corner zoom insets crop a fixed physical region from this image; see
# ZOOM_CROP_UM.
GDS_RENDER_MAX_PX = 3200

# Physical size of the corner zoom window, in um. The crop is always
# this many microns on a side regardless of chip dimensions, so
# smaller dies (e.g. the 0.5x0.5 TQVA at 1936x2531) show a larger
# *fraction* of themselves in each inset than a full 1x1 chip — same
# absolute detail, just scaled differently. A 500 um window easily
# covers the 143-um ws-template cells plus the alignment marks and
# some surrounding pad-ring structure.
ZOOM_CROP_UM = 500.0

# GF180MCU layer numbers
PAD_LAYER = (37, 0)
LABEL_LAYERS = [(81, 10), (53, 10)]  # Metal5_Label, MetalTop_Label

# Visible layers for the GDS background render. The raw gf180mcu.lyp marks
# ~15 layers visible with clashing colors (pink wells, teal dummy metal,
# green poly, yellow-green ESD, salmon n-well, ...), which produces a busy
# multi-coloured render. For a publication-style background we want a
# two-tone look matching the ws-run1 README renders: yellow/olive body
# from top-level metal, dark red pads from the Pad layer. Everything else
# is hidden so the pad ring stands out cleanly.
BG_VISIBLE_LAYERS: set[tuple[int, int]] = {
    (37, 0),   # Pad           → #72342b (dark red)
    (81, 0),   # Metal5        → #cdc16b (olive/yellow)
    (53, 0),   # MetalTop      → #b1dd9c (pale green/yellow)
}

# Minimum pad edge length in microns to count as an IO pad (not a seal ring).
PAD_MIN_UM = 30.0

# Positions of the standard ws-run1 template corner markers, pulled from
# BTAP's direct-child instance probe (see probe_wsip.py):
#   gf180mcu_ws_ip__id   — 143x143 um QR-like identifier, bottom-left,
#                          sitting ~40/50 um in from the die corner.
#   gf180mcu_ws_ip__logo — 143x143 um wafer.space logo, top-right,
#                          sitting ~15 um in from the die corner.
# The template applies the same offsets to every slot, so we can compute
# highlight-frame positions from die_bb without probing the GDS.
WSIP_CELL_UM = 143.0
WSIP_QR_INSET_UM = (40.0, 50.0)   # from (die.x0, die.y0)
WSIP_LOGO_INSET_UM = (15.0, 11.0)  # from (die.x1, die.y1)
WSIP_QR_COLOR = "#00838f"   # deep cyan — contrasts yellow metal + red pads
WSIP_LOGO_COLOR = "#ad1457"  # magenta — same

# QR payload + segno parameters that reproduce the per-chip QRs that
# the wafer.space precheck stamps onto each design. probe_qr_per_chip.py
# decoded every chip's bottom-left QR cell in reticle.oas and found
# they all encode "G801<PROJ>" — the shuttle ID followed by the 4-char
# project code from the README (e.g. WSLG → "G801WSLG"). All chips use
# error level H and alphanumeric mode; the mask varies per project.
# probe_segno_match_v2.py walked every (mask, mode, ec) combination
# and recorded which mask reproduces each chip's matrix byte-for-byte.
# Projects not in the dict default to mask=None (segno auto-picks the
# QR-spec-optimal mask), which matches the precheck output for the
# remaining ~12 chips.
WSIP_QR_DATA_PREFIX = "G801"
WSIP_QR_ERROR = "H"
WSIP_QR_MODE = "alphanumeric"
WSIP_QR_PROJECT_MASKS: dict[str, int] = {
    "2975": 0, "JKU1": 0, "JKU2": 0, "TTP2": 0, "WSLG": 0,
    "BRWN": 3, "BTAP": 3, "CHES": 3,
    "TRID": 4, "TZ01": 4,
    "OCD2": 5, "RZ80": 5, "TQVA": 5,
    "OCD1": 6, "RBOY": 6,
    "GD04": 7, "ISHI": 7,
}

# Physical dimensions of one full reticle slot (1x1). Measured from WSLG,
# the reference full-slot design. Half-slot chips come out at ~1936x5122
# (0.5x1) or ~3932x2531 (1x0.5), so the "full" W and H serve as the
# denominators when converting a die's actual um dimensions back into
# slot units for the info panel.
SLOT_W_UM = 3932.0
SLOT_H_UM = 5122.0

# Slot size per project code, lifted from ws-run1/README.md. Used as a
# cross-check against the size computed from the actual GDS die
# dimensions — mismatches are flagged during generation so the dict
# doesn't drift out of sync with the layout.
PROJECT_SIZES_README: dict[str, str] = {
    "2975": "1x1", "AS03": "1x1", "BRWN": "1x1", "BTAP": "1x1",
    "CAFE": "1x1", "CHES": "1x1",
    "GD02": "0.5x1", "GD03": "1x1", "GD04": "1x0.5",
    "HZ80": "0.5x1", "ISHI": "1x1", "JKU1": "1x1", "JKU2": "1x0.5",
    "KIAN": "1x1", "MOLE": "1x1", "MOS2": "1x1", "MOSB": "1x1",
    "OCD1": "1x1", "OCD2": "1x0.5", "RBOY": "1x1", "RZ80": "1x1",
    "TQVA": "0.5x0.5", "TQVB": "0.5x1", "TQVC": "1x0.5",
    "TRID": "0.5x1", "TTP2": "1x1", "TTPG": "1x1",
    "TZ01": "1x1", "WSLG": "1x1",
}


def _fmt_slot_unit(v: float) -> str:
    """Render a slot unit as '1' or '0.5', no trailing zeros."""
    return str(int(v)) if v == int(v) else f"{v:g}"


def computed_slot_size(die_w: float, die_h: float) -> str:
    """Slot size inferred from the actual die dimensions, rounded to 0.5.

    Used both in the info panel and as the source of truth for the
    cross-check against PROJECT_SIZES_README. 0.5-slot designs measure
    ~1936 um (half of SLOT_W_UM) or ~2531 um (half of SLOT_H_UM), so
    rounding to the nearest half-unit snaps correctly to 0.5 / 1 / 2.
    """
    w_units = round(die_w / SLOT_W_UM * 2) / 2
    h_units = round(die_h / SLOT_H_UM * 2) / 2
    return f"{_fmt_slot_unit(w_units)}x{_fmt_slot_unit(h_units)}"

# Colors per pad class (fill, text). Follows standard electronics convention:
# grounds are black/grey, supplies are reds/oranges, signals are amber.
# Within each supply family, digital and analog use different shades so the
# rails read apart at a glance.
PAD_COLORS: dict[str, tuple[str, str]] = {
    "gnd_digital": ("#212121", "#000000"),   # DVSS   — near-black
    "gnd_analog":  ("#616161", "#37474f"),   # AVSS   — mid-grey
    "gnd":         ("#424242", "#212121"),   # VSS/GND/VSUB — charcoal
    "pwr_digital": ("#d32f2f", "#b71c1c"),   # DVDD   — crimson
    "pwr_analog":  ("#f57c00", "#e65100"),   # AVDD   — orange
    "pwr":         ("#e53935", "#b71c1c"),   # VDD/VCC — red
    "signal":      ("#f5c16c", "#111111"),   # everything else
}


def _contrast_text_color(fill_hex: str) -> str:
    """Return '#fff' or '#111' for max readability on top of fill_hex.

    Uses the perceived-luminance formula (0.299·R + 0.587·G + 0.114·B)
    rather than a flat brightness average, because human eyes weight
    green much more heavily than blue. With fill threshold 0.55, the
    crimson DVDD (#d32f2f → 0.31) and orange AVDD (#f57c00 → 0.55)
    rails get white text while the amber signal pads (#f5c16c → 0.74)
    get black — both directions land on >7:1 contrast.
    """
    h = fill_hex.lstrip("#")
    r = int(h[0:2], 16) / 255
    g = int(h[2:4], 16) / 255
    b = int(h[4:6], 16) / 255
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    return "#ffffff" if luma < 0.55 else "#111111"


def classify_net(name: str | None) -> str:
    """Return a key into PAD_COLORS for the given net name."""
    if not name:
        return "signal"
    n = name.upper()
    # Ground patterns first. "VSS" also appears inside "DVSS" / "AVSS",
    # so the digital / analog variants must be tested before plain VSS.
    if "DVSS" in n or "DGND" in n:
        return "gnd_digital"
    if "AVSS" in n or "AGND" in n:
        return "gnd_analog"
    if "VSS" in n or "GND" in n or n == "GROUND" or "VSUB" in n:
        return "gnd"
    # Power patterns — same ordering trick.
    if "DVDD" in n or "VDDD" in n:
        return "pwr_digital"
    if "AVDD" in n or "VDDA" in n:
        return "pwr_analog"
    if "VDD" in n or "VCC" in n:
        return "pwr"
    return "signal"


@dataclass
class Pad:
    """A single IO pad with its rectangle and assigned net name."""
    x0: float   # um
    y0: float
    x1: float
    y1: float
    net: str | None = None
    # Sequential pad number assigned by _number_pads_ccw. Pad 0 is the
    # first pad encountered going counter-clockwise from the QR cell
    # (which lives in the top-right corner after the 180° rotation), so
    # the numbering walks TOP edge right→left, then LEFT, BOTTOM, RIGHT.
    num: int | None = None

    @property
    def cx(self) -> float:
        return 0.5 * (self.x0 + self.x1)

    @property
    def cy(self) -> float:
        return 0.5 * (self.y0 + self.y1)

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0


def extract_pads(cell: kdb.Cell, layout: kdb.Layout) -> list[Pad]:
    """Return IO-sized pad rectangles in this cell (hierarchical)."""
    layer_index = layout.layer(*PAD_LAYER)
    pads: list[Pad] = []
    it = cell.begin_shapes_rec(layer_index)
    while not it.at_end():
        shape = it.shape()
        tr = it.trans()
        bb = shape.bbox().transformed(tr)
        w_um = bb.width() * layout.dbu
        h_um = bb.height() * layout.dbu
        if w_um >= PAD_MIN_UM and h_um >= PAD_MIN_UM:
            pads.append(Pad(
                x0=bb.left * layout.dbu,
                y0=bb.bottom * layout.dbu,
                x1=bb.right * layout.dbu,
                y1=bb.top * layout.dbu,
            ))
        it.next()
    return pads


def extract_labels(cell: kdb.Cell, layout: kdb.Layout) -> list[tuple[str, float, float]]:
    """Return (net_name, x_um, y_um) for every text on label layers."""
    out: list[tuple[str, float, float]] = []
    for ln, dt in LABEL_LAYERS:
        li = layout.layer(ln, dt)
        it = cell.begin_shapes_rec(li)
        while not it.at_end():
            shape = it.shape()
            tr = it.trans()
            if shape.is_text():
                t = tr * shape.text
                out.append((t.string, t.x * layout.dbu, t.y * layout.dbu))
            it.next()
    return out


def assign_net_names(pads: list[Pad], labels: list[tuple[str, float, float]]) -> None:
    """For each pad, pick the best net-name label whose (x,y) lies inside it.

    GF180MCU GPIO cells put a generic "PAD" (or similar) label inside every
    bondable metal region. The user's top-level net name sits on the same
    pad but is more descriptive (e.g. "bidir_PAD[29]", "clk_PAD", "VDD").
    We score labels so generic tokens lose to descriptive ones.
    """
    generic = {"PAD", "pad", "Pad", "BOND", "bond"}

    def score(name: str) -> tuple[int, int, int, int]:
        is_generic = 1 if name in generic else 0
        # Port-indexed names (contain '[') are very specific: boost them.
        is_indexed = 0 if "[" in name else 1
        # Supply nets are short UPPER names — keep those recognised too.
        is_supply = 0 if name.isupper() and len(name) <= 6 else 1
        # Prefer descriptive (longer) names among otherwise equal candidates.
        return (is_generic, is_indexed, is_supply, -len(name))

    for pad in pads:
        hits: Counter[str] = Counter()
        for name, x, y in labels:
            if pad.x0 <= x <= pad.x1 and pad.y0 <= y <= pad.y1:
                hits[name] += 1
        if not hits:
            continue
        pad.net = sorted(hits.keys(), key=score)[0]


def _classify_edge(pad: Pad, x0: float, y0: float, x1: float, y1: float) -> str:
    """Return which die edge this pad is closest to: 'L','R','T','B'."""
    d_left = pad.cx - x0
    d_right = x1 - pad.cx
    d_bottom = pad.cy - y0
    d_top = y1 - pad.cy
    m = min(d_left, d_right, d_bottom, d_top)
    if m == d_left:
        return "L"
    if m == d_right:
        return "R"
    if m == d_bottom:
        return "B"
    return "T"


def _number_pads_ccw(pads: list[Pad],
                     die_bb: tuple[float, float, float, float]) -> None:
    """Assign sequential pad numbers walking counter-clockwise from the QR.

    The QR cell sits in the top-right corner (after the 180° rotation
    applied in main()), so the natural starting point is the rightmost
    TOP-edge pad whose centre is still to the left of the QR. From there,
    counter-clockwise traversal visits:

        TOP   edge — right → left   (sorted by cx descending)
        LEFT  edge — top   → bottom (sorted by cy descending)
        BOTTOM edge — left → right  (sorted by cx ascending)
        RIGHT edge — bottom → top   (sorted by cy ascending)

    If the layout has any TOP-edge pads to the right of the QR (rare but
    possible if a pad is squeezed between the QR and the right corner),
    they're appended at the very end so the loop closes back at the QR.
    Pads are mutated in place — pad.num is set on each.
    """
    x0, y0, x1, y1 = die_bb
    # X coordinate of the QR's left edge in the rotated coordinate space.
    # Pads with cx < qr_left sit "to the left of the QR" on the top edge.
    qr_left_x = x1 - WSIP_QR_INSET_UM[0] - WSIP_CELL_UM

    edges: dict[str, list[Pad]] = {"T": [], "L": [], "B": [], "R": []}
    for p in pads:
        edges[_classify_edge(p, x0, y0, x1, y1)].append(p)

    top_left = sorted([p for p in edges["T"] if p.cx < qr_left_x],
                      key=lambda p: -p.cx)
    top_right = sorted([p for p in edges["T"] if p.cx >= qr_left_x],
                       key=lambda p: -p.cx)
    left = sorted(edges["L"], key=lambda p: -p.cy)
    bottom = sorted(edges["B"], key=lambda p: p.cx)
    right = sorted(edges["R"], key=lambda p: p.cy)

    ordered = top_left + left + bottom + right + top_right
    for n, p in enumerate(ordered):
        p.num = n


def _is_peripheral(pad: Pad, x0: float, y0: float, x1: float, y1: float) -> bool:
    """A pad is peripheral if it sits against a die edge (not deep inside).

    The threshold is a small multiple of the pad's own dimensions: real
    peripheral pads are placed with only a micrometres-wide gap to the die
    edge, so their centre lies within ~1 pad-width of that edge. Probe
    pads placed inside the die (like MOS2's internal rows) are many times
    their own size away from any edge and fall into the interior bucket.
    """
    d = min(
        pad.cx - x0,
        x1 - pad.cx,
        pad.cy - y0,
        y1 - pad.cy,
    )
    return d <= max(pad.w, pad.h) * 2.0


def render_gds_background(lv: klay.LayoutView, cell_name: str, layout: kdb.Layout,
                          die_bb: tuple[float, float, float, float],
                          out_path: Path,
                          max_px: int | None = None) -> None:
    """Render the design cell via KLayout's LayoutView to a PNG file.

    Styling comes from the .lyp loaded in setup_layout_view — layer colors,
    dither patterns, frame styles and per-layer visibility are all driven
    by ws-run1/lyp/gf180mcu.lyp. This function only picks the cell and
    the viewport.

    The image covers die_bb exactly so it can be placed under the pad
    overlay with `imshow(extent=die_bb)`.
    """
    x0, y0, x1, y1 = die_bb
    die_w = x1 - x0
    die_h = y1 - y0

    lv.active_cellview().cell_name = cell_name
    lv.zoom_box(kdb.DBox(x0, y0, x1, y1))
    lv.max_hier()

    # Pixel size proportional to die aspect ratio so the image maps 1:1 to
    # scene coordinates once we pass `extent=die_bb` to imshow. max_px
    # overrides GDS_RENDER_MAX_PX for callers that print the render
    # larger than this file's own diagrams do (e.g. magnified insets in
    # the bonding-diagrams pipeline).
    if max_px is None:
        max_px = GDS_RENDER_MAX_PX
    if die_w >= die_h:
        w_px = max_px
        h_px = max(int(max_px * die_h / die_w), 32)
    else:
        h_px = max_px
        w_px = max(int(max_px * die_w / die_h), 32)
    lv.save_image(str(out_path), w_px, h_px)


def _wsip_corners(die_bb: tuple[float, float, float, float]
                   ) -> tuple[tuple[float, float, float, float],
                              tuple[float, float, float, float]]:
    """(qr_bbox, logo_bbox) after the chip has been rotated 180°.

    In the GDS the QR cell sits inset from (die.x0, die.y0) — the
    bottom-left corner — and the logo sits inset from (die.x1, die.y1)
    — the top-right corner. We display the chip rotated 180° around
    its centre so the QR ends up in the conventional top-right corner
    that the wafer.space documentation expects, with the logo moving
    to the bottom-left. The inset distances themselves are unchanged
    by rotation; only the reference corner swaps.
    """
    x0, y0, x1, y1 = die_bb
    # QR now in the top-right, inset from (x1, y1).
    qx1 = x1 - WSIP_QR_INSET_UM[0]
    qy1 = y1 - WSIP_QR_INSET_UM[1]
    qr = (qx1 - WSIP_CELL_UM, qy1 - WSIP_CELL_UM, qx1, qy1)
    # Logo now in the bottom-left, inset from (x0, y0).
    lx0 = x0 + WSIP_LOGO_INSET_UM[0]
    ly0 = y0 + WSIP_LOGO_INSET_UM[1]
    logo = (lx0, ly0, lx0 + WSIP_CELL_UM, ly0 + WSIP_CELL_UM)
    return qr, logo


def _rotate_pad_180(pad: Pad, die_bb: tuple[float, float, float, float]
                    ) -> Pad:
    """Rotate a single pad 180° around the die centre.

    Real data rotation, not an axis flip — the pad's coordinates change
    so all downstream logic (edge classification, label placement, the
    info-panel info) still operates in a single consistent coordinate
    space. After rotation an L-edge pad becomes an R-edge pad, etc.,
    which is exactly what we want when we view the chip from the
    rotated orientation.
    """
    x0, y0, x1, y1 = die_bb
    cx2 = x0 + x1
    cy2 = y0 + y1
    return Pad(
        x0=cx2 - pad.x1,
        y0=cy2 - pad.y1,
        x1=cx2 - pad.x0,
        y1=cy2 - pad.y0,
        net=pad.net,
    )


def _rotate_image_180(path: Path) -> None:
    """Rotate the BG render PNG 180° in-place using PIL."""
    from PIL import Image
    img = Image.open(path)
    img.rotate(180).save(path)


def _project_code(cell_name: str) -> str:
    """First underscore-delimited token, used as the README project code."""
    return cell_name.split("_", 1)[0]


def _add_corner_zoom(fig: plt.Figure, ax: plt.Axes,
                     background_image: Path,
                     die_bb: tuple[float, float, float, float],
                     corner: str,
                     crop_um: float,
                     axes_bbox: tuple[float, float, float, float]) -> None:
    """Draw a zoomed inset of `corner` of the GDS background render.

    `corner` is one of 'tl', 'tr', 'bl', 'br'. The inset is placed via
    `ax.inset_axes(axes_bbox)` using axes-relative coordinates, which
    lets it sit in the empty corner region of the axes margin — the
    rectangle bounded by the die edge on two sides and the axes boundary
    on the other two. Pad labels in the L/R strips never reach above y1
    or below y0, and T/B labels never reach left of x0 or right of x1,
    so those corner regions are guaranteed free.
    """
    x0, y0, x1, y1 = die_bb
    if corner == "tl":
        zb = (x0, y1 - crop_um, x0 + crop_um, y1)
    elif corner == "tr":
        zb = (x1 - crop_um, y1 - crop_um, x1, y1)
    elif corner == "bl":
        zb = (x0, y0, x0 + crop_um, y0 + crop_um)
    else:  # br
        zb = (x1 - crop_um, y0, x1, y0 + crop_um)

    axins = ax.inset_axes(axes_bbox, zorder=6)
    img = mpimg.imread(str(background_image))
    axins.imshow(img, extent=(x0, x1, y0, y1), origin="upper",
                 interpolation="bilinear", aspect="auto")
    axins.set_xlim(zb[0], zb[2])
    axins.set_ylim(zb[1], zb[3])
    axins.set_xticks([])
    axins.set_yticks([])
    # Subtle inset border: the inset's position alone tells the reader
    # which corner of the chip it shows (TL inset = top-left corner of
    # the chip), so an additional red rectangle on the chip plus a
    # leader line just clutters the corner artwork — and on rotated
    # chips the rectangle collides with the QR/logo highlight frames.
    # The grey border keeps the inset visually distinct without
    # competing with anything else.
    for spine in axins.spines.values():
        spine.set_edgecolor("#444")
        spine.set_linewidth(1.4)


def _draw_qr_on_ax(ax: plt.Axes, data: str,
                   error: str = "m", mask: int | None = None,
                   mode: str | None = None,
                   fg: str = "#111", bg: str | None = None) -> None:
    """Render a QR code encoding `data` into the given matplotlib axes.

    Draws each 'on' module as a filled 1x1 rectangle in axes units and sets
    the axis limits to the QR's module grid. segno gives us the 2-D matrix
    directly, which sidesteps needing PIL or writing a temporary PNG.

    `error`, `mask` and `mode` are passed through so callers can lock the
    output to a known matrix — the ws-template QR uses a specific
    combination (error=H, mask=3, mode=numeric) and we want this render
    to be byte-identical to the chip's own QR. `boost_error=False`
    prevents segno from silently upgrading the EC level, which would
    change the matrix.
    """
    qr = segno.make(data, error=error, mask=mask, mode=mode,
                    boost_error=False)
    matrix = list(qr.matrix)
    n = len(matrix)
    if bg is not None:
        ax.add_patch(mpatches.Rectangle((0, 0), n, n, facecolor=bg,
                                        edgecolor="none"))
    for row_idx, row in enumerate(matrix):
        for col_idx, bit in enumerate(row):
            if bit:
                # Matrix rows go top-to-bottom; invert y so QR reads upright.
                ax.add_patch(mpatches.Rectangle(
                    (col_idx, n - 1 - row_idx), 1, 1,
                    facecolor=fg, edgecolor="none",
                ))
    ax.set_xlim(0, n)
    ax.set_ylim(0, n)
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)


def render(cell_name: str, pads: list[Pad], die_bb: tuple[float, float, float, float],
           out_png: Path, out_svg: Path, out_pdf: Path | None = None,
           background_image: Path | None = None) -> None:
    """Render pad diagram. If background_image is given, use it as the
    die-area background (typically a KLayout GDS render of the cell).
    Writes PNG + SVG, plus PDF if out_pdf is supplied."""
    x0, y0, x1, y1 = die_bb
    die_w = x1 - x0
    die_h = y1 - y0

    # Outside margin where labels sit, plus the gap between each pad's
    # rectangle and its adjacent text. label_gap is bumped to ~1.5% of
    # the die size so the rotated top/bottom labels don't sit visually
    # on the chip outline, and the L/R labels keep clear of the pad
    # rectangles. The fontsize cap further reserves the top ~25% of the
    # margin so the rotated labels never reach the axes top/bottom
    # spine.
    margin = max(die_w, die_h) * 0.20
    label_gap = max(die_w, die_h) * 0.015

    # Figure: fit the die into a 14" bounding box preserving aspect ratio,
    # then add a dedicated bottom strip (panel_strip_in) for the info
    # panel. We use subplots_adjust with explicit margins (rather than
    # tight_layout) so plot_w_in / plot_h_in below match what the axes
    # actually occupy — tight_layout would shrink the axes to make room
    # for tick labels and the title, which left the per-pad label width
    # cap ~20% too generous and let long pad names overflow the figure.
    canvas = 14.0
    total_w = die_w + 2 * margin
    total_h = die_h + 2 * margin
    if total_w >= total_h:
        fig_w = canvas
        fig_h_chip = canvas * total_h / total_w
    else:
        fig_h_chip = canvas
        fig_w = canvas * total_w / total_h
    # Panel-strip height scales with fig_w so wide figures get a
    # proportionally taller bottom strip — the QR is square (qr_in =
    # panel_h_in) and the project-code fontsize derives from
    # panel_h_in, so taller strip => bigger QR + bigger code text =>
    # the panel dominates the available space rather than huddling at
    # the right. The 0.24·fig_w factor was tuned so that a 14"-wide
    # figure gets ~3.4" of strip (≈ 3" panel) and a 9"-wide figure
    # falls back on the 2.4" floor.
    panel_strip_in = max(2.4, fig_w * 0.24)
    title_strip_in = 1.0  # space at top for title (two-line + padding)
    side_margin_in = 1.05  # space for y-axis label + tick labels
    right_margin_in = 0.45
    xlabel_strip_in = 0.7  # x-axis label + tick labels
    fig_h = fig_h_chip + panel_strip_in + title_strip_in + xlabel_strip_in

    # Axes box position in figure-fraction coords. Locked explicitly so
    # the per-label sizing math below knows the actual chip plot size.
    ax_left_frac = side_margin_in / fig_w
    ax_right_frac = 1 - right_margin_in / fig_w
    ax_top_frac = 1 - title_strip_in / fig_h
    ax_bottom_frac = (panel_strip_in + xlabel_strip_in) / fig_h
    plot_w_in = fig_w - side_margin_in - right_margin_in
    plot_h_in = fig_h_chip
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    fig.subplots_adjust(
        left=ax_left_frac, right=ax_right_frac,
        top=ax_top_frac, bottom=ax_bottom_frac,
    )

    # GDS render as background, or plain fill if none supplied.
    if background_image is not None and background_image.exists():
        img = mpimg.imread(str(background_image))
        ax.imshow(img, extent=(x0, x1, y0, y1), origin="upper",
                  zorder=0, interpolation="bilinear", aspect="auto")
    else:
        ax.add_patch(mpatches.Rectangle(
            (x0, y0), die_w, die_h,
            linewidth=0, edgecolor="none", facecolor="#fafafa", zorder=0,
        ))

    # Thin die outline on top of the render so the chip edge is obvious.
    ax.add_patch(mpatches.Rectangle(
        (x0, y0), die_w, die_h,
        linewidth=1.0, edgecolor="#222", facecolor="none", zorder=1.5,
    ))

    # Corner markers from the wafer.space template: QR in bottom-left,
    # logo in top-right. Both render in the same yellow metal as the rest
    # of the chip, so they're hard to spot — overlay a thick coloured
    # frame around each (slightly grown for visibility), then annotate
    # outside the die with a short leader line connecting the label to
    # the frame. Keeping the label text outside the die avoids covering
    # any of the chip artwork.
    qr_bb, logo_bb = _wsip_corners(die_bb)
    grow = max(die_w, die_h) * 0.005  # ~0.5% of die — bump frames outward
    # Label position offset from the die corner, into the outer margin.
    label_off = margin * 0.28
    # After 180° rotation the QR sits in the top-right and the logo in
    # the bottom-left, so each annotation's outer-margin label and
    # leader line are anchored to the corner the cell now occupies.
    #
    # That outer-margin corner is the same rectangle the corner zoom
    # inset (_add_corner_zoom, zorder=6) is sized to fill, so the
    # label/leader land *under* the inset and only the few characters
    # poking past its edge stay visible. The inset shows a zoom of this
    # very cell, so labelling it on top is coherent: the highlight box,
    # leader and text are drawn at zorder >6 to sit above the inset.
    for bb, col, label, anchor in (
        (qr_bb, WSIP_QR_COLOR, "ID QR", "tr"),
        (logo_bb, WSIP_LOGO_COLOR, "wafer.space logo", "bl"),
    ):
        bx0, by0, bx1, by1 = bb
        bx0 -= grow
        by0 -= grow
        bx1 += grow
        by1 += grow
        ax.add_patch(mpatches.Rectangle(
            (bx0, by0), bx1 - bx0, by1 - by0,
            linewidth=3.0, edgecolor=col, facecolor=col, alpha=0.22,
            zorder=7,
        ))
        if anchor == "bl":
            # Logo sits in bottom-left; label in outer bottom-left.
            tx, ty = x0 - label_off, y0 - label_off
            ha_l, va_l = "right", "top"
            leader_from = (bx0, by0)
        else:
            # QR sits in top-right; label in outer top-right.
            tx, ty = x1 + label_off, y1 + label_off
            ha_l, va_l = "left", "bottom"
            leader_from = (bx1, by1)
        ax.plot(
            [leader_from[0], tx], [leader_from[1], ty],
            color=col, linewidth=1.0, alpha=0.8, zorder=7.1,
        )
        ax.text(tx, ty, label,
                color=col, fontsize=10, ha=ha_l, va=va_l,
                family="monospace", fontweight="bold", zorder=7.2)

    # pt-per-µm conversion factors used by both the inside-pad number and
    # the outer label. Computed up front so the rectangle-drawing loop
    # can size the in-pad number text without needing a second pass.
    pt_per_um_x = 72.0 * plot_w_in / total_w
    pt_per_um_y = 72.0 * plot_h_in / total_h

    # Width of the widest pad number, used to right-align the digits in the
    # outer label so the column of pad names stays vertically aligned.
    num_width = max((len(str(p.num)) for p in pads if p.num is not None),
                    default=1)

    # Draw pads, colored by net class (signal / ground / power variants).
    # The pad number (assigned by _number_pads_ccw) is drawn centred inside
    # the rectangle in a contrasting color so each pad's index is readable
    # without cross-referencing the outer label.
    labelled = unlabelled = 0
    for pad in pads:
        has_net = pad.net is not None
        cls = classify_net(pad.net) if has_net else "signal"
        fill = PAD_COLORS[cls][0] if has_net else "#bbbbbb"
        ax.add_patch(mpatches.Rectangle(
            (pad.x0, pad.y0), pad.w, pad.h,
            linewidth=0.4,
            edgecolor="#222",
            facecolor=fill,
            zorder=2,
        ))
        if pad.num is not None:
            n_digits = len(str(pad.num))
            # Cap inside-pad font size by both pad dimensions, leaving
            # ~30% padding so the digits don't crowd the rectangle edge.
            width_pt = pad.w * pt_per_um_x / (n_digits * 0.6) * 0.7
            height_pt = pad.h * pt_per_um_y * 0.55
            num_fs = max(3.5, min(width_pt, height_pt, 14.0))
            ax.text(
                pad.cx, pad.cy, str(pad.num),
                ha="center", va="center",
                fontsize=num_fs,
                color=_contrast_text_color(fill),
                family="monospace", fontweight="bold",
                zorder=2.5,
            )
        if has_net:
            labelled += 1
        else:
            unlabelled += 1

    # Per-pad adaptive fontsize. Each label is sized independently to be as
    # large as possible while respecting two constraints:
    #
    #   1. Pad-to-neighbour spacing on its own edge: L/R labels render
    #      horizontally, so their cap-height (≈ fontsize in pt) can't exceed
    #      the min *vertical* spacing between L/R pads. T/B labels are
    #      rotated 90°, so their cap-height can't exceed the min *horizontal*
    #      spacing between T/B pads.
    #   2. Margin room outward from the die: an N-char monospace label is
    #      ~0.6·N·fontsize wide in points, so fontsize can't exceed
    #      (margin in pt) / (0.6·N) or the label runs past the figure edge.
    #
    # Computing this per-label rather than globally means a single outlier
    # like `CAPACITORS_MIM_2p0FF_8192FF_MAX_PERIM_1_PAD` (43 chars on TRID)
    # doesn't shrink every short label on the chip — it just shrinks itself.
    # Font sizing uses the inner plot dimensions (plot_w_in / plot_h_in),
    # not the full figure. The figure is padded with inset_strip_in on
    # every side which belongs to the zoom-inset strip, not the axes.
    # (pt_per_um_x / pt_per_um_y were defined above for the inside-pad
    # number sizing — reused here so both stay in sync.)
    margin_pt_x = margin * pt_per_um_x
    margin_pt_y = margin * pt_per_um_y
    safety = 0.85

    edge_ys: dict[str, list[float]] = {"L": [], "R": []}
    edge_xs: dict[str, list[float]] = {"T": [], "B": []}
    for p in pads:
        e = _classify_edge(p, x0, y0, x1, y1)
        if e in ("L", "R"):
            edge_ys[e].append(p.cy)
        else:
            edge_xs[e].append(p.cx)

    def _min_gap(coords: list[float]) -> float:
        if len(coords) < 2:
            return 1e9
        s = sorted(coords)
        return min(s[i + 1] - s[i] for i in range(len(s) - 1))

    edge_height_cap: dict[str, float] = {
        "L": _min_gap(edge_ys["L"]) * pt_per_um_y * safety,
        "R": _min_gap(edge_ys["R"]) * pt_per_um_y * safety,
        "T": _min_gap(edge_xs["T"]) * pt_per_um_x * safety,
        "B": _min_gap(edge_xs["B"]) * pt_per_um_x * safety,
    }
    # Outward margin (in µm) and the pt-per-µm factor for the axis the
    # rotated label extends along: L/R labels extend in x, T/B labels
    # extend in y because they're rotated 90°.
    edge_margin_um: dict[str, float] = {"L": margin, "R": margin,
                                         "T": margin, "B": margin}
    pt_per_um: dict[str, float] = {
        "L": pt_per_um_x, "R": pt_per_um_x,
        "T": pt_per_um_y, "B": pt_per_um_y,
    }

    def _pad_fontsize(pad: Pad, label_text: str) -> float:
        edge = _classify_edge(pad, x0, y0, x1, y1)
        height_cap = edge_height_cap[edge]
        # Width fit: 0.6 × fs × N chars must fit in the outward margin
        # MINUS the label_gap above the chip outline AND MINUS another
        # ~10% reserved for clearance from the axes spine. Cap labels at
        # ~70% of the available margin so a visible buffer remains
        # between the rotated label tops and the plot border.
        # `label_text` is the actual rendered string (with the right-
        # aligned pad-number prefix), so the width cap accounts for the
        # extra digits without forcing _pad_fontsize to recompute the
        # prefix itself.
        usable_pt = (edge_margin_um[edge] - label_gap) * pt_per_um[edge]
        width_cap = usable_pt / (len(label_text) * 0.6) * 0.85
        return max(4.0, min(height_cap, width_cap, 36.0))

    # Place each label directly in line with its pad (no leader lines).
    # L/R edge labels stay horizontal, sharing the pad's y-coordinate.
    # T/B edge labels are rotated 90°, sharing the pad's x-coordinate.
    # `rotation_mode="anchor"` makes ha/va apply to the rotated text's
    # bounding box so "left + rot=90" grows upward from the anchor
    # and "right + rot=90" grows downward.
    for pad in pads:
        edge = _classify_edge(pad, x0, y0, x1, y1)
        if edge == "L":
            tx, ty = x0 - label_gap, pad.cy
            ha, va, rot = "right", "center", 0
        elif edge == "R":
            tx, ty = x1 + label_gap, pad.cy
            ha, va, rot = "left", "center", 0
        elif edge == "T":
            tx, ty = pad.cx, y1 + label_gap
            ha, va, rot = "left", "center", 90
        else:  # B
            tx, ty = pad.cx, y0 - label_gap
            ha, va, rot = "right", "center", 90

        name = pad.net if pad.net else "?"
        # Right-align the pad number to num_width digits so the column
        # of labels lines up neatly even when pads 0..9 sit next to
        # 10..99 — visually a nicer match for monospace font output.
        if pad.num is not None:
            txt = f"{pad.num:>{num_width}} {name}"
        else:
            txt = name
        if pad.net:
            color = PAD_COLORS[classify_net(pad.net)][1]
            weight = "bold" if classify_net(pad.net) != "signal" else "normal"
        else:
            color = "#b00"
            weight = "normal"
        ax.text(
            tx, ty, txt,
            ha=ha, va=va, fontsize=_pad_fontsize(pad, txt), color=color,
            family="monospace", zorder=3, rotation=rot,
            rotation_mode="anchor", fontweight=weight,
        )

    ax.set_xlim(x0 - margin, x1 + margin)
    ax.set_ylim(y0 - margin, y1 + margin)
    ax.set_aspect("equal")
    ax.set_xlabel("x (µm)", fontsize=16)
    ax.set_ylabel("y (µm)", fontsize=16)
    ax.tick_params(axis="both", labelsize=14)
    # Shrink title size on narrow figures so it doesn't overflow the
    # right edge — TRID and the half-width chips have fig_w around 7"
    # which can't fit a 22pt 50-char subtitle. Cap at 22pt for the wide
    # chips (where there's room) and floor at 14pt for legibility.
    title_lines = [
        cell_name,
        f"{die_w:.0f} × {die_h:.0f} µm  ·  "
        f"{labelled} labelled pads, {unlabelled} unlabelled",
    ]
    title_chars = max(len(line) for line in title_lines)
    title_max_pt = (fig_w * 72) / (title_chars * 0.6) * 0.95
    title_fontsize = max(14.0, min(22.0, title_max_pt))
    ax.set_title("\n".join(title_lines),
                 fontsize=title_fontsize, fontweight="bold", pad=14)
    ax.grid(True, which="both", linewidth=0.8, alpha=0.7,
            color="#888", linestyle="--")

    # Axes box position is already locked via subplots_adjust above —
    # no tight_layout call so the per-pad label sizing math (which
    # used plot_w_in / plot_h_in for the pt-per-µm ratio) stays in
    # sync with what matplotlib actually renders.
    panel_strip_frac = panel_strip_in / fig_h

    # Info panel in the bottom-right: project code (big), slot size
    # (small), QR image. The QR encodes "G801<PROJ>" — the shuttle ID
    # plus the project's README code — and is rendered with segno
    # using parameters chosen so the matrix matches the per-chip QR
    # that the wafer.space precheck stamps into reticle.oas. The
    # project→mask dict at the top of the file was derived from
    # probe_segno_match_v2.py's brute-force comparison.
    #
    # Sizing: the panel fills the dedicated bottom strip (panel_strip_in)
    # almost edge-to-edge so the QR and project code dominate the space
    # under the chip plot rather than huddling in a small block. Text
    # row positions are derived from each fontsize-in-inches so the two
    # rows can never overlap — the previous implementation used magic
    # axes-fraction y values and the 42pt code text actually overlapped
    # the slot caption.
    code = _project_code(cell_name)
    size = computed_slot_size(die_w, die_h)

    # Reserve a small gutter inside the strip so the panel breathes; the
    # rest is panel content.
    panel_h_in = panel_strip_in - 0.30
    qr_in = panel_h_in

    # Project code ≈ 62% of panel height (cap-height fits with a top inset
    # and room below for the slot caption). Slot caption is ~24% the size
    # of the project code, monospace.
    code_fs_pt = panel_h_in * 72 * 0.62
    slot_fs_pt = code_fs_pt * 0.24

    # Bracket the bottom strip: project code anchored at the axes' left
    # edge (so it lines up with the chip plot above it); QR anchored at
    # the figure's right edge with a small gutter. On wide figures this
    # naturally fills the horizontal space between them; on narrow ones
    # they end up close together but never overlap because text_w_in is
    # the actual rendered width of the 4-char code at code_fs_pt.
    text_x = ax_left_frac
    text_w_in = len(code) * 0.62 * code_fs_pt / 72 + 0.20
    qr_right_inset_in = 0.35
    qr_x = 1.0 - (qr_right_inset_in + qr_in) / fig_w

    # Centre the panel inside the bottom strip vertically.
    panel_y = (panel_strip_in - panel_h_in) / 2 / fig_h
    ax_text = fig.add_axes((text_x, panel_y,
                            text_w_in / fig_w, panel_h_in / fig_h))
    ax_text.axis("off")

    # Stack the two text rows: project code anchored top, slot caption
    # anchored top below it with a measured 0.12" gap. Both `va="top"`
    # so the y-anchor names the top of the text bbox; bottom = top -
    # fontsize_in_inches / panel_h_in. This makes overlap impossible.
    code_top_frac = 0.96
    code_h_frac = (code_fs_pt / 72) / panel_h_in
    inter_row_gap_frac = 0.12 / panel_h_in
    slot_top_frac = code_top_frac - code_h_frac - inter_row_gap_frac
    ax_text.text(0, code_top_frac, code, fontsize=code_fs_pt,
                 fontweight="bold", family="monospace", color="#111",
                 ha="left", va="top")
    ax_text.text(0, slot_top_frac, f"slot {size}", fontsize=slot_fs_pt,
                 family="monospace", color="#555", ha="left", va="top")

    ax_qr = fig.add_axes((qr_x, panel_y, qr_in / fig_w, qr_in / fig_h))
    qr_data = f"{WSIP_QR_DATA_PREFIX}{code}"
    qr_mask = WSIP_QR_PROJECT_MASKS.get(code)  # None → segno auto-picks
    _draw_qr_on_ax(ax_qr, qr_data,
                   error=WSIP_QR_ERROR, mask=qr_mask,
                   mode=WSIP_QR_MODE, fg="#111", bg="#ffffff")
    for spine in ax_qr.spines.values():
        spine.set_visible(True)
        spine.set_edgecolor("#111")
        spine.set_linewidth(0.8)

    # Zoom insets tucked into the empty corner regions of the axes
    # margin — rectangles bounded by the die edge on two sides and the
    # axes boundary on the other two. These regions contain no pad
    # labels (L/R labels are vertical strips next to the die; T/B
    # labels are horizontal strips above/below), so the insets never
    # collide with any label text. Positions are axes-relative; the
    # inset size in axes fractions is set so the inset comes close to
    # filling the margin corner.
    if background_image is not None and background_image.exists():
        crop_um = min(ZOOM_CROP_UM, 0.5 * min(die_w, die_h))
        # Fraction of the axes spanned by the margin in each axis.
        margin_fx = margin / total_w
        margin_fy = margin / total_h
        # Leave a small gap between the inset and the die/axes edges.
        pad = 0.015
        inset_w = margin_fx - 2 * pad
        inset_h = margin_fy - 2 * pad
        corner_axes_bbox = {
            "tl": (pad, 1 - margin_fy + pad, inset_w, inset_h),
            "tr": (1 - margin_fx + pad, 1 - margin_fy + pad, inset_w, inset_h),
            "bl": (pad, pad, inset_w, inset_h),
            "br": (1 - margin_fx + pad, pad, inset_w, inset_h),
        }
        for corner, bbox in corner_axes_bbox.items():
            _add_corner_zoom(fig, ax, background_image, die_bb,
                             corner, crop_um, bbox)

    fig.savefig(out_png, dpi=180)
    fig.savefig(out_svg)
    if out_pdf is not None:
        fig.savefig(out_pdf)
    plt.close(fig)


def setup_layout_view(layout: kdb.Layout) -> klay.LayoutView:
    """Build a LayoutView that renders like the ws-run1 README images.

    Colors and dither patterns come from ws-run1/lyp/gf180mcu.lyp, but the
    raw .lyp enables ~15 layers (wells, poly, dummies, substrate) in
    clashing colors. We keep only the layers in BG_VISIBLE_LAYERS visible
    so the render stays two-tone (yellow metal + dark red pads) and the
    pad ring reads clearly. The other visual toggles:

    - `grid-visible=false`: suppress KLayout's GUI coordinate grid.
    - `background-color=#ffffff`: white canvas outside the die so the
      overlay labels in make_diagrams.render sit on white paper.
    - `text-visible=false`: don't draw text objects from the layout — our
      matplotlib labels do the annotation; leaving KLayout's in adds
      unreadable microscopic strings inside every pad.
    - `draw-cell-frame=false`: suppress the default black rectangle drawn
      around each instance, which otherwise frames every pad cell and
      every logo sub-cell in a heavy black border.
    """
    lv = klay.LayoutView()
    lv.show_layout(layout, True)
    lv.load_layer_props(str(LYP))
    lv.set_config("grid-visible", "false")
    lv.set_config("background-color", "#ffffff")
    lv.set_config("text-visible", "false")
    lv.set_config("draw-cell-frame", "false")
    # Mask down to the two-tone layer set, and force solid fills on
    # those layers. The .lyp ships every metal with dither pattern I9
    # (inverted horizontal stripes) and pad with I5, which produces
    # the busy striped look that doesn't match shipping/die_renders.
    # Pattern 0 renders solid colour, so polygon edges (and the dummy-
    # fill grid hidden inside large metal regions) read cleanly without
    # the dither distracting from the chip's own structure.
    for it in lv.each_layer():
        keep = (it.source_layer, it.source_datatype) in BG_VISIBLE_LAYERS
        it.visible = keep
        if keep:
            it.dither_pattern = 0
    lv.update_content()
    return lv


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    bg_cache = OUT_DIR.parent / "tmp" / "gds_renders"
    bg_cache.mkdir(parents=True, exist_ok=True)

    layout = kdb.Layout()
    print(f"Reading {OAS} ...")
    t0 = time.time()
    layout.read(str(OAS))
    print(f"  loaded in {time.time() - t0:.1f}s — {layout.cells()} cells")

    lv = setup_layout_view(layout)

    top = next(iter(layout.top_cells()))
    # Skip filler/text utility cells when generating design diagrams.
    skip = {"RETICLE_FILL", "TEXT"}

    instances: list[str] = []
    for inst in top.each_inst():
        name = inst.cell.name
        if name in skip:
            continue
        if name not in instances:
            instances.append(name)
    print(f"\n{len(instances)} unique designs to render")

    summary: list[tuple[str, str, int, int]] = []
    for i, name in enumerate(sorted(instances), start=1):
        t_start = time.time()
        cell = layout.cell(name)
        if cell is None:
            print(f"  [{i}/{len(instances)}] {name}: cell not found, skipping")
            continue
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

        # Drop probe / internal pads — only the ring of peripheral pads
        # is annotated. Interior pads end up in MOS2 / TRID / ISHI-style
        # test structures and aren't part of the chip's pinout.
        pads = [p for p in pads if _is_peripheral(p, *die_bb)]

        # Rotate the entire chip 180° so the QR cell sits in the
        # top-right corner (the wafer.space convention) instead of the
        # GDS-native bottom-left. die_bb is invariant under 180°
        # rotation around its own centre, so it stays the same.
        pads = [_rotate_pad_180(p, die_bb) for p in pads]

        # Assign pad numbers walking CCW from the first pad left of the
        # QR cell. Done after the rotation so the ordering is anchored
        # to the QR's *displayed* position (top-right), not its native
        # GDS position (bottom-left).
        _number_pads_ccw(pads, die_bb)

        # Cross-check the README slot size against the actual GDS. A
        # mismatch either means the README is stale or the chip was
        # laid out at the wrong size; either way it's worth surfacing.
        code = _project_code(name)
        computed = computed_slot_size(
            die_bb[2] - die_bb[0], die_bb[3] - die_bb[1])
        readme = PROJECT_SIZES_README.get(code)
        if readme is not None and readme != computed:
            print(f"    WARNING: {code} README says {readme!r} but "
                  f"GDS is {computed!r} "
                  f"({die_bb[2] - die_bb[0]:.0f} x "
                  f"{die_bb[3] - die_bb[1]:.0f} um)")

        # Suffix the slot size into the output filenames so the size is
        # readable at a glance from a directory listing. bg_png stays
        # keyed by name — slot size is a deterministic function of the
        # cell, so a suffix would add no information *and* invalidate
        # every cached render.
        stem = f"{name}_{computed}"
        out_png = OUT_DIR / f"{stem}.png"
        out_svg = OUT_DIR / f"{stem}.svg"
        out_pdf = OUT_DIR / f"{stem}.pdf"
        bg_png = bg_cache / f"{name}.png"
        render_gds_background(lv, name, layout, die_bb, bg_png)
        # KLayout writes the chip in its native orientation; rotate the
        # raster 180° to match the rotated pad coords above.
        _rotate_image_180(bg_png)
        render(name, pads, die_bb, out_png, out_svg, out_pdf,
               background_image=bg_png)

        labelled = sum(1 for p in pads if p.net)
        summary.append((name, computed, len(pads), labelled))
        print(f"  [{i:>2}/{len(instances)}] {name}: {len(pads):>3} pads, "
              f"{labelled:>3} labelled  slot {computed}  "
              f"({time.time() - t_start:.1f}s)")

    # Write a summary index.
    index = OUT_DIR / "index.md"
    with index.open("w") as fh:
        fh.write("# Reticle pad-diagram index\n\n")
        fh.write("| Design cell | Pads | Labelled | Diagram |\n")
        fh.write("|---|---|---|---|\n")
        for name, size, n, nl in summary:
            fname = f"{name}_{size}.png"
            fh.write(f"| {name} | {n} | {nl} | [{fname}]({fname}) |\n")
    print(f"\nWrote {index}")


if __name__ == "__main__":
    main()
