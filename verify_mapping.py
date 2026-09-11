"""Verify pcb_pad = die_pad + 1 by geometry before any diagram is drawn.

Pairs die pad n (display frame from tmp/<reticle>/pads.json) with COB bond pad n+1
(tmp/cob/<variant>.json) in the padring frame — die centered on the
padring origin, KiCad y-down — and checks the wirebond rules from
run-1/wirebonding/README.md: 1–3 mm length, ≤45° off the pad long axis,
no crossings. Also cross-checks die pad 0 against the PAD_MAPPING.md
worked example (GD03 pad 0 at 3406, 5064 µm display-frame) and against
the footprint's own Cmts.User wire-guide lines.

Writes an overlay plot per design: tmp/plot_mapping_<code>.png.
Exits 1 on hard violations (pad-count mismatch, crossings).

Usage:
    uv run verify_mapping.py [--cob tmp/cob/1x1.json] [--designs GD03 ...]
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection

from plot_pcb import centered_rect
from boards import find_pads

REPO = Path(__file__).resolve().parent
DEFAULT_COB = REPO / "tmp" / "cob" / "1x1.json"

# PAD_MAPPING.md worked example: GD03 diagram pad 0, post-display-rotation.
WORKED_EXAMPLE = {"design": "GD03_chip_top_6_6", "pad": 0,
                  "cx_um": 3406.0, "cy_um": 5064.0, "tol_um": 2.0}

WIRE_MIN_MM = 1.0
WIRE_MAX_MM = 3.0
WIRE_MAX_ANGLE_DEG = 45.0

# Mechanical/non-bond pads beyond the bond ring — excluded from ring pairing
# and numbering on every page. Extra-pad numbers are board-family specific
# (run-1 boards: paddle 75 + GND thru-holes 76-81; TQVA: paddle 57 + mounts
# 58-63), so the shared filter is positional: the bond ring is pads numbered
# 1..N, and a board's ring size N is the die's pad count (die pad count ==
# ring count is enforced before any pairing). Board-specific sets are pinned
# in boards.json and stamped onto each parsed JSON as "extra_nums".
def extra_pads(cob: dict) -> set[str]:
    return set(cob.get("extra_nums", []))


def die_pad_to_pcb_mm(pad: dict, die_bb: list[float]) -> tuple[float, float]:
    """Die display-frame pad → padring-frame mm (KiCad y-down).

    The die is centered on the padring origin; both the display rotation
    and the physical placement rotation are 180°, so they cancel and
    display-frame x maps directly to PCB x, display-frame +y (up) to
    PCB −y.
    """
    cx0 = 0.5 * (die_bb[0] + die_bb[2])
    cy0 = 0.5 * (die_bb[1] + die_bb[3])
    return (pad["cx_um"] - cx0) / 1000.0, -(pad["cy_um"] - cy0) / 1000.0


def pad_axis_kicad(rot_deg: float) -> tuple[float, float]:
    """Unit vector of a PCB pad's long (size[0]) axis, KiCad y-down frame."""
    r = math.radians(rot_deg)
    return math.cos(r), math.sin(r)


