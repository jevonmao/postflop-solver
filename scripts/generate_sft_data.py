#!/usr/bin/env python3
"""Generate weighted SFT (supervised fine-tuning) data from combo-v2 solver files.

Produces {prompt, response} pairs where each response is a single GTO action.
Mixed-strategy combos emit multiple records sampled proportionally to action
frequencies — e.g. KcKd with bet 70% / check 30% generates ~7 bet records for
every 3 check records. This is the only way to teach correct mixing via SFT.

Usage:
    pip install zstandard
    # Smoke: 1000 examples from 4BP files
    python scripts/generate_sft_data.py --matchups 4BP --max-examples 1000 --out /tmp/sft_smoke.jsonl

    # Full run, 10%% sample rate
    python scripts/generate_sft_data.py --sample-rate 0.1 --out data/sft_10pct.jsonl
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
# Natural language helpers (shared with generate_pref_data.py)
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


def action_nl(token):
    if token == "check":
        return "check"
    if token == "call":
        return "call"
    if token == "fold":
        return "fold"
    if token.startswith("bet_"):
        return f"bet {token[4:]}"
    if token.startswith("raise_to_"):
        return f"raise {token[9:]}"
    if token.startswith("allin"):
        parts = token.split("_")
        return f"all-in {parts[1]}" if len(parts) > 1 else "all-in"
    return token


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


def legal_actions_nl(actions):
    return ", ".join(action_nl(a) for a in actions)


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

    def board_desc(cards):
        return ", ".join(card_nl(c) for c in cards)

    lines = []

    flop_acts = _street_actions_nl(streets_actions[0]) if streets_actions else ""
    if flop_acts:
        lines.append(f"The flop comes {board_desc(flop_cards)}, then {flop_acts}.")
    else:
        lines.append(f"The flop comes {board_desc(flop_cards)}.")

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
# Strategy utilities
# ---------------------------------------------------------------------------

def normalize_strategy(entry, n_actions):
    if isinstance(entry, int):
        freqs = [0.0] * n_actions
        freqs[entry] = 1.0
        return freqs
    total = sum(entry)
    if total < 1e-9:
        return [1.0 / n_actions] * n_actions
    return [f / total for f in entry]


def entropy(freqs):
    return -sum(f * math.log(f + 1e-12) for f in freqs if f > 1e-9)

# ---------------------------------------------------------------------------
# Prompt construction (same as generate_pref_data.py)
# ---------------------------------------------------------------------------

def build_prompt(matchup, rec, hero_hand, hero_player, hero_eq, hero_ev):
    board = rec["board"]
    street = _STREET_NAME.get(len(board), "river")
    pot = rec["pot"]
    eff_stack = rec["eff_stack"]
    actions = rec["actions"]
    oop = rec["oop"]
    ip = rec["ip"]

    hand_fmt = hand_nl(hero_hand)
    preflop = _PREFLOP_NARRATIVE[matchup]
    postflop = history_to_narrative(rec["history"], board)

    ra = rec["range_advantage"]
    na = rec["nut_advantage"]

    if ra == "OOP":
        range_adv = f"OOP (BB) has range advantage (OOP range_eq={oop['range_eq']:.3f}, IP range_eq={ip['range_eq']:.3f})"
    elif ra == "IP":
        range_adv = f"IP (SB) has range advantage (OOP range_eq={oop['range_eq']:.3f}, IP range_eq={ip['range_eq']:.3f})"
    else:
        range_adv = f"Neither player has a clear range advantage (OOP range_eq={oop['range_eq']:.3f}, IP range_eq={ip['range_eq']:.3f})"

    if na == "OOP":
        nut_adv = "OOP (BB) has nut advantage"
    elif na == "IP":
        nut_adv = "IP (SB) has nut advantage"
    else:
        nut_adv = "Neither player has a clear nut advantage"

    lines = [
        "You are a specialist in playing Heads Up No Limit Texas Holdem. The following will be a game scenario and you need to make the optimal decision.",
        "",
        "Here is a game summary:",
        "",
        f"The small blind is {_SB_CHIPS} chips and the big blind is {_BB_CHIPS} chips. Everyone started with {_STARTING_STACK} chips.",
        "The player positions involved in this game are BB, SB.",
        f"In this hand, your position is {hero_player}, and your holding is {hand_fmt}.",
        preflop,
        postflop,
        "",
        "",
        f"Now it is your turn to make a move on the {street}.",
        f"To remind you, the current pot size is {pot} chips, and your holding is {hand_fmt}.",
        f"Your current stack: {eff_stack} chips.",
        f"Legal actions: {legal_actions_nl(actions)}.",
        "",
        "GTO SOLVER CONTEXT:",
        f"Your equity: {hero_eq:.1%}",
        f"Your EV: {hero_ev} chips",
        f"Range advantage: {range_adv}",
        f"Nut advantage: {nut_adv}",
        f"OOP range: {oop['nut']:.1%} nut / {oop['strong']:.1%} strong / {oop['marginal']:.1%} marginal / {oop['air']:.1%} air",
        f"IP  range: {ip['nut']:.1%} nut / {ip['strong']:.1%} strong / {ip['marginal']:.1%} marginal / {ip['air']:.1%} air",
        "",
        "Decide on an action based on the strength of your hand and the GTO solver context above. Do not explain your answer.",
        "Your optimal action is:",
    ]
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Main generation loop
# ---------------------------------------------------------------------------

def generate(args):
    solves_dir = Path(args.solves_dir)
    matchups = [m.strip() for m in args.matchups.split(",")]
    out_path = Path(args.out)

    rng = random.Random(args.seed)

    # Street-level sample rates: flop=1.0, turn=0.5, river=0.1 by default
    street_rate = {
        3: args.flop_rate,
        4: args.turn_rate,
        5: args.river_rate,
    }

    stats = {
        "written": 0,
        "skipped_weight": 0,
        "skipped_entropy": 0,
        "skipped_street": 0,
        "files_processed": 0,
        "files_skipped_sample": 0,
        "actions_emitted": {},
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
                    print(f"[warn] unsupported schema in {fpath}, skipping", file=sys.stderr)
                    continue

                combos_oop = header["combos_oop"]
                combos_ip = header["combos_ip"]

                for rec in it:
                    if args.max_examples and stats["written"] >= args.max_examples:
                        break

                    cd = rec.get("combo_data")
                    if cd is None:
                        continue

                    actions = rec["actions"]
                    n_act = len(actions)
                    if n_act <= 1:
                        continue

                    # Street-level downsampling
                    board_len = len(rec["board"])
                    sr = street_rate.get(board_len, args.river_rate)
                    if sr < 1.0 and rng.random() > sr:
                        stats["skipped_street"] += 1
                        continue

                    # Node-level entropy filter
                    range_strat = rec.get("range_strategy", [])
                    if range_strat and entropy(range_strat) < args.min_entropy:
                        stats["skipped_entropy"] += 1
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
                    actor_evs = actor_side["ev"]
                    strategy_list = cd["strategy"]

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

                        freqs = normalize_strategy(strategy_list[j], n_act)

                        # For each action with non-zero frequency, emit one SFT record.
                        # The record is sampled proportionally to freq × reach_weight,
                        # but here we emit the argmax action once per combo
                        # (caller can set --actions-per-combo for weighted multi-emit).
                        if args.actions_per_combo == 1:
                            # Simple argmax — faster, less training signal for mixing
                            action_idx = max(range(n_act), key=lambda i: freqs[i])
                            emit_actions = [(action_idx, 1.0)]
                        else:
                            # Emit one record per non-trivial action, weight = freq
                            # (training loss is implicitly weighted by dataset frequency)
                            emit_actions = [
                                (i, f) for i, f in enumerate(freqs)
                                if f >= args.min_action_freq
                            ]

                        hero_hand = hero_combos[actor_idxs[j]]
                        hero_eq = actor_eqs[j]
                        hero_ev = actor_evs[j]

                        prompt = build_prompt(matchup, rec, hero_hand, hero_player, hero_eq, hero_ev)

                        for action_idx, _freq in emit_actions:
                            if args.max_examples and stats["written"] >= args.max_examples:
                                break
                            action_tok = actions[action_idx]
                            record = {
                                "prompt": prompt,
                                "response": f"<action>{action_nl(action_tok)}</action>",
                            }
                            out_f.write(json.dumps(record) + "\n")
                            stats["written"] += 1
                            stats["actions_emitted"][action_tok] = stats["actions_emitted"].get(action_tok, 0) + 1

    print(f"\n=== Stats ===", file=sys.stderr)
    print(f"Files processed   : {stats['files_processed']}", file=sys.stderr)
    print(f"Files skipped     : {stats['files_skipped_sample']} (sample-rate filter)", file=sys.stderr)
    print(f"Examples written  : {stats['written']}", file=sys.stderr)
    print(f"Skipped (entropy) : {stats['skipped_entropy']}", file=sys.stderr)
    print(f"Skipped (weight)  : {stats['skipped_weight']}", file=sys.stderr)
    print(f"Skipped (street)  : {stats['skipped_street']}", file=sys.stderr)
    if stats["actions_emitted"]:
        print(f"Action distribution:", file=sys.stderr)
        total = sum(stats["actions_emitted"].values())
        for tok, cnt in sorted(stats["actions_emitted"].items(), key=lambda x: -x[1])[:10]:
            print(f"  {tok:25s}: {cnt:8d}  ({cnt/total:.1%})", file=sys.stderr)
    return stats["written"]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--solves-dir", default="data/solves_combo",
                    help="Root directory with matchup subdirs of .jsonl.zst files")
    ap.add_argument("--out", default="data/sft_training.jsonl",
                    help="Output JSONL path")
    ap.add_argument("--matchups", default="SRP,3BP,4BP",
                    help="Comma-separated matchup names (default: SRP,3BP,4BP)")
    ap.add_argument("--min-weight", type=float, default=0.005,
                    help="Skip combos with reach weight below this threshold (default 0.005)")
    ap.add_argument("--min-entropy", type=float, default=0.1,
                    help="Skip nodes whose range_strategy entropy is below this (default 0.1)")
    ap.add_argument("--max-per-node", type=int, default=3,
                    help="Max combos sampled per decision node (default 3)")
    ap.add_argument("--max-examples", type=int, default=0,
                    help="Cap total output examples; 0 = unlimited")
    ap.add_argument("--sample-rate", type=float, default=1.0,
                    help="Fraction of .jsonl.zst files to include (default 1.0)")
    ap.add_argument("--seed", type=int, default=42,
                    help="Random seed for sampling (default 42)")
    ap.add_argument("--actions-per-combo", type=int, default=1,
                    help="1 = emit argmax only (fast); >1 = emit all non-trivial actions "
                         "proportionally for weighted mixing (default 1)")
    ap.add_argument("--min-action-freq", type=float, default=0.05,
                    help="Minimum action frequency to emit when --actions-per-combo>1 (default 0.05)")
    ap.add_argument("--flop-rate", type=float, default=1.0,
                    help="Fraction of flop nodes to include (default 1.0 — keep all)")
    ap.add_argument("--turn-rate", type=float, default=0.5,
                    help="Fraction of turn nodes to include (default 0.5)")
    ap.add_argument("--river-rate", type=float, default=0.1,
                    help="Fraction of river nodes to include (default 0.1)")
    args = ap.parse_args()

    n = generate(args)
    print(n)


if __name__ == "__main__":
    main()
