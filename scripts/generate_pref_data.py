#!/usr/bin/env python3
"""Generate preference training data (ORPO / SIMPO / DPO) from combo-v2 solver files.

Streams data/solves_combo/ → {prompt, chosen, rejected} JSONL.
Compatible with trl.SimPOTrainer, trl.ORPOTrainer, trl.DPOTrainer.

Usage:
    pip install zstandard
    # Smoke: 1000 examples from 4BP files (fast)
    python scripts/generate_pref_data.py --matchups 4BP --max-examples 1000 --out /tmp/pref_smoke.jsonl

    # Full run, 10%% sample rate (~500k examples)
    python scripts/generate_pref_data.py --matchups SRP --sample-rate 0.1 --out data/pref_srp_10pct.jsonl
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
# Streaming helper (mirrors decode_combo_data.py)
# ---------------------------------------------------------------------------

def iter_jsonl_zst(path):
    """Yield decoded JSON dicts from a .jsonl.zst file."""
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
# Natural language helpers (mirror prompt_formatter.py conventions)
# ---------------------------------------------------------------------------

_RANK = {
    "2": "Two", "3": "Three", "4": "Four", "5": "Five", "6": "Six",
    "7": "Seven", "8": "Eight", "9": "Nine", "T": "Ten",
    "J": "Jack", "Q": "Queen", "K": "King", "A": "Ace",
}
_SUIT = {"c": "Club", "d": "Diamond", "h": "Heart", "s": "Spade"}


def card_nl(card: str) -> str:
    return f"{_RANK[card[0].upper()]} of {_SUIT[card[1].lower()]}"


def hand_nl(hand: str) -> str:
    return f"[{card_nl(hand[:2])} and {card_nl(hand[2:])}]"


def action_nl(token: str) -> str:
    """Solver action token → concise display string."""
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


def action_nl_past(token: str) -> str:
    """Past-tense action for narrative reconstruction."""
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


def legal_actions_nl(actions: list) -> str:
    return ", ".join(action_nl(a) for a in actions)


# ---------------------------------------------------------------------------
# Preflop narrative (fixed per matchup)
# ---------------------------------------------------------------------------

# In HU postflop: OOP = BB, IP = SB. Preflop: SB acts first.
_PREFLOP_NARRATIVE = {
    "SRP": "Before the flop, SB raised, BB called.",
    "3BP": "Before the flop, SB raised, BB 3-bet, SB called.",
    "4BP": "Before the flop, SB raised, BB 3-bet, SB 4-bet, BB called.",
}

_STREET_NAME = {3: "flop", 4: "turn", 5: "river"}

# Chip unit: 1 chip = 0.01 BB; 200 BB = 20,000 chips; BB = 100 chips, SB = 50 chips
_BB_CHIPS = 100
_SB_CHIPS = 50
_STARTING_STACK = 20000

# ---------------------------------------------------------------------------
# Postflop history → English narrative
# ---------------------------------------------------------------------------

def _street_actions_nl(action_tokens: list) -> str:
    """Convert a single street's action tokens to narrative with BB/SB attribution.

    OOP (BB) acts first on every postflop street. Each action advances the actor index.
    """
    actors = ["BB", "SB"]
    idx = 0
    phrases = []
    for token in action_tokens:
        actor = actors[idx % 2]
        phrases.append(f"{actor} {action_nl_past(token)}")
        idx += 1
    return ", ".join(phrases) if phrases else ""


def history_to_narrative(history: list, board: list) -> str:
    """Reconstruct postflop narrative from solver history tokens.

    history: list of tokens like ["check", "bet_165", "deal_9s", "check"]
    board:   current board as card strings, e.g. ["Kh","7d","2c","9s"]

    The flop is NOT a deal token — it's the game's starting state. Turn and river
    cards appear as "deal_X" tokens splitting each street's actions.
    """
    streets_actions: list = []
    current: list = []
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

    def board_desc(cards: list) -> str:
        return ", ".join(card_nl(c) for c in cards)

    lines = []

    # Flop
    flop_acts = _street_actions_nl(streets_actions[0]) if streets_actions else ""
    flop_desc = board_desc(flop_cards)
    if flop_acts:
        lines.append(f"The flop comes {flop_desc}, then {flop_acts}.")
    else:
        lines.append(f"The flop comes {flop_desc}.")

    # Turn
    if len(streets_actions) > 1 and turn_card:
        turn_acts = _street_actions_nl(streets_actions[1])
        turn_desc = card_nl(turn_card)
        if turn_acts:
            lines.append(f"The turn comes {turn_desc}, then {turn_acts}.")
        else:
            lines.append(f"The turn comes {turn_desc}.")

    # River
    if len(streets_actions) > 2 and river_card:
        river_acts = _street_actions_nl(streets_actions[2])
        river_desc = card_nl(river_card)
        if river_acts:
            lines.append(f"The river comes {river_desc}, then {river_acts}.")
        else:
            lines.append(f"The river comes {river_desc}.")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Strategy utilities
# ---------------------------------------------------------------------------

def normalize_strategy(entry, n_actions: int) -> list:
    """Handle both pure (int action index) and mixed (list[float]) strategy entries."""
    if isinstance(entry, int):
        freqs = [0.0] * n_actions
        freqs[entry] = 1.0
        return freqs
    total = sum(entry)
    if total < 1e-9:
        return [1.0 / n_actions] * n_actions
    return [f / total for f in entry]


def entropy(freqs: list) -> float:
    return -sum(f * math.log(f + 1e-12) for f in freqs if f > 1e-9)


def _agg_level(token: str) -> int:
    """Aggression level 0–4 for a solver action token."""
    if token.startswith("fold"):
        return 0
    if token.startswith("check"):
        return 1
    if token.startswith("call"):
        return 2
    if token.startswith("bet"):
        return 3
    return 4  # raise / allin


def pick_worst_rejected(zero_idxs: list, actions: list, chosen_idx: int) -> int:
    """Return the zero-frequency action index that maximizes contrast with chosen."""
    chosen_agg = _agg_level(actions[chosen_idx])
    return max(zero_idxs, key=lambda i: abs(_agg_level(actions[i]) - chosen_agg))


def pick_soft_rejected(freqs: list, actions: list, chosen_idx: int, min_gap: float) -> int | None:
    """Return the lowest-frequency action index if gap to chosen exceeds min_gap.

    Used when no action has 0% frequency (mixed-strategy nodes). Returns None if
    the gap is too small to constitute a meaningful preference signal.
    """
    n = len(freqs)
    chosen_agg = _agg_level(actions[chosen_idx])
    # Find best contrast candidate among non-chosen actions
    candidates = [(i, freqs[i]) for i in range(n) if i != chosen_idx]
    if not candidates:
        return None
    # Prefer max aggression contrast, break ties by lower frequency
    rejected_idx = min(candidates, key=lambda t: (-abs(_agg_level(actions[t[0]]) - chosen_agg), t[1]))[0]
    if freqs[chosen_idx] - freqs[rejected_idx] < min_gap:
        return None
    return rejected_idx

# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

def build_prompt(
    matchup: str,
    rec: dict,
    hero_hand: str,
    hero_player: str,
    hero_eq: float,
    hero_ev: int,
) -> str:
    """Build the full LLM prompt for one (node, combo) decision."""
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

def generate(args) -> int:
    solves_dir = Path(args.solves_dir)
    matchups = [m.strip() for m in args.matchups.split(",")]
    out_path = Path(args.out)

    rng = random.Random(args.seed)

    stats = {
        "written": 0,
        "skipped_weight": 0,
        "skipped_entropy": 0,
        "skipped_no_rejected": 0,
        "soft_pairs": 0,
        "hard_pairs": 0,
        "files_processed": 0,
        "files_skipped_sample": 0,
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

                    # Node-level entropy filter: skip near-pure-strategy nodes
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

                    # Weight-proportional sampling up to max_per_node combos
                    eligible = [j for j, w in enumerate(actor_ws) if w >= args.min_weight]
                    if not eligible:
                        continue

                    sample_ws = [actor_ws[j] for j in eligible]
                    n_sample = min(args.max_per_node, len(eligible))
                    sampled = rng.choices(eligible, weights=sample_ws, k=n_sample)
                    sampled = list(dict.fromkeys(sampled))  # deduplicate

                    for j in sampled:
                        if args.max_examples and stats["written"] >= args.max_examples:
                            break

                        if actor_ws[j] < args.min_weight:
                            stats["skipped_weight"] += 1
                            continue

                        freqs = normalize_strategy(strategy_list[j], n_act)

                        chosen_idx = max(range(n_act), key=lambda i: freqs[i])
                        zero_idxs = [i for i, f in enumerate(freqs) if f < 0.01]

                        if not zero_idxs:
                            if args.min_freq_gap > 0.0:
                                rejected_idx = pick_soft_rejected(freqs, actions, chosen_idx, args.min_freq_gap)
                                if rejected_idx is None:
                                    stats["skipped_no_rejected"] += 1
                                    continue
                                stats["soft_pairs"] += 1
                            else:
                                stats["skipped_no_rejected"] += 1
                                continue
                        else:
                            rejected_idx = pick_worst_rejected(zero_idxs, actions, chosen_idx)
                            stats["hard_pairs"] += 1

                        hero_hand = hero_combos[actor_idxs[j]]
                        hero_eq = actor_eqs[j]
                        hero_ev = actor_evs[j]

                        prompt = build_prompt(
                            matchup, rec,
                            hero_hand, hero_player,
                            hero_eq, hero_ev,
                        )

                        record = {
                            "prompt": prompt,
                            "chosen": f"<action>{action_nl(actions[chosen_idx])}</action>",
                            "rejected": f"<action>{action_nl(actions[rejected_idx])}</action>",
                        }
                        out_f.write(json.dumps(record) + "\n")
                        stats["written"] += 1

    print(f"\n=== Stats ===", file=sys.stderr)
    print(f"Files processed   : {stats['files_processed']}", file=sys.stderr)
    print(f"Files skipped     : {stats['files_skipped_sample']} (sample-rate filter)", file=sys.stderr)
    print(f"Examples written  : {stats['written']}", file=sys.stderr)
    print(f"  Hard pairs (0%) : {stats['hard_pairs']}", file=sys.stderr)
    print(f"  Soft pairs (gap): {stats['soft_pairs']}", file=sys.stderr)
    print(f"Skipped (entropy) : {stats['skipped_entropy']}", file=sys.stderr)
    print(f"Skipped (weight)  : {stats['skipped_weight']}", file=sys.stderr)
    print(f"Skipped (no rej.) : {stats['skipped_no_rejected']}", file=sys.stderr)
    return stats["written"]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--solves-dir", default="data/solves_combo",
                    help="Root directory with matchup subdirs of .jsonl.zst files")
    ap.add_argument("--out", default="data/pref_training.jsonl",
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
    ap.add_argument("--min-freq-gap", type=float, default=0.0,
                    help="When >0, emit soft preference pairs for mixed-strategy combos with no "
                         "0%%-freq action. chosen=argmax, rejected=argmin, only if "
                         "chosen_freq - rejected_freq >= this value (default 0.0 = disabled). "
                         "Recommended: 0.5 to recover ~58%% of skipped combos.")
    args = ap.parse_args()

    n = generate(args)
    print(n)


if __name__ == "__main__":
    main()