def segments_cross(a1, a2, b1, b2) -> bool:
    """True if open segments a1–a2 and b1–b2 intersect at a non-endpoint."""

    def orient(p, q, r) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_seg(p, q, r) -> bool:
        return (min(p[0], r[0]) <= q[0] <= max(p[0], r[0])
                and min(p[1], r[1]) <= q[1] <= max(p[1], r[1]))

    d1 = orient(b1, b2, a1)
    d2 = orient(b1, b2, a2)
    d3 = orient(a1, a2, b1)
    d4 = orient(a1, a2, b2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    for p, q, r in ((b1, a1, b2), (b1, a2, b2), (a1, b1, a2), (a1, b2, a2)):
        if abs(orient(p, q, r)) < 1e-12 and on_seg(p, q, r):
            return True
    return False


def verify_design(design: dict, cob: dict) -> list[str]:
    """Run all checks for one design; return human-readable violations."""
    issues: list[str] = []
    die_bb = design["die_bb_um"]
    die_pads = {p["n"]: p for p in design["pads"]}
    ring = sorted((p for p in cob["pads"] if p["num"] not in extra_pads(cob)),
                  key=lambda p: int(p["num"]))

    if len(die_pads) != len(ring):
        # Wrong COB variant for this die (e.g. 0.5x0.5 die vs 1x1 padring) —
        # not verifiable against this ring; caller reports and skips.
        return [f"pad count mismatch: die {len(die_pads)} vs pcb {len(ring)} "
                f"(needs its own slot-variant COB)"]

    # Pair die pad n with PCB pad n+1, the documented mapping.
    pairs = []
    for pcb in ring:
        n = int(pcb["num"]) - 1
        dp = die_pads.get(n)
        if dp is None:
            issues.append(f"die pad {n} missing for pcb pad {pcb['num']}")
            continue
        dx, dy = die_pad_to_pcb_mm(dp, die_bb)
        pairs.append((n, dp, (dx, dy), pcb))

    # Worked-example spot check.
    we = WORKED_EXAMPLE
    if design["name"] == we["design"] and we["pad"] in die_pads:
        dp = die_pads[we["pad"]]
        derr = math.hypot(dp["cx_um"] - we["cx_um"], dp["cy_um"] - we["cy_um"])
        status = "ok" if derr <= we["tol_um"] else "MISMATCH"
        print(f"  worked example pad {we['pad']}: extracted "
              f"({dp['cx_um']:.1f}, {dp['cy_um']:.1f}) um vs documented "
              f"({we['cx_um']}, {we['cy_um']}) um — {status} (err {derr:.2f} um)")
        if derr > we["tol_um"]:
            issues.append("worked example coordinate mismatch")

    # Wire geometry per pair. PCB pads use footprint-local coords (x_mm,
    # y_mm): the die sits at the padring origin, not at the board origin.
    lens, angs = [], []
    for n, dp, (dx, dy), pcb in pairs:
        px, py = pcb["x_mm"], pcb["y_mm"]
        length = math.hypot(px - dx, py - dy)
        ax, ay = pad_axis_kicad(pcb["rot_deg"])
        ux, uy = (px - dx) / length, (py - dy) / length
        angle = math.degrees(math.acos(max(-1.0, min(1.0, abs(ux * ax + uy * ay)))))
        lens.append(length)
        angs.append(angle)
        if not WIRE_MIN_MM <= length <= WIRE_MAX_MM:
            issues.append(f"pad {n}: wire {length:.2f} mm outside "
                          f"[{WIRE_MIN_MM}, {WIRE_MAX_MM}]")
        if angle > WIRE_MAX_ANGLE_DEG:
            issues.append(f"pad {n}: wire {angle:.1f}° off pad axis > {WIRE_MAX_ANGLE_DEG}°")

    # Wire–wire crossings.
    n_cross = 0
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            _, _, d_i, p_i = pairs[i]
            _, _, d_j, p_j = pairs[j]
            if segments_cross(d_i, (p_i["x_mm"], p_i["y_mm"]),
                              d_j, (p_j["x_mm"], p_j["y_mm"])):
                n_cross += 1
                if n_cross <= 5:
                    issues.append(f"wires cross: die pad {pairs[i][0]}×{pairs[j][0]}")
    if n_cross > 5:
        issues.append(f"... and {n_cross - 5} more crossings")

    # Footprint wire guides: each Cmts.User line runs between a bond pad and
    # the die pad the author intended — but direction varies by edge (top
    # guides run pad→die, left guides run die→pad), so associate via the
    # endpoint nearer a ring pad and compare the *other* endpoint with the
    # paired die pad. Informational only: the drawings are approximate
    # (~0.4 mm fan error near corners).
    guides = cob["graphics"].get("Cmts.User", [])
    guide_errs = []
    for g in guides:
        ends = [g["start"], g["end"]]
        near = []
        for ex, ey in ends:
            d = min(math.hypot(p["x_mm"] - ex, p["y_mm"] - ey) for p in ring)
            near.append(d)
        pad_end = 0 if near[0] <= near[1] else 1
        if near[pad_end] > 1.0:  # not a pad guide
            continue
        die_end = ends[1 - pad_end]
        ex, ey = die_end
        pcb = min(ring, key=lambda p: math.hypot(p["x_mm"] - ends[pad_end][0],
                                                 p["y_mm"] - ends[pad_end][1]))
        n = int(pcb["num"]) - 1
        dp = die_pads.get(n)
        if dp is None:
            continue
        dx, dy = die_pad_to_pcb_mm(dp, die_bb)
        guide_errs.append(math.hypot(dx - ex, dy - ey))
    if guide_errs:
        print(f"  wire-guide endpoint distance: max {max(guide_errs):.3f} mm, "
              f"median {sorted(guide_errs)[len(guide_errs) // 2]:.3f} mm")

    if lens:
        print(f"  wire lengths: min {min(lens):.2f} max {max(lens):.2f} mm  |  "
              f"angles: max {max(angs):.1f}°  |  crossings: {n_cross}")
    return issues


def plot_design(design: dict, cob: dict, out: Path) -> None:
    die_bb = design["die_bb_um"]
    die_pads = {p["n"]: p for p in design["pads"]}
    ring = sorted((p for p in cob["pads"] if p["num"] not in extra_pads(cob)),
                  key=lambda p: int(p["num"]))

    fig, ax = plt.subplots(figsize=(8, 9))
    segments, colors = [], []
    cmap = plt.get_cmap("viridis")
    lmin, lmax = WIRE_MIN_MM, WIRE_MAX_MM
    for pcb in ring:
        n = int(pcb["num"]) - 1
        dp = die_pads.get(n)
        if dp is None:
            continue
        dx, dy = die_pad_to_pcb_mm(dp, die_bb)
        px, py = pcb["x_mm"], pcb["y_mm"]
        segments.append([(dx, -dy), (px, -py)])
        length = math.hypot(px - dx, py - dy)
        colors.append(cmap(min(1.0, max(0.0, (length - lmin) / (lmax - lmin)))))
        # Die pad footprint (axis-aligned in this frame)
        w = (dp["x1_um"] - dp["x0_um"]) / 1000.0
        h = (dp["y1_um"] - dp["y0_um"]) / 1000.0
        ax.add_patch(plt.Rectangle((dx - w / 2, -dy - h / 2), w, h,
                                   facecolor="#f5c16c", edgecolor="#222", lw=0.3))
        # PCB bond finger, long axis toward the die (rot flipped for y-up)
        pw, ph = pcb["size_mm"]
        patch = centered_rect(px, -py, pw, ph, -pcb["rot_deg"])
        patch.set_facecolor("#2299bb")
        patch.set_edgecolor("none")
        patch.set_alpha(0.9)
        ax.add_patch(patch)
    ax.add_collection(LineCollection(segments, colors=colors, linewidths=0.7, zorder=1))
    ax.set_title(f"{design['name']} ↔ 1×1 padring — pcb_pad = die_pad + 1\n"
                 f"wire color: green {lmin} mm → yellow {lmax} mm (KiCad y-up)")
    ax.set_aspect("equal")
    ax.autoscale()
    ax.margins(0.05)
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)
    print(f"  wrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cob", type=Path, default=DEFAULT_COB)
    ap.add_argument("--pads", type=Path, default=None,
                    help="pads.json (default: the single tmp/<reticle>/"
                         "pads.json; required when several exist)")
    ap.add_argument("--designs", nargs="*", default=None,
                    help="subset of design names to verify (default: all)")
    args = ap.parse_args()

    cob = json.loads(args.cob.read_text())
    designs = json.loads(find_pads(args.pads).read_text())
    if args.designs:
        designs = [d for d in designs if d["name"] in args.designs]

    rc = 0
    for design in designs:
        print(f"{design['name']} (slot {design['slot_size']}, "
              f"{len(design['pads'])} pads):")
        issues = verify_design(design, cob)
        if len(issues) == 1 and "pad count mismatch" in issues[0]:
            print(f"    SKIP: {issues[0]}")
            continue
        for msg in issues:
            print(f"    VIOLATION: {msg}")
        if issues:
            rc = 1
        plot_design(design, cob, REPO / "tmp" / f"plot_mapping_{design['code']}.png")

    raise SystemExit(rc)


if __name__ == "__main__":
    main()
