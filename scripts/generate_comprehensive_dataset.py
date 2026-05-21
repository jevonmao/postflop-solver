#!/usr/bin/env python3
"""Generate a comprehensive GTO poker training dataset from combo-v2 solver files.

Single pass through all solver data, emitting 7 task types simultaneously.
Only filter applied: street rebalancing (flop 100%, turn 50%, river 10%).
Intended for HuggingFace publication as a multi-config dataset.

Output files written to --out-dir:
  action_pref.jsonl       {prompt, chosen, rejected, metadata}
                          Preference pairs: chosen=GTO argmax, rejected=0%-freq action (hard)
                          or lowest-freq action with gap>=0.5 (soft). Both tagged in metadata.

  action_sft.jsonl        {prompt, response, metadata}
                          SFT: all actions with freq>=5%, one record per (combo, action).
                          Dataset frequency naturally encodes the mixing distribution.

  equity.jsonl            {prompt, response, metadata}
                          Equity estimation: predict hero equity from hand+board+history.
                          No solver context in prompt — pure hand-reading task.

  hand_strength.jsonl     {prompt, response, metadata}
                          Hand classification: nut / strong / marginal / weak / air.
                          Same prompts as equity but coarser bucketed output.

  range_advantage.jsonl   {prompt, response, metadata}
                          Per-node (no hand): which player has range/nut advantage?
                          Input: board + matchup + action history + range composition.
                          Output: structured text e.g. "IP range advantage, OOP nut advantage"

  range_strategy.jsonl    {prompt, response, metadata}
                          Per-node (no hand): predict range-level action frequencies.
                          Input: board + matchup + history + both players' range stats.
                          Output: "check: 99.3% | bet 165: 0.5% | bet 375: 0.2%"

Usage:
    pip install zstandard
    python scripts/generate_comprehensive_dataset.py \\
        --solves-dir solves-combo-full \\
        --out-dir data/comprehensive_dataset \\
        --matchups SRP,3BP,4BP \\
        --sample-rate 0.1

    # Full dataset (178 GB input, slow):
    python scripts/generate_comprehensive_dataset.py \\
        --solves-dir solves-combo-full \\
        --out-dir data/comprehensive_dataset
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

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(it, **kwargs):
        return it

# ---------------------------------------------------------------------------
# Streaming reader
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


def action_nl(token):
    if token in ("check", "call", "fold"):
        return token
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
    phrases = []
    for i, token in enumerate(action_tokens):
        phrases.append(f"{actors[i % 2]} {action_nl_past(token)}")
    return ", ".join(phrases)


def history_to_narrative(history, board):
    streets = []
    current = []
    for token in history:
        if token.startswith("deal_"):
            streets.append(current)
            current = []
        else:
            current.append(token)
    streets.append(current)

    flop_cards = board[:3]
    turn_card = board[3] if len(board) > 3 else None
    river_card = board[4] if len(board) > 4 else None

    lines = []

    flop_acts = _street_actions_nl(streets[0]) if streets else ""
    flop_desc = ", ".join(card_nl(c) for c in flop_cards)
    lines.append(f"The flop comes {flop_desc}" + (f", then {flop_acts}." if flop_acts else "."))

    if len(streets) > 1 and turn_card:
        turn_acts = _street_actions_nl(streets[1])
        lines.append(f"The turn comes {card_nl(turn_card)}" + (f", then {turn_acts}." if turn_acts else "."))

    if len(streets) > 2 and river_card:
        river_acts = _street_actions_nl(streets[2])
        lines.append(f"The river comes {card_nl(river_card)}" + (f", then {river_acts}." if river_acts else "."))

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Strategy + equity utilities
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


def _agg_level(token):
    if token.startswith("fold"):  return 0
    if token.startswith("check"): return 1
    if token.startswith("call"):  return 2
    if token.startswith("bet"):   return 3
    return 4  # raise / allin


def pick_hard_rejected(zero_idxs, actions, chosen_idx):
    chosen_agg = _agg_level(actions[chosen_idx])
    return max(zero_idxs, key=lambda i: abs(_agg_level(actions[i]) - chosen_agg))


def pick_soft_rejected(freqs, actions, chosen_idx, min_gap):
    candidates = [(i, freqs[i]) for i in range(len(freqs)) if i != chosen_idx]
    if not candidates:
        return None
    chosen_agg = _agg_level(actions[chosen_idx])
    rejected_idx = min(candidates,
                       key=lambda t: (-abs(_agg_level(actions[t[0]]) - chosen_agg), t[1]))[0]
    if freqs[chosen_idx] - freqs[rejected_idx] < min_gap:
        return None
    return rejected_idx

# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_SYSTEM_HEADER = (
    "You are a specialist in playing Heads Up No Limit Texas Holdem. "
    "The following will be a game scenario and you need to make the optimal decision."
)

_RANGE_SYSTEM_HEADER = (
    "You are a specialist in Heads Up No Limit Texas Holdem GTO strategy. "
    "Analyze the following game situation and answer the question."
)

_STACK_HEADER = (
    f"The small blind is {_SB_CHIPS} chips and the big blind is {_BB_CHIPS} chips. "
    f"Everyone started with {_STARTING_STACK} chips."
)


def _situation_block(matchup, rec, hero_player, hand_fmt):
    """Shared narrative for per-combo prompts."""
    board = rec["board"]
    postflop = history_to_narrative(rec["history"], board)
    return "\n".join([
        _STACK_HEADER,
        "The player positions involved in this game are BB, SB.",
        f"In this hand, your position is {hero_player}, and your holding is {hand_fmt}.",
        _PREFLOP_NARRATIVE[matchup],
        postflop,
    ])


def build_action_prompt(matchup, rec, hero_hand, hero_player, hero_eq, hero_ev):
    board = rec["board"]
    street = _STREET_NAME.get(len(board), "river")
    pot, eff_stack = rec["pot"], rec["eff_stack"]
    actions = rec["actions"]
    oop, ip = rec["oop"], rec["ip"]
    hand_fmt = hand_nl(hero_hand)

    ra, na = rec["range_advantage"], rec["nut_advantage"]
    if ra == "OOP":
        range_adv = f"OOP (BB) has range advantage (OOP range_eq={oop['range_eq']:.3f}, IP range_eq={ip['range_eq']:.3f})"
    elif ra == "IP":
        range_adv = f"IP (SB) has range advantage (OOP range_eq={oop['range_eq']:.3f}, IP range_eq={ip['range_eq']:.3f})"
    else:
        range_adv = f"Neither player has a clear range advantage (OOP range_eq={oop['range_eq']:.3f}, IP range_eq={ip['range_eq']:.3f})"

    if na == "OOP":   nut_adv = "OOP (BB) has nut advantage"
    elif na == "IP":  nut_adv = "IP (SB) has nut advantage"
    else:             nut_adv = "Neither player has a clear nut advantage"

    return "\n".join([
        _SYSTEM_HEADER, "",
        "Here is a game summary:", "",
        _situation_block(matchup, rec, hero_player, hand_fmt), "", "",
        f"Now it is your turn to make a move on the {street}.",
        f"To remind you, the current pot size is {pot} chips, and your holding is {hand_fmt}.",
        f"Your current stack: {eff_stack} chips.",
        f"Legal actions: {legal_actions_nl(actions)}.", "",
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
    ])


def build_equity_prompt(matchup, rec, hero_hand, hero_player, task):
    board = rec["board"]
    street = _STREET_NAME.get(len(board), "river")
    pot, eff_stack = rec["pot"], rec["eff_stack"]
    hand_fmt = hand_nl(hero_hand)

    if task == "equity":
        question = (
            f"Now it is your turn to act on the {street}. "
            f"The current pot is {pot} chips, your stack is {eff_stack} chips.\n"
            "Based on your hole cards and the action history, estimate your equity "
            "against your opponent's range.\n"
            "Your equity is approximately:"
        )
    else:
        question = (
            f"Now it is your turn to act on the {street}. "
            f"The current pot is {pot} chips, your stack is {eff_stack} chips.\n"
            "Classify the strength of your hand against your opponent's range. "
            "Answer with exactly one of: nut, strong, marginal, weak, air.\n"
            "Your hand strength is:"
        )

    return "\n".join([
        "You are a specialist in playing Heads Up No Limit Texas Holdem.", "",
        _situation_block(matchup, rec, hero_player, hand_fmt), "",
        question,
    ])


def build_range_advantage_prompt(matchup, rec):
    """Per-node prompt: no hand. Asks which player has range/nut advantage."""
    board = rec["board"]
    street = _STREET_NAME.get(len(board), "river")
    pot = rec["pot"]
    oop, ip = rec["oop"], rec["ip"]
    postflop = history_to_narrative(rec["history"], board)

    return "\n".join([
        _RANGE_SYSTEM_HEADER, "",
        _STACK_HEADER,
        "The player positions involved in this game are BB, SB (BB is out of position).",
        _PREFLOP_NARRATIVE[matchup],
        postflop, "",
        f"Current pot: {pot} chips. It is the {street}.", "",
        f"OOP (BB) range composition: {oop['nut']:.1%} nut / {oop['strong']:.1%} strong / "
        f"{oop['marginal']:.1%} marginal / {oop['weak']:.1%} weak / {oop['air']:.1%} air",
        f"IP (SB) range composition:  {ip['nut']:.1%} nut / {ip['strong']:.1%} strong / "
        f"{ip['marginal']:.1%} marginal / {ip['weak']:.1%} weak / {ip['air']:.1%} air", "",
        "Which player has range advantage and nut advantage on this board given the action history? "
        "Answer with the player (OOP, IP, or EVEN) for each, separated by a comma.",
        "Range advantage, Nut advantage:",
    ])


def build_range_strategy_prompt(matchup, rec):
    """Per-node prompt: no hand. Predicts the range-level action frequency distribution."""
    board = rec["board"]
    street = _STREET_NAME.get(len(board), "river")
    pot, eff_stack = rec["pot"], rec["eff_stack"]
    oop, ip = rec["oop"], rec["ip"]
    to_act = rec["to_act"]
    actor_label = "OOP (BB)" if to_act == "O" else "IP (SB)"
    actions = rec["actions"]
    postflop = history_to_narrative(rec["history"], board)

    ra, na = rec["range_advantage"], rec["nut_advantage"]
    range_adv_str = f"{ra} range advantage, {na} nut advantage"

    return "\n".join([
        _RANGE_SYSTEM_HEADER, "",
        _STACK_HEADER,
        "The player positions involved in this game are BB, SB (BB is out of position).",
        _PREFLOP_NARRATIVE[matchup],
        postflop, "",
        f"Current pot: {pot} chips, effective stack: {eff_stack} chips. It is the {street}.",
        f"It is {actor_label}'s turn to act.",
        f"Legal actions: {legal_actions_nl(actions)}.", "",
        f"OOP (BB) range: range_eq={oop['range_eq']:.3f} | {oop['nut']:.1%} nut / {oop['strong']:.1%} strong / "
        f"{oop['marginal']:.1%} marginal / {oop['weak']:.1%} weak / {oop['air']:.1%} air",
        f"IP (SB) range:  range_eq={ip['range_eq']:.3f} | {ip['nut']:.1%} nut / {ip['strong']:.1%} strong / "
        f"{ip['marginal']:.1%} marginal / {ip['weak']:.1%} weak / {ip['air']:.1%} air",
        f"Range advantage: {range_adv_str}", "",
        f"Given this context, what is the GTO range-level action frequency for {actor_label}?",
        "Express as percentages for each action, e.g. 'check: 52.0% | bet 165: 48.0%'",
        "GTO range strategy:",
    ])

# ---------------------------------------------------------------------------
# Main generation loop
# ---------------------------------------------------------------------------

TASK_NAMES = ["action_pref", "action_sft", "equity", "hand_strength",
              "range_advantage", "range_strategy"]


def board_to_str(board):
    return "".join(board)


def street_name(board_len):
    return _STREET_NAME.get(board_len, "river")




def process_node(rec, matchup, combos_oop, combos_ip, args, rng):
    """Generate all training records for one decision node.

    Returns list of (task_name, record_dict) pairs. No I/O.
    """
    out = []

    cd = rec.get("combo_data")
    if cd is None:
        return out

    actions = rec["actions"]
    n_act = len(actions)
    if n_act <= 1:
        return out

    board = rec["board"]
    street = street_name(len(board))
    board_str = board_to_str(board)
    to_act = rec["to_act"]
    hero_player = "BB" if to_act == "O" else "SB"
    actor_side = cd["oop"] if to_act == "O" else cd["ip"]
    hero_combos = combos_oop if to_act == "O" else combos_ip

    node_meta = {"matchup": matchup, "street": street, "board": board_str, "to_act": hero_player}

    # Per-node tasks
    ra, na = rec["range_advantage"], rec["nut_advantage"]
    out.append(("range_advantage", {
        "prompt": build_range_advantage_prompt(matchup, rec),
        "response": f"{ra}, {na}",
        "metadata": node_meta,
    }))

    range_strat = rec.get("range_strategy", [])
    if range_strat and len(range_strat) == n_act:
        rs_parts = " | ".join(f"{action_nl(actions[i])}: {range_strat[i]:.1%}" for i in range(n_act))
        out.append(("range_strategy", {
            "prompt": build_range_strategy_prompt(matchup, rec),
            "response": rs_parts,
            "metadata": node_meta,
        }))

    # Per-combo tasks
    actor_idxs = actor_side["idx"]
    actor_eqs  = actor_side["eq"]
    actor_ws   = actor_side["w"]
    actor_evs  = actor_side["ev"]
    strategy_list = cd["strategy"]

    if not actor_idxs:
        return out

    eligible = [j for j, w in enumerate(actor_ws) if w >= args.min_weight]
    if not eligible:
        return out

    sample_ws = [actor_ws[j] for j in eligible]
    n_sample = min(args.max_per_node, len(eligible))
    sampled = list(dict.fromkeys(rng.choices(eligible, weights=sample_ws, k=n_sample)))

    for j in sampled:
        if actor_ws[j] < args.min_weight:
            continue

        freqs = normalize_strategy(strategy_list[j], n_act)
        hero_hand = hero_combos[actor_idxs[j]]
        hero_eq   = actor_eqs[j]
        hero_ev   = actor_evs[j]
        combo_meta = {**node_meta, "hero_hand": hero_hand}

        action_prompt   = build_action_prompt(matchup, rec, hero_hand, hero_player, hero_eq, hero_ev)
        equity_prompt   = build_equity_prompt(matchup, rec, hero_hand, hero_player, "equity")
        strength_prompt = build_equity_prompt(matchup, rec, hero_hand, hero_player, "strength")

        chosen_idx = max(range(n_act), key=lambda i: freqs[i])
        zero_idxs  = [i for i, f in enumerate(freqs) if f < 0.01]

        if zero_idxs:
            rej = pick_hard_rejected(zero_idxs, actions, chosen_idx)
            out.append(("action_pref", {
                "prompt": action_prompt,
                "chosen":   f"<action>{action_nl(actions[chosen_idx])}</action>",
                "rejected": f"<action>{action_nl(actions[rej])}</action>",
                "metadata": {**combo_meta, "pair_type": "hard"},
            }))
        else:
            rej = pick_soft_rejected(freqs, actions, chosen_idx, min_gap=0.5)
            if rej is not None:
                out.append(("action_pref", {
                    "prompt": action_prompt,
                    "chosen":   f"<action>{action_nl(actions[chosen_idx])}</action>",
                    "rejected": f"<action>{action_nl(actions[rej])}</action>",
                    "metadata": {**combo_meta, "pair_type": "soft"},
                }))

        for i, freq in enumerate(freqs):
            if freq >= args.min_action_freq:
                out.append(("action_sft", {
                    "prompt": action_prompt,
                    "response": f"<action>{action_nl(actions[i])}</action>",
                    "metadata": {**combo_meta, "action_freq": round(freq, 3)},
                }))

        out.append(("equity", {
            "prompt": equity_prompt,
            "response": f"{hero_eq:.1%}",
            "metadata": combo_meta,
        }))

        out.append(("hand_strength", {
            "prompt": strength_prompt,
            "response": equity_to_strength(hero_eq),
            "metadata": {**combo_meta, "equity": round(hero_eq, 3)},
        }))

    return out


def generate(args):
    solves_dir = Path(args.solves_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    matchups = [m.strip() for m in args.matchups.split(",")]
    rng = random.Random(args.seed)

    task_files = {t: open(out_dir / f"{t}.jsonl", "w") for t in TASK_NAMES}
    stats = {t: 0 for t in TASK_NAMES}
    stats.update({"files_processed": 0, "files_skipped": 0, "nodes_skipped_street": 0})

    try:
        for matchup in matchups:
            matchup_dir = solves_dir / matchup
            if not matchup_dir.exists():
                print(f"[warn] missing: {matchup_dir}", file=sys.stderr)
                continue

            files = sorted(matchup_dir.glob("*.jsonl.zst"))
            pbar = tqdm(files, desc=matchup, unit="file", leave=True)
            for fpath in pbar:
                if args.max_files and stats["files_processed"] >= args.max_files:
                    break
                if args.sample_rate < 1.0 and rng.random() > args.sample_rate:
                    stats["files_skipped"] += 1
                    continue
                total = sum(stats[t] for t in TASK_NAMES)
                pbar.set_postfix(records=f"{total:,}")

                stats["files_processed"] += 1
                it = iter_jsonl_zst(fpath)
                try:
                    header = next(it)
                except StopIteration:
                    continue
                if header.get("schema") != "combo-v2":
                    continue

                combos_oop = header["combos_oop"]
                combos_ip  = header["combos_ip"]

                # Stratified street sampling: fill a per-street record budget from each file.
                # Files are DFS-ordered (flop root → turn nodes → river nodes), so we stream
                # forward until each street's budget is filled, then skip that street's nodes.
                # Stop reading when all budgets are filled — reads only a small prefix of each file.
                street_budgets = {3: args.flop_budget, 4: args.turn_budget, 5: args.river_budget}
                street_counts  = {3: 0, 4: 0, 5: 0}

                for rec in it:
                    # Stop early if all street budgets are filled
                    if all(street_counts[s] >= street_budgets[s] for s in street_budgets):
                        break

                    board_len = len(rec.get("board", []))
                    budget = street_budgets.get(board_len, 0)
                    if street_counts[board_len] >= budget:
                        # This street is already full; keep reading for other streets
                        stats["nodes_skipped_street"] += 1
                        continue

                    for task, record_dict in process_node(rec, matchup, combos_oop, combos_ip, args, rng):
                        if street_counts[board_len] >= budget:
                            break
                        task_files[task].write(json.dumps(record_dict) + "\n")
                        stats[task] += 1
                        street_counts[board_len] += 1

    finally:
        for f in task_files.values():
            f.close()

    print(f"\n=== Final Stats ===", file=sys.stderr)
    print(f"Files processed   : {stats['files_processed']}", file=sys.stderr)
    print(f"Files skipped     : {stats['files_skipped']}", file=sys.stderr)
    print(f"Nodes skipped     : {stats['nodes_skipped_street']} (street filter)", file=sys.stderr)
    print(f"", file=sys.stderr)
    total = sum(stats[t] for t in TASK_NAMES)
    street_dist = {}
    print(f"Records per task:", file=sys.stderr)
    for t in TASK_NAMES:
        print(f"  {t:20s}: {stats[t]:>10,}", file=sys.stderr)
    print(f"  {'TOTAL':20s}: {total:>10,}", file=sys.stderr)
    return stats


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--solves-dir", default="data/solves_combo",
                    help="Root dir with matchup subdirs of .jsonl.zst files")
    ap.add_argument("--out-dir", default="data/comprehensive_dataset",
                    help="Output directory; one .jsonl per task type")
    ap.add_argument("--matchups", default="SRP,3BP,4BP")
    ap.add_argument("--sample-rate", type=float, default=1.0,
                    help="Fraction of files to include (default 1.0)")
    ap.add_argument("--max-files", type=int, default=0,
                    help="Cap total files processed; 0 = unlimited")
    ap.add_argument("--min-weight", type=float, default=0.001,
                    help="Skip combos with reach weight below this (default 0.001)")
    ap.add_argument("--max-per-node", type=int, default=5,
                    help="Max combos sampled per decision node (default 5)")
    ap.add_argument("--min-action-freq", type=float, default=0.05,
                    help="Min action frequency to emit in action_sft (default 0.05)")
    ap.add_argument("--flop-budget", type=int, default=999999,
                    help="Max records to collect from flop nodes per file (default: unlimited). "
                         "For a 100K dataset across 5265 files, use 7.")
    ap.add_argument("--turn-budget", type=int, default=999999,
                    help="Max records from turn nodes per file (default: unlimited). "
                         "For a 100K dataset, use 7.")
    ap.add_argument("--river-budget", type=int, default=999999,
                    help="Max records from river nodes per file (default: unlimited). "
                         "For a 100K dataset, use 6.")
    ap.add_argument("--max-records-per-file", type=int, default=0,
                    help="Legacy cap (ignored when per-street budgets are set). "
                         "Use --flop-budget/--turn-budget/--river-budget instead.")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    generate(args)


if __name__ == "__main__":
    main()
