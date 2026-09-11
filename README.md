# bonding-diagrams

Generates chip-on-board (COB) wire-bonding diagram packets for every
design on the wafer.space run-1 reticle: one 3-page A4 PDF per design —

1. **die pinout** — the design's pad diagram (page-1 pinout PDF, scaled
   to A4; the sibling repo's committed run-1 PDFs, or generated per
   reticle via `make_pinouts.py`),
2. **placement** — the COB breakout board with the die render seated in
   its cavity, PCB pad numbers, die-site scale bar, and a zoomed inset of
   the die's QR cell against the board's alignment fiducial (rocket logo
   or copper circle, per board),
3. **bonding** — placement page plus one wire per bond finger, drawn
   true to scale (25 µm), with per-wire length/angle validation.

Rendered output lands in `bonding-diagrams/<reticle>/<design>_<slot>.pdf`
(plus `bonding-diagrams/<reticle>/index.md`), where `<reticle>` is the checkout
name passed to `extract_dies.py --reticle` (default `ws-run1`).

## Credits

This repo stands on three sibling wafer.space repos (all Apache-2.0;
see [LICENSE](LICENSE) — `make_diagrams.py` is vendored under that
license with its modifications marked in the file header):

- **[wafer-space-die-pad-diagrams]** — the die pad extraction and pinout
  diagram pipeline. `make_diagrams.py` is **vendored verbatim** from it
  (the only delta here is the `max_px`
  parameter on `render_gds_background`, see
  `extract_dies.DIE_RENDER_MAX_PX`). Page 1 embeds that repo's committed
  run-1 pinout PDFs (`diagrams/`); other reticles get theirs generated
  with the same vendored code (`make_pinouts.py`). Die↔PCB coordinate
  conventions follow its `PAD_MAPPING.md`.
- **[ws-run1]** — the reticle layout (`layout/reticle.oas`); source of
  all die geometry, net labels, and the two-tone die renders.
- **[chip-on-board-wire-bonded-pcbs]** — the KiCad COB breakout boards.
  Not checked out by hand: `boards.json` pins each board to a git URL +
  commit, and `parse_pcb.py`/`boards.py` retrieve just the single
  `.kicad_pcb` file (KiCad ≥6 embeds footprints, so one file suffices)
  via a blob-filtered partial clone cached in `tmp/repos/`.

[wafer-space-die-pad-diagrams]: https://github.com/wafer-space/wafer-space-die-pad-diagrams
[ws-run1]: https://github.com/wafer-space/ws-run1
[chip-on-board-wire-bonded-pcbs]: https://github.com/wafer-space/chip-on-board-wire-bonded-pcbs

## Required repo layout

Two sibling checkouts must sit next to this repo (paths are hard-coded
relative to it):

```
<workspace>/
├── bonding-diagrams/              this repo
├── ws-run1/                       reticle layout — needs layout/reticle.oas
└── wafer-space-die-pad-diagrams/  optional — falls back to its
                                   diagrams/*.pdf when a pinout isn't
                                   generated locally (see make_pinouts.py)
```

The KiCad board repo is **not** a required sibling — it is fetched
automatically at the pinned revision (see above). Alternatively a
board's `"git"` field in `boards.json` may name a local directory, in
which case HEAD must match the pinned `"rev"` and the board file must be
clean (or pass `--allow-dirty`).

## Pipeline

Four stages, each with a file intermediate so they re-run independently:

| stage | reads | writes |
|---|---|---|
| `extract_dies.py` | `<reticle>/layout/reticle.oas` (via vendored `make_diagrams.py`; `--reticle` picks the checkout) | `tmp/<reticle>/pads.json`, `tmp/<reticle>/gds_renders/<cell>.png` |
| `make_pinouts.py --reticle <r>` | `tmp/<reticle>/pads.json` + cached GDS renders | `tmp/<reticle>/pinouts/<name>_<slot>.pdf` (page 1) |
| `parse_pcb.py --board <id>` | `boards.json` + the pinned `.kicad_pcb` | `tmp/cob/<id>.json` |
| `verify_mapping.py` | `tmp/<reticle>/pads.json` + `tmp/cob/<id>.json` | geometry check only (fails loudly) |
| `make_bonding_diagrams.py --board <id>` | the above | `bonding-diagrams/<reticle>/<name>_<slot>.pdf` (+ page previews in `tmp/<reticle>/pages/`) |
| `make_index.py` | the above | `bonding-diagrams/<reticle>/index.md` |

