#!/usr/bin/env python3
"""Identify missing or partial spots in a matchup's solves directory.

A spot is "complete" only when both the data file (`<idx>_<flop>.jsonl`
or `<idx>_<flop>.jsonl.zst`) and its `.meta` sidecar exist. Anything else
is reported so the driver will (re-)solve it on the next run.

Usage:
    python scripts/find_missing.py SRP
    python scripts/find_missing.py SRP --solves-dir /vision/u/jevon/postflop-solver/solves
    SOLVES_DIR=/path/to/solves python scripts/find_missing.py 3BP

Output:
    - stderr: human-readable summary + per-spot listing
    - stdout: comma-separated canonical indices that need (re-)solving,
      suitable for piping into other tools

Exit code 0 if everything's complete, 1 otherwise.
"""

import argparse
import os
import re
import sys
from pathlib import Path

MATCHUPS = ("SRP", "3BP", "4BP")
STEM_RE = re.compile(r"^(\d{4})_([A-Za-z0-9]+)\.(jsonl\.zst|jsonl|meta)$")

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_canonical_flops() -> list[str]:
    """Returns the 1755 canonical flops by sort-order index (1-based)."""
    p = REPO_ROOT / "data" / "canonical_flops.txt"
    flops: list[str] = []
    for line in p.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        cards = line.split()
        if len(cards) != 3:
            continue
        flops.append("".join(cards))
    if len(flops) != 1755:
        sys.exit(f"expected 1755 canonical flops in {p}, got {len(flops)}")
    return flops


def scan_matchup(matchup_dir: Path) -> dict[int, dict[str, bool]]:
    state: dict[int, dict[str, bool]] = {}
    if not matchup_dir.is_dir():
        return state
    for entry in matchup_dir.iterdir():
        m = STEM_RE.match(entry.name)
        if not m:
            continue
        idx = int(m.group(1))
        ext = m.group(3)
        s = state.setdefault(idx, {"jsonl": False, "meta": False})
        if ext in ("jsonl", "jsonl.zst"):
            s["jsonl"] = True
        elif ext == "meta":
            s["meta"] = True
    return state


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("matchup", choices=MATCHUPS)
    ap.add_argument("--solves-dir", default=None,
                    help="path containing matchup subfolders "
                         "(default: $SOLVES_DIR or <repo>/solves)")
    args = ap.parse_args()

    solves_dir = Path(args.solves_dir
                      or os.environ.get("SOLVES_DIR")
                      or REPO_ROOT / "solves")
    matchup_dir = solves_dir / args.matchup

    print(f"Scanning {matchup_dir}", file=sys.stderr)
    canonical = load_canonical_flops()
    state = scan_matchup(matchup_dir)

    complete: list[int] = []
    partial:  list[tuple[int, str, dict[str, bool]]] = []
    missing:  list[tuple[int, str]] = []
    for idx in range(1, 1756):
        flop = canonical[idx - 1]
        s = state.get(idx)
        if not s:
            missing.append((idx, flop))
        elif s["jsonl"] and s["meta"]:
            complete.append(idx)
        else:
            partial.append((idx, flop, s))

    print(f"Complete: {len(complete):4d} / 1755", file=sys.stderr)
    print(f"Partial:  {len(partial):4d}", file=sys.stderr)
    print(f"Missing:  {len(missing):4d}", file=sys.stderr)

    if partial:
        print("\n=== PARTIAL (will be re-solved on next driver run) ===",
              file=sys.stderr)
        for idx, flop, s in partial:
            tags = []
            if not s["jsonl"]: tags.append("no-jsonl")
            if not s["meta"]:  tags.append("no-meta")
            print(f"  {idx:04d}  {flop}  ({', '.join(tags)})", file=sys.stderr)

    if missing:
        print("\n=== MISSING ===", file=sys.stderr)
        for idx, flop in missing:
            print(f"  {idx:04d}  {flop}", file=sys.stderr)

    todo = [idx for idx, _ in missing] + [idx for idx, _, _ in partial]
    if todo:
        print(",".join(str(i) for i in sorted(todo)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
