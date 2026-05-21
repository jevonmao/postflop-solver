#!/usr/bin/env python3
"""Generate equity estimation and hand-strength classification data from combo-v2 solver files.

Two auxiliary tasks that teach the model fundamental hand-reading without
referencing GTO actions. No solver context block is injected — the model must
learn to estimate equity from board + hand + action history alone.

Task A (--task equity): {prompt, response} where response = "Your equity is 67.3%"
Task B (--task strength): {prompt, response} where response = one of
    nut / strong / marginal / weak / air

Usage:
    pip install zstandard
    python scripts/generate_equity_data.py --task equity --matchups 4BP --max-examples 1000 \\
        --out /tmp/equity_smoke.jsonl

    python scripts/generate_equity_data.py --task strength --matchups SRP,3BP,4BP \\
        --sample-rate 0.05 --out data/strength_5pct.jsonl
"""

import argparse
import json
import math
import random
import sys
from pathlib import Path

try:
    import zstandard as zstd
except ImportError:
    sys.exit("pip install zstandard")

# ---------------------------------------------------------------------------
# Streaming helper
# ---------------------------------------------------------------------------

def iter_jsonl_zst(path):
    with open(path, "rb") as fh:
        dctx = zstd.ZstdDecompressor()
        with dctx.stream_reader(fh) as reader:
            buf = b""
            while True:
                chunk = reader.read(1 << 16)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line:
                        yield json.loads(line)
            if buf.strip():
                yield json.loads(buf)

# ---------------------------------------------------------------------------
# Natural language helpers
# ---------------------------------------------------------------------------

_RANK = {
    "2": "Two", "3": "Three", "4": "Four", "5": "Five", "6": "Six",
    "7": "Seven", "8": "Eight", "9": "Nine", "T": "Ten",
    "J": "Jack", "Q": "Queen", "K": "King", "A": "Ace",
}
_SUIT = {"c": "Club", "d": "Diamond", "h": "Heart", "s": "Spade"}


def card_nl(card):
    return f"{_RANK[card[0].upper()]} of {_SUIT[card[1].lower()]}"


def hand_nl(hand):
    return f"[{card_nl(hand[:2])} and {card_nl(hand[2:])}]"


def action_nl_past(token):
    if token == "check":
        return "checked"
    if token == "call":
        return "called"
    if token == "fold":
        return "folded"
    if token.startswith("bet_"):
        return f"bet {token[4:]} chips"
    if token.startswith("raise_to_"):
        return f"raised to {token[9:]} chips"
    if token.startswith("allin"):
        parts = token.split("_")
        return f"went all-in ({parts[1]} chips)" if len(parts) > 1 else "went all-in"
    return token


_PREFLOP_NARRATIVE = {
    "SRP": "Before the flop, SB raised, BB called.",
    "3BP": "Before the flop, SB raised, BB 3-bet, SB called.",
    "4BP": "Before the flop, SB raised, BB 3-bet, SB 4-bet, BB called.",
}

_STREET_NAME = {3: "flop", 4: "turn", 5: "river"}
_BB_CHIPS = 100
_SB_CHIPS = 50
_STARTING_STACK = 20000


def _street_actions_nl(action_tokens):
    actors = ["BB", "SB"]
    idx = 0
    phrases = []
    for token in action_tokens:
        phrases.append(f"{actors[idx % 2]} {action_nl_past(token)}")
        idx += 1
    return ", ".join(phrases) if phrases else ""


def history_to_narrative(history, board):
    streets_actions = []
    current = []
    for token in history:
        if token.startswith("deal_"):
            streets_actions.append(current)
            current = []
        else:
            current.append(token)
    streets_actions.append(current)

    flop_cards = board[:3]
    turn_card = board[3] if len(board) > 3 else None
    river_card = board[4] if len(board) > 4 else None

    lines = []
    flop_acts = _street_actions_nl(streets_actions[0]) if streets_actions else ""
    if flop_acts:
        lines.append(f"The flop comes {', '.join(card_nl(c) for c in flop_cards)}, then {flop_acts}.")
    else:
        lines.append(f"The flop comes {', '.join(card_nl(c) for c in flop_cards)}.")

    if len(streets_actions) > 1 and turn_card:
        turn_acts = _street_actions_nl(streets_actions[1])
        if turn_acts:
            lines.append(f"The turn comes {card_nl(turn_card)}, then {turn_acts}.")
        else:
            lines.append(f"The turn comes {card_nl(turn_card)}.")

    if len(streets_actions) > 2 and river_card:
        river_acts = _street_actions_nl(streets_actions[2])
        if river_acts:
            lines.append(f"The river comes {card_nl(river_card)}, then {river_acts}.")
        else:
            lines.append(f"The river comes {card_nl(river_card)}.")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Hand-strength bucketing (mirrors solver thresholds)
