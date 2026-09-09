# bonding_diagrams

Multi-page bonding diagrams for the factory, one per chip design on the
wafer.space `ws-run1` reticle.

Each design gets a multi-page PDF (plus per-page PNG/SVG):

1. **Die pinout** — GDS-rendered die with every IO pad labeled by net,
   same style as `wafer-space-die-pad-diagrams`.
2. **Die placement** — the COB breakout PCB rendered with the die in its
   cavity, in the orientation it is actually bonded (face-up, rotated
   180° relative to the GDS frame, QR toward the rocket fiducial).
3. **Bonding diagram** — both together, one bond wire per pad from die
   pad to PCB pad, colored by net class (GND / power / signal).

## Data sources

This repo expects to sit alongside the two input repos:

```
parent-dir/
├── ws-run1/                        ← die layout (OASIS)
│   ├── layout/reticle.oas
│   └── lyp/gf180mcu.lyp
├── chip-on-board-wire-bonded-pcbs/ ← breakout PCBs (KiCad)
│   ├── run-1/1x1-cob/
│   ├── run-1/0p5x1-cob/  ·  run-1/1x0p5-cob/
│   └── run-2/0p5x0p5-cob/
└── bonding-diagrams/               ← this repo
```

- **Die geometry, pads, net labels** — `reticle.oas`, read with klayout
  and rendered in the two-tone wafer.space style (yellow Metal5, dark-red
  Pad), reusing the extraction core of `wafer-space-die-pad-diagrams`.
- **Bond pad positions, die cavity, board outline** — the `.kicad_pcb` of
  the COB variant matching the die's slot size.
- **Pad correspondence** — `pcb_pad = die_pad + 1`. Both rings number
  counter-clockwise from the top-right corner, and the die's 180°
  placement rotation cancels the diagrams' 180° display rotation. The
  full chain, worked example, and rotation warnings live in
  `wafer-space-die-pad-diagrams/PAD_MAPPING.md` — read it before
  debugging any off-by-one or flipped wire.

## Status

Planning stage — see [PLAN.md](PLAN.md). Nothing generated yet.