`boards.json` is the knowledgebase of per-board facts that can't be
derived each run: git URL + pinned rev, PCB path, slot/die designs
served, bond-ring pad count, mechanical extra pads (paddle, mounting
holes), QR-alignment fiducial kind, and die-site size. `parse_pcb.py`
asserts ring count and die site against the parsed geometry, so a
drifted board file fails loudly; pad-count and cavity guards in
`make_bonding_diagrams.py` route each die to its only compatible board.

Third-party boards (any entry whose `git` isn't the wafer-space board
repo) carry a few extra pins:

- `padring_fp` — the padring footprint's exact lib id, since the
  `*padring*` name heuristic doesn't apply.
- `edge_cuts_layer` — outline fallback when the file draws the board
  shape on a user layer instead of Edge.Cuts.
- `ring_map` — a bond-map correction for rings whose numbering doesn't
  follow `pcb_pad = die_pad + 1`: `{"offset": N}` bonds pcb pad `i` to
  die pad `(i-1+N) mod ring`. Both MOSB boards (round and rectangular)
  use offset 17 — confirmed against the author's Cmts.User wire guides
  (median endpoint error ~0.15 mm, zero wire crossings).
- `wire_max_mm` / `wire_max_angle_deg` — per-board wire-bond limits
  stamped onto the parse; verify_mapping checks against them instead of
  the rectangular-board defaults (1–3 mm, 45°).

## Pad correspondence

`pcb_pad = die_pad + 1`: both rings number counter-clockwise from the
top-right corner, and the die's 180° placement rotation cancels the
pinout diagrams' 180° display rotation. The full chain, worked example,
and rotation warnings live in
`wafer-space-die-pad-diagrams/PAD_MAPPING.md` — read it before debugging
any off-by-one or flipped wire. `verify_mapping.py` re-checks the
mapping geometrically per board (pad counts, per-pad distances, wire
angles) and fails loudly on mismatch. Boards with a pinned `ring_map`
(see above) deviate from the `+1` convention on purpose — their pairing
is `ring_map`-ordered.

## How to run

Requires [uv] (Python ≥3.12; deps in `pyproject.toml`: klayout,
matplotlib, pypdf, segno), `git` with access to the board repo, and
optionally `pdftoppm` (poppler) for the page-1 preview PNGs.

```sh
uv sync

# 1. Die data + two-tone renders for every design on the reticle.
#    Slow the first time (KLayout render, ~8000 px/die); cached in tmp/.
#    --reticle names a sibling checkout dir (or a path) holding
#    layout/reticle.oas — rerun with ws-run2 when that reticle lands.
uv run extract_dies.py --all
uv run extract_dies.py --all --reticle ws-run2   # when available

# 1b. Page-1 pinout PDFs. The sibling repo commits these only for run-1;
#     other reticles need them generated (also regenerates any design the
#     sibling is missing). Cached per design; the renderer falls back to
#     the sibling's diagrams/*.pdf automatically.
uv run make_pinouts.py --reticle ws-run2

# 2. One COB JSON per board — fetches the pinned .kicad_pcb on first use
#    (~2.5 MB partial clone into tmp/repos/), then reuses the cache.
uv run parse_pcb.py --board 1x1
uv run parse_pcb.py --board 1x0p5
uv run parse_pcb.py --board 0p5x1
uv run parse_pcb.py --board tqva

# 3. Optional: verify die↔PCB pad mapping geometry for a board.
uv run verify_mapping.py --cob tmp/cob/1x1.json

# 4. Render. Smoke subset by default; --designs takes cell names from
#    tmp/<reticle>/pads.json (the single extracted reticle; pass --pads
#    when several exist). Wrong-board dies are SKIPped with the reason.
uv run make_bonding_diagrams.py --board 1x1 --designs GD03_chip_top_6_6
uv run make_bonding_diagrams.py --board tqva --designs TQVA_chip_top_14_8

# 5. Regenerate the index table.
uv run make_index.py
```

Designs whose pad count matches no board are custom one-off pad rings
with no COB breakout; they are listed under "No PCB" in
`bonding-diagrams/<reticle>/index.md` and skipped by the renderer.

[uv]: https://docs.astral.sh/uv/