# ---------------------------------------------------------------------------

def equity_to_strength(eq):
    if eq > 0.85:
        return "nut"
    if eq > 0.65:
        return "strong"
    if eq > 0.40:
        return "marginal"
    if eq > 0.20:
        return "weak"
    return "air"

# ---------------------------------------------------------------------------
# Prompt construction — no solver context block
# ---------------------------------------------------------------------------

def build_prompt(matchup, rec, hero_hand, hero_player, task):
    board = rec["board"]
    street = _STREET_NAME.get(len(board), "river")
    pot = rec["pot"]
    eff_stack = rec["eff_stack"]

    hand_fmt = hand_nl(hero_hand)
    preflop = _PREFLOP_NARRATIVE[matchup]
    postflop = history_to_narrative(rec["history"], board)

    if task == "equity":
        question = (
            f"Now it is your turn to act on the {street}. "
            f"The current pot is {pot} chips, your stack is {eff_stack} chips.\n"
            "Based on your hole cards and the action history, estimate your equity "
            "against your opponent's range.\n"
            "Your equity is approximately:"
        )
    else:  # strength
        question = (
            f"Now it is your turn to act on the {street}. "
            f"The current pot is {pot} chips, your stack is {eff_stack} chips.\n"
            "Classify the strength of your hand against your opponent's range. "
            "Answer with exactly one of: nut, strong, marginal, weak, air.\n"
            "Your hand strength is:"
        )

    lines = [
        "You are a specialist in playing Heads Up No Limit Texas Holdem.",
        "",
        f"The small blind is {_SB_CHIPS} chips and the big blind is {_BB_CHIPS} chips. Everyone started with {_STARTING_STACK} chips.",
        "The player positions involved in this game are BB, SB.",
        f"In this hand, your position is {hero_player}, and your holding is {hand_fmt}.",
        preflop,
        postflop,
        "",
        question,
    ]
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Main generation loop
# ---------------------------------------------------------------------------

