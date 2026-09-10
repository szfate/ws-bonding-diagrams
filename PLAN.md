# Plan

## Output

`bonding/<NAME>.pdf` per top-level design in `reticle.oas` — three pages
(die pinout, placement, bonding) — plus an `index.md` with slot size and
pad count per design. A smoketest script renders a curated subset first,
mirroring `make_diagrams_smoketest.py`.

## Pipeline

Four stages, each a small module with a file intermediate so stages can
be re-run independently:

1. **`extract_dies.py`** — OAS → die data + background render.
   Vendored `make_diagrams.py` from the sibling repo (it can no longer
   take changes; the vendored copy's only delta is the `max_px`
   parameter on `render_gds_background`): pad extraction
   (layer 37/0, ≥30 µm edge filter), net labels from `Metal5_Label` /
   `MetalTop_Label`, 180° display rotation, two-tone GDS render.
   Emits `tmp/pads.json` — per design: pads[] (`n`, x/y µm, net, edge),
   die bbox, render PNG.

2. **`parse_pcb.py`** — `.kicad_pcb` → COB data.
   S-expression parse in plain Python (no KiCad install required):
   padring footprint pads (number, x/y mm, rotation), die cavity /
   placement outline, board edge. One JSON per slot variant under
   `tmp/cob/`.

3. **`mapping.py`** — align the two pad rings.
   Transform die pads into PCB frame (cavity center + 180° rotation),
   then pair them with PCB pads in the same CCW order. Checks per
   variant before any drawing: pad counts equal, per-pad distances
   within the wirebond rules from `run-1/wirebonding/README.md`
   (1–3 mm, ≤45° from pad normal, no crossings — the pin-for-pin walk
   gives that by construction). Fail loudly on mismatch; a wrong
   mapping prints as a wrong factory diagram.

4. **`make_bonding_diagrams.py`** — render the pages.
   matplotlib `PdfPages`, one mm-based coordinate frame shared by pages
   2–3 (die µm → mm).
   - **Page 1** die pinout: embed the existing per-design PDF from
     `wafer-space-die-pad-diagrams/diagrams/` via `pypdf`, scaled onto
     A4 so all three pages share one paper size.
   - **Page 2** placement: PCB drawn from `parse_pcb.py` output — copper
     pads, cavity, silkscreen, mezzanine connector outline — with the
     die render placed in the cavity at the mapped origin/rotation.
     Annotate PCB pad numbers 1..74.
   - **Page 3** bonding: page 2 + one wire per pad, drawn from the die
     pad edge to the PCB pad as a straight fan line (arcs looked busier
     at 74 wires; revisit if the bonder wants them); color by die net
     class (page-1 palette); length range in the title.

## Slot variant → COB map

| die slot | COB source | bond pads |
|---|---|---|
| 1×1 | `run-1/1x1-cob/1x1-mezzanine.kicad_pcb` | 74 (documented) |
| 0.5×1 | `run-1/0p5x1-cob/mezzanine-0p5x1.kicad_pcb` | verify |
| 1×0.5 | `run-1/1x0p5-cob/mezzanine-1x0p5.kicad_pcb` | verify |
| 0.5×0.5 | `run-2/0p5x0p5-cob/0p5x0p5-cob.kicad_pcb` | verify |

First task under step 2: count padring pads in each variant. The 74-pad
convention is documented for 1×1 only; half/quarter slots are expected
to have proportionally fewer.

## Order of work

1. [x] Parse the 1×1 `.kicad_pcb`, dump the padring to JSON, plot it raw to
   eyeball orientation (remember: KiCad y-down). — `parse_pcb.py` +
   `plot_pcb.py`; padring graphics turn out to include the die courtyard
   (Dwgs.User ticks) and per-pad wire guides (Cmts.User).
2. [x] Wire up die extraction; verify `pcb_pad = die_pad + 1` by geometry
   against the PAD_MAPPING.md worked example (die pad 0 → PCB pad 1 at
   +3.20, −4.70 mm footprint-local). — `extract_dies.py` (imports the
   sibling pipeline) + `verify_mapping.py`: worked example matches to
   0.00 µm; 1×1 wires 2.20–2.99 mm, ≤38.7°, zero crossings.
3. [x] Page set for one clean 1×1 design (WSLG — big pads, sparse interior).
   — `make_bonding_diagrams.py`: 3-page PDF (pinout embedded as is,
   placement, bonding) + tmp/pages previews; wires 2.20–2.99 mm fan.
4. Remaining slot variants, then all ~40 designs + index.

## Risks / open questions

- **No run-1 0.5×0.5 COB.** TQVA (0.5×0.5, run 1) would bond to the
  run-2 board — confirm its padring matches the run-1 padframe before
  trusting the mapping, or exclude TQVA with a note.
- **Mapping JSONs are gone.** The `tmp/*.json` files referenced by
  PAD_MAPPING.md were gitignored and their generator scripts
  (`extract_pads_remote.py`, `parse_cob_padring.py`) were never
  committed — steps 1–2 re-derive them from source.
- **KiCad y-down** — the one systematic trap; every mm coordinate from
  the PCB gets negated on y before plotting (see PAD_MAPPING.md §4).
- **Wire visual is illustrative, not DRC** — arcs drawn to look right;
  actual wire length/angle validation happens in `mapping.py`, die
  thickness and bond-finger geometry are not modeled.
- **Die placement origin** — cavity center assumed; if the PCB fab
  layers carry an explicit die-placement courtyard, prefer that.
