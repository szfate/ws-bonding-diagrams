"""Parse a KiCad COB breakout .kicad_pcb into JSON for the bonding diagrams.

Extracts everything the bonding pages need from the padring footprint —
all pads (number, position, size, rotation, net, pinfunction) plus the
footprint graphics (die courtyard, padring annotations, mask opening) —
along with the board outline from Edge.Cuts, the copper (segments, vias,
zone pour polygons), board-level silkscreen/Eco texts and graphics, and a
summary of the other footprints (e.g. the mezzanine connector).

Coordinates are kept in KiCad convention: millimetres, y pointing down.
Callers must negate y before plotting in a math-up frame (see
PAD_MAPPING.md §4 in wafer-space-die-pad-diagrams for the full trap).

Usage:
    uv run parse_pcb.py --board tqva [--allow-dirty]
    uv run parse_pcb.py [--pcb PATH] [--out PATH]     # manual, no boards.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from boards import fetch_pcb, load_boards, resolve_board

REPO = Path(__file__).resolve().parent
DEFAULT_PCB = REPO.parent / "chip-on-board-wire-bonded-pcbs" / "run-1" / "1x1-cob" / "1x1-mezzanine.kicad_pcb"
DEFAULT_OUT = REPO / "tmp" / "cob" / "1x1.json"

PADRING_LIB_ID = "padring"

GRAPHIC_SYMBOLS = {
    "fp_line": ("line", ("start", "end")),
    "gr_line": ("line", ("start", "end")),
    "fp_rect": ("rect", ("start", "end")),
    "gr_rect": ("rect", ("start", "end")),
    "fp_poly": ("poly", None),
    "gr_poly": ("poly", None),
    "fp_circle": ("circle", ("center", "end")),
    "gr_circle": ("circle", ("center", "end")),
    "fp_arc": ("arc", ("start", "mid", "end")),
    "gr_arc": ("arc", ("start", "mid", "end")),
}


# ---------------------------------------------------------------------------
# S-expression reader

def parse_sexp(text: str) -> list:
    """Parse KiCad S-expressions into nested lists (first item = symbol)."""
    pos = 0
    n = len(text)

    def parse_one() -> object:
        nonlocal pos
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            raise ValueError("unexpected end of input")
        ch = text[pos]
        if ch == "(":
            pos += 1
            items = []
            while True:
                while pos < n and text[pos].isspace():
                    pos += 1
                if pos < n and text[pos] == ")":
                    pos += 1
                    return items
                items.append(parse_one())
        if ch == '"':
            pos += 1
            out = []
            while pos < n:
                c = text[pos]
                if c == "\\" and pos + 1 < n:
                    nxt = text[pos + 1]
                    out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(nxt, nxt))
                    pos += 2
                    continue
                if c == '"':
                    pos += 1
                    return "".join(out)
                out.append(c)
                pos += 1
            raise ValueError("unterminated string")
        start = pos
        while pos < n and not text[pos].isspace() and text[pos] not in "()":
            pos += 1
        return text[start:pos]

    result = parse_one()
    return result if isinstance(result, list) else [result]


def children(node: list, symbol: str) -> list[list]:
    """All direct children of `node` whose first item equals `symbol`."""
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == symbol]


def first(node: list, symbol: str) -> list | None:
    """First direct child of `node` whose first item equals `symbol`."""
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == symbol:
            return c
    return None


def _xy(node: list) -> tuple[float, float]:
    # KiCad writes coordinates as bare numbers: (start 1.0 2.0) / (xy 1.0 2.0)
    return float(node[1]), float(node[2])


def _xy_list(node: list) -> list[list[float]]:
    return [list(_xy(c)) for c in children(node, "xy")]


def _find_all(node: list, symbol: str) -> list[list]:
    """All descendant nodes (depth-first) whose first item equals `symbol`."""
    found = []
    for c in node[1:]:
        if isinstance(c, list) and c:
            if c[0] == symbol:
                found.append(c)
            found.extend(_find_all(c, symbol))
    return found


# ---------------------------------------------------------------------------
# Extraction

def as_float(s: str | None) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_at(node: list) -> tuple[float, float, float]:
    """(at x y [rot]) → (x, y, rot); rot defaults to 0."""
    at = first(node, "at")
    if at is None:
        return 0.0, 0.0, 0.0
    vals = at[1:]
    x = float(vals[0])
    y = float(vals[1])
    rot = float(vals[2]) if len(vals) > 2 else 0.0
    return x, y, rot


def parse_pad(pad: list) -> dict:
    x, y, rot = parse_at(pad)
    size = first(pad, "size")
    net = first(pad, "net")
    out = {
        "num": pad[1],
        "type": pad[2],
        "shape": pad[3],
        "x_mm": x,
        "y_mm": y,
        "rot_deg": rot,
        "size_mm": [float(size[1]), float(size[2])] if size else None,
        "layers": [l[1] for l in children(pad, "layer")],
    }
    for key, sym in (("net", "net"), ("pinfunction", "pinfunction"), ("pintype", "pintype")):
        node = first(pad, sym)
        if node:
            out[key] = node[1]
    drill = first(pad, "drill")
    if drill:
        out["drill_mm"] = float(drill[1])
    if net:
        out["net_num"] = as_float(net[1])
    return out


def parse_graphic(node: list) -> dict | None:
    sym = node[0]
    if sym not in GRAPHIC_SYMBOLS:
        return None
    kind, coord_syms = GRAPHIC_SYMBOLS[sym]
    # Filled shapes (KiCad 8) carry plural `(layers ...)`; outlines carry
    # singular `(layer ...)`. Prefer the singular, fall back to the
    # plural's first entry.
    layer_node = first(node, "layer")
    layers_node = first(node, "layers")
    if layer_node:
        layer, layers = layer_node[1], [layer_node[1]]
    elif layers_node:
        layer, layers = layers_node[1], list(layers_node[1:])
    else:
        layer, layers = None, []
    out = {"type": kind, "layer": layer, "layers": layers}
    if coord_syms is None:  # poly
        pts_node = first(node, "pts")
        out["pts"] = _xy_list(pts_node) if pts_node else []
    else:
        for s in coord_syms:
            c = first(node, s)
            if c:
                out[s] = list(_xy(c))
    stroke = first(node, "stroke")
    if stroke:
        sw = first(stroke, "width")
        if sw:
            out["stroke_mm"] = float(sw[1])
    fill = first(node, "fill")
    if fill:
        out["fill"] = fill[1] in ("yes", "true", "solid")
    return out


def parse_text(node: list) -> dict | None:
    at = first(node, "at")
    layer = first(node, "layer")
    if at is None or layer is None or len(at) < 3:
        return None
    rot = float(at[3]) if len(at) > 3 else 0.0
    return {"text": node[1], "x_mm": float(at[1]), "y_mm": float(at[2]),
            "rot_deg": rot, "layer": layer[1]}


def footprint_summary(fp: list) -> dict:
    lib_id = fp[1]
    x, y, rot = parse_at(fp)
    ref = "~"
    props = children(fp, "property")
    for p in props:
        if p[1] == "Reference" and len(p) > 2:
            ref = p[2]
            break
    return {"lib_id": lib_id, "reference": ref, "at_mm": [x, y], "rot_deg": rot}


def extract(pcb_path: Path) -> dict:
    text = pcb_path.read_text()
    root = parse_sexp(text)

    pads_out: list[dict] = []
    graphics: dict[str, list] = {}
    board_graphics: dict[str, list] = {}
    padring_summary = None
    other_footprints = []

    for fp in _find_all(root, "footprint"):
        summary = footprint_summary(fp)
        if PADRING_LIB_ID in summary["lib_id"]:
            padring_summary = summary
            fx, fy, frot = summary["at_mm"] + [summary["rot_deg"]]
            for pad in children(fp, "pad"):
                p = parse_pad(pad)
                # global position: rotate local offset by footprint rotation,
                # then translate (KiCad rot is CCW in the y-down screen frame)
                r = math.radians(frot)
                p["gx_mm"] = fx + p["x_mm"] * math.cos(r) - p["y_mm"] * math.sin(r)
                p["gy_mm"] = fy + p["x_mm"] * math.sin(r) + p["y_mm"] * math.cos(r)
                pads_out.append(p)
            for g_node in _find_all(fp, "fp_line") + _find_all(fp, "fp_rect") + \
                    _find_all(fp, "fp_poly") + _find_all(fp, "fp_circle") + _find_all(fp, "fp_arc"):
                g = parse_graphic(g_node)
                if g:
                    graphics.setdefault(g["layer"], []).append(g)
        else:
            # Front-side furniture of the other footprints (pin-1 silk
            # marker; everything else on this board is back-side), in
            # global coordinates.
            fx, fy, frot = summary["at_mm"] + [summary["rot_deg"]]
            r = math.radians(frot)
            for g_node in _find_all(fp, "fp_line") + _find_all(fp, "fp_rect") + \
                    _find_all(fp, "fp_poly") + _find_all(fp, "fp_circle") + _find_all(fp, "fp_arc"):
                g = parse_graphic(g_node)
                if not g or not (g["layer"] or "").startswith("F."):
                    continue
                for key in ("start", "end", "center", "mid"):
                    if key in g:
                        lx, ly = g[key]
                        g[key] = [fx + lx * math.cos(r) - ly * math.sin(r),
                                  fy + lx * math.sin(r) + ly * math.cos(r)]
                if "pts" in g:
                    g["pts"] = [[fx + lx * math.cos(r) - ly * math.sin(r),
                                 fy + lx * math.sin(r) + ly * math.cos(r)]
                                for lx, ly in g["pts"]]
                board_graphics.setdefault(g["layer"], []).append(g)
            other_footprints.append(summary)

    edge_cuts = []
    for g_node in _find_all(root, "gr_line") + _find_all(root, "gr_rect") + \
            _find_all(root, "gr_poly") + _find_all(root, "gr_circle") + _find_all(root, "gr_arc"):
        g = parse_graphic(g_node)
        if not g:
            continue
        if g["layer"] == "Edge.Cuts":
            edge_cuts.append(g)
        elif g["layer"]:  # silk boxes, Eco annotations — global frame
            board_graphics.setdefault(g["layer"], []).append(g)

    # Copper: front + back tracks (callers pick a layer), through vias,
    # zone pours as filled polygons, board-level texts.
    segments = []
    for s in _find_all(root, "segment"):
        layer, start, end = first(s, "layer"), first(s, "start"), first(s, "end")
        if not (layer and start and end):
            continue
        seg = {"layer": layer[1], "start": list(_xy(start)), "end": list(_xy(end))}
        width = first(s, "width")
        if width:
            seg["width"] = float(width[1])
        net = first(s, "net")
        if net and isinstance(net[-1], str):
            seg["net"] = net[-1]
        segments.append(seg)

    vias = []
    for v in _find_all(root, "via"):
        at, size = first(v, "at"), first(v, "size")
        if not (at and size):
            continue
        layers = first(v, "layers")
        drill = first(v, "drill")
        vias.append({"x_mm": float(at[1]), "y_mm": float(at[2]),
                     "size_mm": float(size[1]),
                     "drill_mm": float(drill[1]) if drill else None,
                     "layers": list(layers[1:]) if layers else []})

    zone_polygons = []
    for z in _find_all(root, "zone"):
        net_name = first(z, "net_name")
        net = net_name[1] if net_name else ""
        for fp_node in _find_all(z, "filled_polygon"):
            layer, pts_node = first(fp_node, "layer"), first(fp_node, "pts")
            if not (layer and pts_node):
                continue
            zone_polygons.append({"layer": layer[1], "net": net,
                                  "pts": _xy_list(pts_node)})

    texts = [t for t in (parse_text(n) for n in _find_all(root, "gr_text")) if t]

    return {
        "source": str(pcb_path),
        "padring": padring_summary,
        "pads": pads_out,
        "graphics": graphics,
        "edge_cuts": edge_cuts,
        "other_footprints": other_footprints,
        "segments": segments,
        "vias": vias,
        "zone_polygons": zone_polygons,
        "texts": texts,
        "board_graphics": board_graphics,
    }


def cavity_mm(data: dict) -> tuple[float, float] | None:
    """Smallest axis-aligned F.Mask opening rect, (w, h) mm — the die site
    the placement guard fits die sizes against."""
    cavity = None
    for s in data["graphics"].get("F.Mask", []):
        if s["type"] == "rect" and "start" in s:
            w = abs(s["end"][0] - s["start"][0])
            h = abs(s["end"][1] - s["start"][1])
            if cavity is None or w * h < cavity[0] * cavity[1]:
                cavity = (w, h)
    return cavity


def apply_board_facts(data: dict, board_id: str, entry: dict,
                      meta: dict) -> None:
    """Stamp boards.json facts onto the parse and assert they still hold.

    ring_count and die_site_mm are pinned expectations, not inputs: the
    kicad file is the source of truth for geometry, so a mismatch means
    the board drifted from what the knowledgebase (and prior diagrams)
    were built against — fail loudly instead of rendering a wrong page.
    """
    extras = sorted(entry["extra_nums"], key=int)
    ring = [p for p in data["pads"] if p["num"] not in set(extras)]
    if len(ring) != entry["ring_count"]:
        raise SystemExit(
            f"board {board_id}: ring has {len(ring)} bond pads, boards.json "
            f"pins {entry['ring_count']} — the kicad file drifted from the "
            f"knowledgebase")
    cavity = cavity_mm(data)
    if cavity:
        dw, dh = entry["die_site_mm"]
        if abs(cavity[0] - dw) > 0.05 or abs(cavity[1] - dh) > 0.05:
            raise SystemExit(
                f"board {board_id}: die site is {cavity[0]:.2f}x"
                f"{cavity[1]:.2f} mm, boards.json pins {dw}x{dh} — "
                f"the kicad file drifted from the knowledgebase")
    data["board"] = board_id
    data["rev"] = meta["rev"]
    data["dirty"] = meta["dirty"]
    data["extra_nums"] = extras
    data["qr_alignment"] = entry["qr_alignment"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--board", help="board id in boards.json (fetches the "
                                    "pinned kicad_pcb, asserts ring/die-site)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="render from a locally-dirty board file anyway "
                         "(outputs flagged untraced)")
    ap.add_argument("--pcb", type=Path, default=DEFAULT_PCB,
                    help="manual mode: kicad_pcb path (ignores boards.json)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    if args.board:
        board_id, entry = resolve_board(load_boards(), board=args.board)
        args.pcb, meta = fetch_pcb(entry, allow_dirty=args.allow_dirty)
        if args.out == DEFAULT_OUT:
            args.out = REPO / "tmp" / "cob" / f"{board_id}.json"

    data = extract(args.pcb)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    if args.board:
        apply_board_facts(data, board_id, entry, meta)

    args.out.write_text(json.dumps(data, indent=1))

    extras = set(data.get("extra_nums") or
                 {"75", "76", "77", "78", "79", "80", "81"})
    ring = [p for p in data["pads"] if p["num"] not in extras]
    nets: dict[str, int] = {}
    for p in ring:
        nets[p.get("pinfunction", "?").rsplit("_", 1)[0]] = nets.get(p.get("pinfunction", "?").rsplit("_", 1)[0], 0) + 1
    print(f"padring: {data['padring']}")
    print(f"bond pads: {len(ring)}, extra pads: {len(data['pads']) - len(ring)}")
    print(f"pinfunction classes: {nets}")
    print(f"graphic layers: { {k: len(v) for k, v in data['graphics'].items()} }")
    print(f"edge_cuts shapes: {len(data['edge_cuts'])}")
    print(f"other footprints: {len(data['other_footprints'])}")
    if args.board:
        trace = f"rev {meta['rev'][:10]}"
        if meta["dirty"]:
            trace += " (DIRTY — untraced)"
        print(f"board {board_id}: {trace}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
