"""Generate bonding-diagrams/<reticle>/index.md — the per-design table per reticle.

Reads tmp/<reticle>/pads.json (every extracted reticle) and tmp/cob/*.json
(board data) and maps each design to its board with the same guards as
make_bonding_diagrams: pad count must equal the board's bond-ring count
and the die must fit the board's die site (smallest F.Mask rect). Designs
matching no board are custom pad rings — no COB breakout exists for them.

Usage:
    uv run make_index.py
"""

from __future__ import annotations

import json
from pathlib import Path

from boards import load_boards

REPO = Path(__file__).resolve().parent
COB_DIR = REPO / "tmp" / "cob"
OUT_DIR = REPO / "bonding-diagrams"


def die_site(cob: dict) -> tuple[float, float] | None:
    """The die site (mm) — parse stamps the asserted boards.json value;
    fall back to the smallest F.Mask rect for un-stamped COB JSONs."""
    if cob.get("die_site_mm"):
        return (cob["die_site_mm"][0], cob["die_site_mm"][1])
    cavity = None
    for s in cob["graphics"].get("F.Mask", []):
        if s["type"] == "rect" and "start" in s:
            w = abs(s["end"][0] - s["start"][0])
            h = abs(s["end"][1] - s["start"][1])
            if cavity is None or w * h < cavity[0] * cavity[1]:
                cavity = (w, h)
    return cavity


def matching_board(design: dict, cobs: list[dict],
                   boards_kb: dict) -> dict | None:
    """The board this design renders on, or None if no PCB exists.

    Precedence mirrors boards.resolve_board: a die named in a board's
    "designs" claims it outright; everything else is matched
    geometrically (pad count + die site) among the boards serving a slot
    — a design-specific board (e.g. MOSB's round COB) never takes an
    unclaimed die.
    """
    claims = [bid for bid, e in boards_kb.items()
              if design["name"] in e.get("designs", ())]
    if claims:
        for cob in cobs:
            if cob["board"] == claims[0]:
                return cob
        raise SystemExit(f"{design['name']}: claimed by board {claims[0]} "
                         f"but tmp/cob/{claims[0]}.json is missing")
    slot_boards = {bid for bid, e in boards_kb.items() if e.get("slot")}
    dw, dh = design["die_w_um"] / 1000.0, design["die_h_um"] / 1000.0
    matches = []
    for cob in cobs:
        if cob["board"] not in slot_boards:
            continue
        extras = set(str(n) for n in cob.get("extra_nums", []))
        ring = [p for p in cob["pads"] if str(p["num"]) not in extras]
        if len(design["pads"]) != len(ring):
            continue
        cavity = die_site(cob)
        if cavity and (dw > cavity[0] + 0.2 or dh > cavity[1] + 0.2):
            continue
        matches.append(cob)
    if len(matches) > 1:
        raise SystemExit(f"{design['name']}: ambiguous — matches "
                         f"{[c['board'] for c in matches]}")
    return matches[0] if matches else None


def main() -> None:
    cobs = [json.loads(p.read_text()) for p in sorted(COB_DIR.glob("*.json"))]
    revs = {c["board"]: c.get("rev", "?")[:7] for c in cobs}
    boards_kb = load_boards()

    pads_files = sorted((REPO / "tmp").glob("*/pads.json"))
    if not pads_files:
        raise SystemExit("no tmp/<reticle>/pads.json — run extract_dies.py first")

    for pads_path in pads_files:
        designs = json.loads(pads_path.read_text())
        rows = []
        for d in designs:
            cob = matching_board(d, cobs, boards_kb)
            reticle = d.get("reticle", pads_path.parent.name)
            labelled = sum(1 for p in d["pads"] if p["net"])
            pdf = f"{d['name']}_{d['slot_size']}.pdf"
            rendered = bool(cob) and (OUT_DIR / reticle / pdf).exists()
            if cob and not rendered:
                pdf = "— (board matches, not rendered — see pipeline SKIP)"
            elif not cob:
                pdf = "no PCB (custom pad ring)"
            rows.append({
                "name": d["name"], "slot": d["slot_size"], "reticle": reticle,
                "die": f"{d['die_w_um']:.0f}×{d['die_h_um']:.0f}",
                "pads": len(d["pads"]), "labelled": labelled,
                "board": cob["board"] if cob else "—",
                "rendered": rendered,
                "pdf": pdf,
            })
        write_index(reticle, rows, revs)


def write_index(reticle: str, rows: list[dict], revs: dict[str, str]) -> None:
    by_board: dict[str, list[dict]] = {}
    for r in rows:
        by_board.setdefault(r["board"], []).append(r)

    lines = [
        f"# Bonding diagrams — {reticle}",
        "",
        "One 3-page PDF per design: die pinout, placement, bonding.",
        "Boards pinned at chip-on-board-wire-bonded-pcbs "
        f"`{next(iter(revs.values()))}`.",
        "",
    ]
    for board in sorted(by_board, key=lambda b: (b == "—", b)):
        group = sorted(by_board[board], key=lambda r: r["name"])
        if board == "—":
            lines += [f"## No PCB — custom pad ring ({len(group)} designs)",
                      "",
                      "Pad count matches no COB board; these one-off pad "
                      "rings most likely have no breakout PCB.",
                      ""]
        else:
            lines += [f"## Board `{board}` ({len(group)} designs)", ""]
        lines += [
            "| design | slot | die (µm) | pads | labelled | PDF |",
            "|---|---|---|---|---|---|",
        ]
        for r in group:
            lines.append(f"| {r['name']} | {r['slot']} | {r['die']} | "
                         f"{r['pads']} | {r['labelled']} | {r['pdf']} |")
        lines.append("")

    out = OUT_DIR / reticle / "index.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    rendered = sum(1 for r in rows if r["rendered"])
    print(f"wrote {out} — {rendered} rendered, "
          f"{len(rows) - rendered} no-PCB/unrendered")


if __name__ == "__main__":
    main()
