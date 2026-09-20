"""Run/die orientation + bonding spec config (runs.json).

Three layers merged shallowly (whole sub-dict wins), most specific last:
    defaults -> runs[<reticle>] -> dies[<design>]

Rotation semantics (die or view relative to a reference frame):
  page1_die_rotation_deg — how page 1 draws the die relative to GDS;
    also the frame of tmp/<reticle>/pads.json (180 for run-1, 0 for
    ws-run2).
  view_rotation_deg — pages 2-3 drawing rotation about the padring
    origin (ws-run2: 180 → pin-1 bottom-left). The die's drawn
    orientation is placement ⊕ view; the physical placement rotation
    lives on the parsed COB JSON (parse_pcb default 180) and pages 2-3
    draw the die in the page-1 frame for all current configs.

Spec metadata (die_thickness_um, bond.*) is stored but not yet rendered
on the diagrams.

Usage:
    from runs import run_config
    cfg = run_config("ws-run2", "WSLG_chip_top_10_2")
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent
RUNS_JSON = REPO / "runs.json"

ROT_KEYS = ("page1_die_rotation_deg", "view_rotation_deg")
TOP_KEYS = (*ROT_KEYS, "die_thickness_um", "bond")
BOND_KEYS = ("wire_diameter_um", "wire_material", "process")


def _validate(section: dict, where: str) -> None:
    bad = [k for k in section if k not in TOP_KEYS]
    if bad:
        raise SystemExit(f"{where}: unknown keys {bad} — allowed: {TOP_KEYS}")
    if "bond" in section:
        bad = [k for k in section["bond"] if k not in BOND_KEYS]
        if bad:
            raise SystemExit(f"{where}: unknown bond keys {bad} — "
                             f"allowed: {BOND_KEYS}")


def load_runs(path: Path = RUNS_JSON) -> dict:
    """Load and validate runs.json (structure only; values unvalidated)."""
    data = json.loads(path.read_text())
    _validate(data.get("defaults", {}), "defaults")
    for run, entry in data.get("runs", {}).items():
        _validate(entry, f"runs[{run!r}]")
    for die, entry in data.get("dies", {}).items():
        _validate(entry, f"dies[{die!r}]")
    return data


def run_config(run: str, design: str | None = None,
               path: Path = RUNS_JSON) -> dict:
    """Merged defaults -> runs[run] -> dies[design] config dict."""
    data = load_runs(path)
    cfg = dict(data.get("defaults", {}))
    cfg.update(data.get("runs", {}).get(run, {}))
    if design is not None:
        cfg.update(data.get("dies", {}).get(design, {}))
    return cfg
