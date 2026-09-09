"""Plot a parsed COB padring JSON for eyeballing orientation.

Raw sanity-check renderer for parse_pcb.py output: board outline, bond
pads colored by net class, pad numbers, and the padring annotation
graphics. Coordinates are plotted math-up (y negated from KiCad), which
puts pad 1 in the top-right — the same corner the diagram numbering and
the die's placement rotation expect.

Usage:
    uv run plot_pcb.py [--json PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Polygon

REPO = Path(__file__).resolve().parent
DEFAULT_JSON = REPO / "tmp" / "cob" / "1x1.json"
DEFAULT_OUT = REPO / "tmp" / "plot_padring_1x1.png"

# Net class → color, matching the run-1 README palette
# (GND gray, VDD red, PWR_AUX blue, signals the default cyan).
CLASS_COLORS = {
    "GND": "#777777",
    "VDD_IO": "#cc2222",
    "VDD_CORE": "#8b0000",
    "PWR_AUX": "#2244cc",
    "signal": "#2299bb",
}


def net_class(pad: dict) -> str:
    pf = pad.get("pinfunction", "")
    for cls in CLASS_COLORS:
        if pf.startswith(cls + "_") or pf == cls:
            return cls
    return "signal"


def centered_rect(x: float, y: float, w: float, h: float, angle_deg: float) -> Polygon:
    """Corners of a w×h rect centered at (x, y), rotated angle_deg CCW."""
    r = math.radians(angle_deg)
    c, s = math.cos(r), math.sin(r)
    pts = []
    for dx, dy in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2)):
        pts.append((x + dx * c - dy * s, y + dx * s + dy * c))
    return Polygon(pts, closed=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", type=Path, default=DEFAULT_JSON)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    data = json.loads(args.json.read_text())

    # Recentre on the padring origin: this is the frame the die is placed in
    # and the frame PAD_MAPPING.md quotes (pad 1 at +3.20, −4.70 mm).
    ox, oy = data["padring"]["at_mm"]

    fig, ax = plt.subplots(figsize=(8, 9))

    # Board outline (mm relative to padring origin, y negated)
    for shape in data["edge_cuts"]:
        if shape["type"] == "rect" and "start" in shape:
            (x0, y0), (x1, y1) = shape["start"], shape["end"]
            x0, y0, x1, y1 = x0 - ox, y0 - oy, x1 - ox, y1 - oy
            ax.add_patch(plt.Rectangle((min(x0, x1), -max(y0, y1)), abs(x1 - x0), abs(y1 - y0),
                                       fill=False, edgecolor="black", lw=1.2))
        elif "pts" in shape:
            ax.add_patch(Polygon([(px - ox, -(py - oy)) for px, py in shape["pts"]],
                                 closed=True, fill=False, edgecolor="black", lw=1.2))

    # Padring graphics are already footprint-local — the offset makes that
    # the plot frame, so draw them as-is.
    fx, fy = 0.0, 0.0
    for layer, style in (("Dwgs.User", dict(color="#bbbbbb", lw=0.5)),
                         ("Cmts.User", dict(color="#66aaff", lw=0.5, alpha=0.6)),
                         ("F.Mask", dict(color="#cc44aa", lw=1.0)),
                         ("F.Cu", dict(color="#996633", lw=0.8, alpha=0.8)),
                         ("F.SilkS", dict(color="#33aa33", lw=0.8))):
        for shape in data["graphics"].get(layer, []):
            if shape["type"] == "line" and "start" in shape:
                (x0, y0), (x1, y1) = shape["start"], shape["end"]
                ax.plot([fx + x0, fx + x1], [-(fy + y0), -(fy + y1)], **style)
            elif shape["type"] == "rect" and "start" in shape:
                (x0, y0), (x1, y1) = shape["start"], shape["end"]
                ax.add_patch(plt.Rectangle((fx + min(x0, x1), -(fy + max(y0, y1))),
                                           abs(x1 - x0), abs(y1 - y0), fill=False, **style))
            elif shape["type"] == "poly" or "pts" in shape:
                pts = [(fx + px, -(fy + py)) for px, py in shape["pts"]]
                ax.add_patch(Polygon(pts, closed=True, fill=False, **style))
            elif shape["type"] == "circle" and "center" in shape:
                cx, cy = shape["center"]
                ex, ey = shape["end"]
                r = math.hypot(ex - cx, ey - cy)
                ax.add_patch(plt.Circle((fx + cx, -(fy + cy)), r, fill=False, **style))

    # Other footprints (rocket fiducial, mezzanine connector, caps) as markers
    for fp in data["other_footprints"]:
        x, y = fp["at_mm"][0] - ox, -(fp["at_mm"][1] - oy)
        ax.plot(x, y, "+", color="#bb7733", ms=8, mew=1.2)
        ax.annotate(fp["reference"], (x, y), textcoords="offset points",
                    xytext=(6, 6), fontsize=7, color="#bb7733")

    # Pads: bond ring by class, extras (75..81) outlined distinctly
    for pad in data["pads"]:
        x, y = pad["gx_mm"] - ox, pad["gy_mm"] - oy
        w, h = pad["size_mm"]
        # KiCad y-down rot θ becomes CCW −θ in the math-up plot frame
        poly = centered_rect(x, -y, w, h, -pad["rot_deg"])
        extra = pad["num"] in {"75", "76", "77", "78", "79", "80", "81"}
        if extra:
            poly.set_facecolor("none")
            poly.set_edgecolor(CLASS_COLORS[net_class(pad)])
            poly.set_linewidth(1.0)
            poly.set_linestyle("--")
        else:
            poly.set_facecolor(CLASS_COLORS[net_class(pad)])
            poly.set_edgecolor("none")
        ax.add_patch(poly)
        if not extra:
            ax.annotate(pad["num"], (x, -y), fontsize=4.5, ha="center", va="center",
                        color="black")

    # Highlight pad 1 and pad 74 — orientation checkpoints
    by_num = {p["num"]: p for p in data["pads"]}
    for num, color in (("1", "red"), ("74", "orange")):
        p = by_num[num]
        x, y = p["gx_mm"] - ox, -p["gy_mm"] + oy
        ax.plot(x, y, "o", ms=10, mfc="none", mec=color, mew=1.5)
        ax.annotate(f"pad {num}", (x, y), textcoords="offset points",
                    xytext=(10, 10), fontsize=8, color=color, fontweight="bold")

    padring = data["padring"]
    ax.set_title(f"{padring['lib_id']}  ref {padring['reference']}\n"
                 f"padring origin (0,0), math-up, pad 1 top-right")
    ax.set_aspect("equal")
    ax.autoscale()
    ax.margins(0.05)
    ax.set_xlabel("x mm")
    ax.set_ylabel("y mm (up)")
    fig.tight_layout()
    fig.savefig(args.out, dpi=200)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
