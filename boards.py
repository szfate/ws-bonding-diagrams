"""Board knowledgebase: which kicad_pcb (at which commit) renders which dies.

boards.json is the single source of truth for board facts the diagrams
need and cannot safely derive each run: the source repo + pinned git rev,
the bond-ring size, the mechanical (non-bond) pad numbers, and the
QR-alignment convention. Geometry that can be re-derived from the kicad
file (pad positions, cavity shape) is *not* stored here — instead the
pinned expectations (ring_count, die_site_mm) are asserted against the
parsed result so a drifted board file fails loudly at parse time.

A board serves either a whole die slot ("slot") or specific dies
("designs"); die-specific boards win over slot boards at resolve time,
and two boards claiming the same die or slot is a hard error.

Retrieval is per-file: a repo is cloned once into tmp/repos/ with
--filter=blob:none (history and trees, no file contents), and each
board's .kicad_pcb is checked out at its pinned rev on demand — the
.kicad_pcb format embeds its footprints, so one file is all a render
needs. A "git" field naming an existing local directory is used in
place of a clone, with HEAD required to match the pinned rev and the
board file required to be clean (override: --allow-dirty, which flags
the render outputs as untraceable).

Usage:
    from boards import load_boards, resolve_board, fetch_pcb
    boards = load_boards()
    board_id, entry = resolve_board(boards, slot="0.5x0.5")
    pcb_path, meta = fetch_pcb(entry)
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent
BOARDS_JSON = REPO / "boards.json"
REPO_CACHE = REPO / "tmp" / "repos"

REQUIRED_FIELDS = ("git", "rev", "pcb", "ring_count", "extra_nums",
                   "qr_alignment", "die_site_mm")


def find_pads(explicit: Path | None = None) -> Path:
    """Resolve the pads file: tmp/<reticle>/pads.json for the single
    extracted reticle, or an explicit --pads path.

    Extraction writes per-reticle intermediates so die names can never
    clash across reticles; with several extracted at once the caller
    must disambiguate.
    """
    if explicit is not None:
        return explicit
    candidates = sorted((REPO / "tmp").glob("*/pads.json"))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise SystemExit("no tmp/<reticle>/pads.json — run extract_dies.py first")
    raise SystemExit("multiple reticles extracted: "
                     + ", ".join(c.parent.name for c in candidates)
                     + " — pass --pads tmp/<reticle>/pads.json")


def load_boards(path: Path = BOARDS_JSON) -> dict:
    """Load and validate boards.json."""
    data = json.loads(path.read_text())
    boards = data.get("boards")
    if not isinstance(boards, dict) or not boards:
        raise SystemExit(f"{path}: no \"boards\" object")
    for board_id, entry in boards.items():
        missing = [f for f in REQUIRED_FIELDS if f not in entry]
        if missing:
            raise SystemExit(f"{path}: board {board_id!r} missing {missing}")
        if "slot" not in entry and not entry.get("designs"):
            raise SystemExit(f"{path}: board {board_id!r} serves nothing — "
                             f"set \"slot\" and/or \"designs\"")
        bad_qr = entry["qr_alignment"] not in ("rocket", "copper-circle")
        if bad_qr:
            raise SystemExit(f"{path}: board {board_id!r}: qr_alignment must "
                             f"be \"rocket\" or \"copper-circle\"")
    return boards


def resolve_board(boards: dict, *, board: str | None = None,
                  design: str | None = None, slot: str | None = None
                  ) -> tuple[str, dict]:
    """Pick the board entry for a board id, a die, or a die slot.

    Precedence: explicit board id > die name in entry "designs" > slot
    match. Ambiguity at any level is a hard error — a wrong guess here
    prints as a wrong factory diagram.
    """
    if board is not None:
        if board not in boards:
            raise SystemExit(f"unknown board {board!r} — "
                             f"known: {sorted(boards)}")
        return board, boards[board]

    if design is not None:
        claims = [bid for bid, e in boards.items()
                  if design in e.get("designs", ())]
        if len(claims) > 1:
            raise SystemExit(f"die {design!r} claimed by boards {claims} — "
                             f"disambiguate boards.json")
        if claims:
            return claims[0], boards[claims[0]]

    if slot is not None:
        claims = [bid for bid, e in boards.items() if e.get("slot") == slot]
        if len(claims) > 1:
            raise SystemExit(f"slot {slot!r} served by boards {claims} — "
                             f"give one of them an explicit \"designs\" list")
        if claims:
            return claims[0], boards[claims[0]]

    raise SystemExit(f"no board for design={design!r} slot={slot!r} — "
                     f"add one to boards.json")


def _git(args: list[str], cwd: Path | None = None) -> str:
    """Run git, returning stdout; SystemExit with stderr on failure."""
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True)
    if proc.returncode != 0:
        where = f" in {cwd}" if cwd else ""
        raise SystemExit(f"git {' '.join(args)}{where} failed: "
                         f"{proc.stderr.strip()}")
    return proc.stdout.strip()


def _slug(url: str) -> str:
    name = Path(url.rstrip("/")).name
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name.removesuffix(".git"))


def fetch_pcb(entry: dict, *, allow_dirty: bool = False
              ) -> tuple[Path, dict]:
    """Materialize entry["pcb"] at entry["rev"]; return (path, meta).

    Local-directory "git": use the checkout in place after verifying
    HEAD == rev and the board file is clean. URL "git": per-repo
    blob:none clone cache in tmp/repos/, path-limited checkout at rev.

    meta carries {"rev", "dirty"} for the render outputs' traceability.
    """
    src = entry["git"]
    rev = entry["rev"]
    meta = {"rev": rev, "dirty": False}

    if Path(src).expanduser().is_dir():
        repo = Path(src).expanduser()
        head = _git(["rev-parse", "HEAD"], cwd=repo)
        if head != rev:
            raise SystemExit(
                f"{repo} is at {head[:10]}, boards.json pins {rev[:10]} — "
                f"checkout the pinned rev (or bump boards.json)")
        dirty = _git(["status", "--porcelain", "--", entry["pcb"]],
                     cwd=repo)
        if dirty:
            if not allow_dirty:
                raise SystemExit(
                    f"{entry['pcb']} has uncommitted changes in {repo} — "
                    f"the render would not match the pinned rev; commit "
                    f"(or pass --allow-dirty to render untraced)")
            meta["dirty"] = True
            print(f"WARN: {entry['pcb']} dirty in local checkout — "
                  f"render is untraced against rev {rev[:10]}")
        return repo / entry["pcb"], meta

    cache = REPO_CACHE / _slug(src)
    if not (cache / ".git").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        print(f"cloning {src} (blob:none) into {cache}")
        _git(["clone", "--filter=blob:none", "--no-checkout", src,
              str(cache)])
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", rev],
                      cwd=cache).returncode != 0:
        print(f"fetching pinned rev {rev[:10]} from origin")
        _git(["fetch", "origin", rev], cwd=cache)
    # Path-limited checkout: pulls only this board's blob from the
    # promisor remote. Idempotent for repeat runs at the same rev.
    _git(["checkout", "--quiet", rev, "--", entry["pcb"]], cwd=cache)
    return cache / entry["pcb"], meta
