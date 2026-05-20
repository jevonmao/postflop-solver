#!/usr/bin/env python3
"""
Audit data/solves/ for unusual betting lines that might hurt LLM fine-tuning.

Checks for:
  - Deep raise sequences (3-bet+, 4-bet+, etc. post-flop)
  - Multiple consecutive aggressive actions
  - All-in over-jam lines (raise over raise over raise)
  - Degenerate single-action nodes (already filtered by prepare script, but verify)
  - Street/history length distribution

Usage:
    python3 scripts/audit_dataset.py [--data-dir data/solves] [--sample-weird N]
"""

import argparse
import json
import os
import re
from collections import Counter, defaultdict


def classify_action(a: str) -> str:
    if a.startswith("deal_"):
        return "deal"
    if a in ("check", "call", "fold"):
        return a
    if a.startswith("bet_"):
        return "bet"
    if a.startswith("raise_") or a.startswith("allin"):
        return "raise"
    return "other"


def parse_history(history: list[str]):
    """Return betting actions only (no deal_ events), split by street."""
    streets = []
    current: list[str] = []
    for h in history:
        if h.startswith("deal_"):
            streets.append(current)
            current = []
        else:
            current.append(classify_action(h))
    streets.append(current)
    return streets  # [flop_actions, turn_actions, river_actions, ...]


def count_raises_in_sequence(actions: list[str]) -> int:
    """Count consecutive raises/bets without a call/check in between."""
    max_streak = 0
    streak = 0
    for a in actions:
        if a in ("bet", "raise"):
            streak += 1
            max_streak = max(max_streak, streak)
        elif a in ("call", "check", "fold"):
            streak = 0
    return max_streak


def street_of_record(history: list[str]) -> str:
    deals = sum(1 for h in history if h.startswith("deal_"))
    return ["flop", "turn", "river", "extra"][min(deals, 3)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="data/solves")
    parser.add_argument("--sample-weird", type=int, default=5,
                        help="Print N example records for each weird category")
    args = parser.parse_args()

    stats = {
        "total": 0,
        "by_matchup": Counter(),
        "by_street": Counter(),
        "single_action": 0,
        "history_len": Counter(),        # distribution of history length (bet actions only)
        "raise_streak": Counter(),       # max consecutive raises per record
        "deep_lines": [],                # records with raise streak >= 3
        "long_histories": [],            # records with many betting actions
    }

    matchup_dirs = [d for d in os.listdir(args.data_dir)
                    if os.path.isdir(os.path.join(args.data_dir, d))]

    for matchup in sorted(matchup_dirs):
        matchup_path = os.path.join(args.data_dir, matchup)
        for fname in os.listdir(matchup_path):
            if not fname.endswith(".jsonl"):
                continue
            fpath = os.path.join(matchup_path, fname)
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    stats["total"] += 1
                    stats["by_matchup"][r.get("matchup", matchup)] += 1

                    history = r.get("history", [])
                    actions_only = [classify_action(h) for h in history
                                    if not h.startswith("deal_")]
                    n_bet_actions = len(actions_only)
                    stats["history_len"][n_bet_actions] += 1

                    street = street_of_record(history)
                    stats["by_street"][street] += 1

                    n_available = len(r.get("actions", []))
                    if n_available <= 1:
                        stats["single_action"] += 1

                    # Check for aggressive sequences within any street
                    streets = parse_history(history)
                    max_streak = max(
                        (count_raises_in_sequence(s) for s in streets),
                        default=0
                    )
                    stats["raise_streak"][max_streak] += 1

                    if max_streak >= 3 and len(stats["deep_lines"]) < args.sample_weird:
                        stats["deep_lines"].append(r)

                    if n_bet_actions >= 8 and len(stats["long_histories"]) < args.sample_weird:
                        stats["long_histories"].append(r)

    total = stats["total"]
    print(f"=== Dataset Audit ===")
    print(f"Total decision records : {total:,}")
    print()

    print("--- Records by matchup ---")
    for m, c in sorted(stats["by_matchup"].items()):
        print(f"  {m:6s}: {c:>10,}  ({100*c/total:.1f}%)")
    print()

    print("--- Records by street ---")
    for s in ["flop", "turn", "river", "extra"]:
        c = stats["by_street"].get(s, 0)
        if c:
            print(f"  {s:6s}: {c:>10,}  ({100*c/total:.1f}%)")
    print()

    print("--- Single-action nodes (degenerate, should be filtered) ---")
    print(f"  {stats['single_action']:,}  ({100*stats['single_action']/total:.2f}%)")
    print()

    print("--- Betting-action history length distribution ---")
    for length in sorted(stats["history_len"]):
        c = stats["history_len"][length]
        bar = "#" * min(40, int(40 * c / total))
        print(f"  len={length:2d}: {c:>8,}  {bar}")
    print()

    print("--- Max consecutive raise/bet streak per record ---")
    for streak in sorted(stats["raise_streak"]):
        c = stats["raise_streak"][streak]
        pct = 100 * c / total
        print(f"  streak={streak}: {c:>8,}  ({pct:.2f}%)")
    print()

    deep_count = sum(v for k, v in stats["raise_streak"].items() if k >= 3)
    print(f"  => Records with 3+ consecutive raises: {deep_count:,}  ({100*deep_count/total:.3f}%)")
    print()

    if stats["deep_lines"]:
        print(f"--- Sample records with deep raise sequences (showing up to {args.sample_weird}) ---")
        for r in stats["deep_lines"]:
            print(f"  matchup={r['matchup']}  board={r['board']}  street={street_of_record(r['history'])}")
            print(f"  history : {r['history']}")
            print(f"  actions : {r['actions']}")
            print()

    if stats["long_histories"]:
        print(f"--- Sample records with long histories (>=8 bet actions, showing up to {args.sample_weird}) ---")
        for r in stats["long_histories"]:
            actions_only = [h for h in r["history"] if not h.startswith("deal_")]
            print(f"  matchup={r['matchup']}  board={r['board']}  street={street_of_record(r['history'])}")
            print(f"  history ({len(actions_only)} bet actions): {r['history']}")
            print()

    print("=== Done ===")


if __name__ == "__main__":
    main()