def generate(args):
    solves_dir = Path(args.solves_dir)
    matchups = [m.strip() for m in args.matchups.split(",")]
    out_path = Path(args.out)
    task = args.task

    rng = random.Random(args.seed)

    street_rate = {3: args.flop_rate, 4: args.turn_rate, 5: args.river_rate}

    stats = {
        "written": 0,
        "skipped_weight": 0,
        "skipped_street": 0,
        "files_processed": 0,
        "files_skipped_sample": 0,
        "bucket_counts": {"nut": 0, "strong": 0, "marginal": 0, "weak": 0, "air": 0},
    }

    with open(out_path, "w") as out_f:
        for matchup in matchups:
            matchup_dir = solves_dir / matchup
            if not matchup_dir.exists():
                print(f"[warn] no directory for matchup {matchup}: {matchup_dir}", file=sys.stderr)
                continue

            files = sorted(matchup_dir.glob("*.jsonl.zst"))
            print(f"[{matchup}] {len(files)} files found", file=sys.stderr)

            for fpath in files:
                if args.max_examples and stats["written"] >= args.max_examples:
                    break

                if args.sample_rate < 1.0 and rng.random() > args.sample_rate:
                    stats["files_skipped_sample"] += 1
                    continue

                stats["files_processed"] += 1
                it = iter_jsonl_zst(fpath)
                try:
                    header = next(it)
                except StopIteration:
                    continue

                if header.get("schema") != "combo-v2":
                    continue

                combos_oop = header["combos_oop"]
                combos_ip = header["combos_ip"]

                for rec in it:
                    if args.max_examples and stats["written"] >= args.max_examples:
                        break

                    cd = rec.get("combo_data")
                    if cd is None:
                        continue

                    # Street-level downsampling
                    board_len = len(rec["board"])
                    sr = street_rate.get(board_len, args.river_rate)
                    if sr < 1.0 and rng.random() > sr:
                        stats["skipped_street"] += 1
                        continue

                    to_act = rec["to_act"]
                    if to_act == "O":
                        hero_player = "BB"
                        actor_side = cd["oop"]
                        hero_combos = combos_oop
                    else:
                        hero_player = "SB"
                        actor_side = cd["ip"]
                        hero_combos = combos_ip

                    actor_idxs = actor_side["idx"]
                    actor_eqs = actor_side["eq"]
                    actor_ws = actor_side["w"]

                    if not actor_idxs:
                        continue

                    eligible = [j for j, w in enumerate(actor_ws) if w >= args.min_weight]
                    if not eligible:
                        continue

                    sample_ws = [actor_ws[j] for j in eligible]
                    n_sample = min(args.max_per_node, len(eligible))
                    sampled = list(dict.fromkeys(rng.choices(eligible, weights=sample_ws, k=n_sample)))

                    for j in sampled:
                        if args.max_examples and stats["written"] >= args.max_examples:
                            break

                        if actor_ws[j] < args.min_weight:
                            stats["skipped_weight"] += 1
                            continue

                        eq = actor_eqs[j]
                        hero_hand = hero_combos[actor_idxs[j]]

                        prompt = build_prompt(matchup, rec, hero_hand, hero_player, task)

                        if task == "equity":
                            response = f"{eq:.1%}"
                        else:
                            strength = equity_to_strength(eq)
                            response = strength
                            stats["bucket_counts"][strength] += 1

                        record = {"prompt": prompt, "response": response}
                        out_f.write(json.dumps(record) + "\n")
                        stats["written"] += 1

    print(f"\n=== Stats ===", file=sys.stderr)
    print(f"Task              : {task}", file=sys.stderr)
    print(f"Files processed   : {stats['files_processed']}", file=sys.stderr)
    print(f"Files skipped     : {stats['files_skipped_sample']} (sample-rate filter)", file=sys.stderr)
    print(f"Examples written  : {stats['written']}", file=sys.stderr)
    print(f"Skipped (weight)  : {stats['skipped_weight']}", file=sys.stderr)
    print(f"Skipped (street)  : {stats['skipped_street']}", file=sys.stderr)
    if task == "strength":
        total = sum(stats["bucket_counts"].values()) or 1
        print(f"Bucket distribution:", file=sys.stderr)
        for b, cnt in stats["bucket_counts"].items():
            print(f"  {b:10s}: {cnt:8d}  ({cnt/total:.1%})", file=sys.stderr)
    return stats["written"]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--task", choices=["equity", "strength"], default="equity",
                    help="equity: predict numeric equity; strength: predict nut/strong/marginal/weak/air (default: equity)")
    ap.add_argument("--solves-dir", default="data/solves_combo",
                    help="Root directory with matchup subdirs of .jsonl.zst files")
    ap.add_argument("--out", default="data/equity_training.jsonl",
                    help="Output JSONL path")
    ap.add_argument("--matchups", default="SRP,3BP,4BP",
                    help="Comma-separated matchup names (default: SRP,3BP,4BP)")
    ap.add_argument("--min-weight", type=float, default=0.005,
                    help="Skip combos with reach weight below this threshold (default 0.005)")
    ap.add_argument("--max-per-node", type=int, default=3,
                    help="Max combos sampled per decision node (default 3)")
    ap.add_argument("--max-examples", type=int, default=0,
                    help="Cap total output examples; 0 = unlimited")
    ap.add_argument("--sample-rate", type=float, default=1.0,
                    help="Fraction of .jsonl.zst files to include (default 1.0)")
    ap.add_argument("--seed", type=int, default=42,
                    help="Random seed for sampling (default 42)")
    ap.add_argument("--flop-rate", type=float, default=1.0,
                    help="Fraction of flop nodes to include (default 1.0)")
    ap.add_argument("--turn-rate", type=float, default=0.5,
                    help="Fraction of turn nodes to include (default 0.5)")
    ap.add_argument("--river-rate", type=float, default=0.1,
                    help="Fraction of river nodes to include (default 0.1)")
    args = ap.parse_args()

    n = generate(args)
    print(n)


if __name__ == "__main__":
    main()
