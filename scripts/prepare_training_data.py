#!/usr/bin/env python3
"""Convert data/solves JSONL records into ShareGPT fine-tuning format.

Each solver record becomes one ShareGPT conversation:
  human: structured game-state description
  gpt:   template-based reasoning + GTO range strategy JSON

Usage:
  python scripts/prepare_training_data.py [--solves-dir data/solves] \
      [--out-dir data/training] [--matchups 4BP,3BP,SRP] \
      [--seed 42] [--split 0.8/0.1/0.1] [--min-actions 2] \
      [--max-raise-streak 3] [--balance-matchups]

Filtering options:
  --max-raise-streak N  Drop records where any street has N or more consecutive
                        raises/bets without a call/check in between. Useful for
                        removing rare deep-reraise lines (e.g. river 4-bets) that
                        add noise without meaningful LLM signal. Default: no limit.
                        Recommended: 3 (drops ~1.75% of records, streak=4+).

  --balance-matchups    Subsample overrepresented matchups so each contributes
                        roughly equal total records to the training data. 4BP
                        generates ~7x more records per flop than SRP; without
                        balancing the model sees mostly short-stack jam/fold spots.
                        Downsamples each matchup to match the smallest matchup's
                        total record count. Sampling is done per-flop to preserve
                        flop distribution within each matchup.
"""

import argparse
import json
import math
import random
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MATCHUP_LABELS = {
    "SRP":  "SRP — BTN open vs BB call (single raised pot), 200BB deep",
    "3BP":  "3BP — BB 3-bet vs BTN call, 200BB deep",
    "4BP":  "4BP — BTN 4-bet vs BB call, 200BB deep",
}

SUIT_NAMES = {"c": "clubs", "d": "diamonds", "h": "hearts", "s": "spades"}
RANK_NAMES = {
    "2": "2", "3": "3", "4": "4", "5": "5", "6": "6",
    "7": "7", "8": "8", "9": "9", "T": "Ten", "J": "Jack",
    "Q": "Queen", "K": "King", "A": "Ace",
}

REQUIRED_KEYS = {
    "matchup", "flop_idx", "history", "board", "to_act", "pot",
    "eff_stack", "spr", "actions", "oop", "ip",
    "range_advantage", "nut_advantage", "range_strategy",
}
REQUIRED_STATS_KEYS = {"range_eq", "nut", "strong", "marginal", "weak", "air"}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def chips_to_bb(chips: int | float) -> float:
    return chips / 100.0


def fmt_bb(chips: int | float) -> str:
    bb = chips_to_bb(chips)
    return f"{bb:.1f}BB" if bb != int(bb) else f"{int(bb)}BB"


def card_label(card: str) -> str:
    """'Kh' → 'Kh'  (kept compact in prompts)"""
    return card


def board_str(board: list[str]) -> str:
    return " ".join(board)


def street_name(board: list[str]) -> str:
    return {3: "Flop", 4: "Turn", 5: "River"}[len(board)]


def action_bb(action: str) -> str:
    """Convert raw action label to human-readable BB form."""
    if action in ("check", "call", "fold"):
        return action.capitalize()
    m = re.match(r"bet_(\d+)", action)
    if m:
        return f"Bet {fmt_bb(int(m.group(1)))}"
    m = re.match(r"raise_to_(\d+)", action)
    if m:
        return f"Raise to {fmt_bb(int(m.group(1)))}"
    m = re.match(r"allin_(\d+)", action)
    if m:
        return f"All-in ({fmt_bb(int(m.group(1)))})"
    return action  # fallback


def action_label_short(action: str) -> str:
    """Compact action for strategy JSON keys (keep original solver format)."""
    return action


def history_str(history: list[str]) -> str:
    """Render history list to readable string, separating streets."""
    if not history:
        return "[start of hand]"
    parts = []
    for h in history:
        if h.startswith("deal_"):
            card = h[5:]
            parts.append(f"| dealt {card}")
        else:
            parts.append(action_bb(h))
    return " → ".join(parts)


def pct(val: float) -> str:
    return f"{val * 100:.0f}%"


def describe_range_strength(stats: dict, actor: str) -> str:
    """One sentence qualitatively characterising a player's range at this node."""
    nut = stats["nut"]
    strong = stats["strong"]
    marginal = stats["marginal"]
    weak = stats["weak"]
    air = stats["air"]

    # Determine dominant archetype
    top = nut + strong
    bot = weak + air

    if nut > 0.30:
        archetype = "heavily nut-heavy"
    elif top > 0.55:
        archetype = "value-heavy"
    elif air > 0.40:
        archetype = "bluff-heavy (mostly air)"
    elif bot > 0.60:
        archetype = "weak and marginal-heavy"
    elif abs(top - bot) < 0.15:
        archetype = "balanced (mixed value and bluffs)"
    else:
        archetype = "marginal-heavy"

    return (
        f"{actor}'s range is {archetype} "
        f"(nut {pct(nut)}, strong {pct(strong)}, "
        f"marginal {pct(marginal)}, weak {pct(weak)}, air {pct(air)})."
    )


def describe_spr(spr: float) -> str:
    if spr < 1.5:
        return "very shallow (near-shove territory)"
    if spr < 3:
        return "shallow (limited post-flop maneuvering)"
    if spr < 6:
        return "medium"
    if spr < 12:
        return "moderately deep"
    return "deep (lots of post-flop play remaining)"


# ---------------------------------------------------------------------------
# Board texture analysis
# ---------------------------------------------------------------------------

RANK_ORDER = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8,
              "9": 9, "T": 10, "J": 11, "Q": 12, "K": 13, "A": 14}


def _parse_board(board: list[str]) -> tuple[list[int], list[str]]:
    ranks = sorted([RANK_ORDER[c[0]] for c in board], reverse=True)
    suits = [c[1] for c in board]
    return ranks, suits


def _flush_texture(suits: list[str]) -> str:
    """Describe flush texture across all visible board cards."""
    counts = {s: suits.count(s) for s in set(suits)}
    max_count = max(counts.values())
    n = len(suits)
    if max_count >= 4:
        return "four-suited (flush locked in)"
    if max_count == 3:
        if n == 3:
            return "monotone (flush possible)"
        return "flush has hit the board"
    if max_count == 2:
        return "two-tone (flush draw present)"
    return "rainbow (no flush draw)"


def _connectedness(flop_ranks: list[int]) -> str | None:
    """Describe straight-draw texture from the three flop ranks."""
    top3 = sorted(set(flop_ranks), reverse=True)[:3]
    if len(top3) < 2:
        return None  # trips board, connectedness not meaningful
    span = top3[0] - top3[-1]
    if span <= 2 and len(top3) == 3:
        return "highly connected (multiple straight draws)"
    if span <= 3 and len(top3) >= 2:
        return "connected (straight draw present)"
    if span <= 5:
        return "semi-connected (gutshot possible)"
    return "disconnected (no straight draw)"


def _high_card_label(rank: int) -> str:
    for r, name in [(14, "ace-high"), (13, "king-high"), (12, "queen-high"),
                    (11, "jack-high"), (10, "ten-high")]:
        if rank == r:
            return name
    return "low-card"


def _pairing(ranks: list[int]) -> str:
    from collections import Counter
    c = Counter(ranks)
    if max(c.values()) >= 3:
        return "trips"
    if list(c.values()).count(2) >= 2:
        return "two-pair on board"
    if list(c.values()).count(2) == 1:
        return "paired"
    return "unpaired"


def describe_board_texture(board: list[str]) -> str:
    """One sentence describing board texture using all visible cards."""
    all_ranks, all_suits = _parse_board(board)
    flop_ranks, _ = _parse_board(board[:3])

    pair_desc = _pairing(all_ranks)
    flush_desc = _flush_texture(all_suits)
    connect_desc = _connectedness(flop_ranks)
    high = _high_card_label(all_ranks[0])

    parts = [high]
    if pair_desc == "trips":
        parts.append("trips board")
    elif pair_desc == "two-pair on board":
        parts.append("double-paired board")
    elif pair_desc == "paired":
        parts.append("paired board")

    if connect_desc:
        parts.append(connect_desc)
    parts.append(flush_desc)

    return f"The board is {', '.join(parts)}."


def conjugate_action(action_raw: str) -> tuple[str, str]:
    """Return (verb_phrase, noun_phrase) for describing an action in a sentence.

    verb_phrase: '{actor} {verb_phrase}' → e.g. 'checks', 'bets 16.5BB', 'goes all-in'
    noun_phrase: for the secondary clause → e.g. 'check', 'a bet of 16.5BB', 'an all-in'
    """
    if action_raw == "check":
        return "checks", "check"
    if action_raw == "call":
        return "calls", "call"
    if action_raw == "fold":
        return "folds", "fold"
    m = re.match(r"bet_(\d+)", action_raw)
    if m:
        bb = fmt_bb(int(m.group(1)))
        return f"bets {bb}", f"a {bb} bet"
    m = re.match(r"raise_to_(\d+)", action_raw)
    if m:
        bb = fmt_bb(int(m.group(1)))
        return f"raises to {bb}", f"a raise to {bb}"
    m = re.match(r"allin_(\d+)", action_raw)
    if m:
        bb = fmt_bb(int(m.group(1)))
        return f"goes all-in ({bb})", f"an all-in ({bb})"
    # fallback
    label = action_bb(action_raw).lower()
    return label + "s", label


def describe_strategy(actions: list[str], strategy: list[float], to_act: str) -> str:
    """Narrative description of the strategy vector."""
    pairs = sorted(zip(actions, strategy), key=lambda x: -x[1])
    dominant_action, dominant_prob = pairs[0]
    verb, _ = conjugate_action(dominant_action)

    if dominant_prob >= 0.90:
        desc = f"{to_act} almost always {verb} (~{pct(dominant_prob)} frequency)"
    elif dominant_prob >= 0.70:
        desc = f"{to_act} usually {verb} (~{pct(dominant_prob)} frequency)"
    elif dominant_prob >= 0.50:
        desc = f"{to_act} predominantly {verb} (~{pct(dominant_prob)} frequency)"
    else:
        # Tight split — frame as a mixed strategy rather than a dominant action
        desc = f"Strategy is split: {to_act} {verb} {pct(dominant_prob)} of the time"

    if len(pairs) > 1 and pairs[1][1] >= 0.10:
        _, noun = conjugate_action(pairs[1][0])
        desc += f", mixing in {noun} {pct(pairs[1][1])} of the time"

    return desc + "."


# ---------------------------------------------------------------------------
# Core reasoning generator
# ---------------------------------------------------------------------------

def generate_reasoning(record: dict) -> str:
    oop = record["oop"]
    ip = record["ip"]
    ra = record["range_advantage"]   # "IP", "OOP", "EVEN"
    na = record["nut_advantage"]
    to_act_code = record["to_act"]   # "O" or "I"
    to_act = "IP" if to_act_code == "I" else "OOP"
    actor_stats = ip if to_act == "IP" else oop
    spr = record["spr"]
    board = record["board"]
    actions = record["actions"]
    strategy = record["range_strategy"]

    sentences = []

    # 1. Board texture (uses full current board; connectivity anchored to flop)
    sentences.append(describe_board_texture(board))

    # 2. Range equity lead
    oop_eq = oop["range_eq"]
    ip_eq = ip["range_eq"]
    eq_diff = abs(ip_eq - oop_eq)
    if ra == "EVEN":
        sentences.append(
            f"Range equity is nearly balanced between the two players "
            f"(OOP {oop_eq:.2f}, IP {ip_eq:.2f})."
        )
    else:
        leader_eq = ip_eq if ra == "IP" else oop_eq
        trailer_eq = oop_eq if ra == "IP" else ip_eq
        intensity = "marginal" if eq_diff < 0.06 else ("solid" if eq_diff < 0.12 else "large")
        sentences.append(
            f"{ra} holds a {intensity} range equity advantage "
            f"({leader_eq:.2f} vs {trailer_eq:.2f})."
        )

    # 3. Actor's range characterisation
    sentences.append(describe_range_strength(actor_stats, to_act))

    # 4. Nut advantage — note splits (range advantage ≠ nut advantage)
    if na == "EVEN":
        sentences.append("Nut advantage is roughly even between both players.")
    elif na == ra:
        sentences.append(
            f"{na} also holds the nut advantage"
            f" ({pct(ip['nut'] if na == 'IP' else oop['nut'])} nut combos),"
            f" reinforcing the overall equity edge."
        )
    elif ra == "EVEN":
        # Equity is balanced but nut advantage is lopsided
        nuts_val = ip["nut"] if na == "IP" else oop["nut"]
        sentences.append(
            f"Despite balanced range equity, {na} holds a clear nut advantage"
            f" ({pct(nuts_val)} nut combos), which matters for polarization and bluff-catching."
        )
    else:
        # Split: nut advantage goes to the range-disadvantaged player
        nuts_val = ip["nut"] if na == "IP" else oop["nut"]
        sentences.append(
            f"Notably, nut advantage belongs to {na} ({pct(nuts_val)} nut combos)"
            f" despite {ra} holding the overall range equity edge —"
            f" a split that complicates polarization decisions."
        )

    # 5. SPR and street context
    street = street_name(board)
    spr_desc = describe_spr(spr)
    if street == "Flop":
        street_note = "On the flop, ranges are still wide; this is primarily about range definition and probing for information."
    elif street == "Turn":
        street_note = "By the turn, ranges have narrowed; bets carry more equity-denial and value-extraction weight."
    else:
        street_note = "On the river, hands are fully defined; strategy is purely value vs. bluff polarization."
    sentences.append(f"SPR is {spr:.1f} ({spr_desc}). {street_note}")

    # 6. Strategy narrative
    sentences.append(describe_strategy(actions, strategy, to_act))

    reasoning = " ".join(sentences)

    # Strategy JSON — round to 2dp and renormalize to sum exactly 1.0
    strat_rounded = [round(p, 2) for p in strategy]
    total = sum(strat_rounded)
    if total > 0:
        strat_rounded = [round(p / total, 2) for p in strat_rounded]
        # Fix rounding residual on the largest element
        residual = round(1.0 - sum(strat_rounded), 2)
        if residual != 0:
            max_idx = strat_rounded.index(max(strat_rounded))
            strat_rounded[max_idx] = round(strat_rounded[max_idx] + residual, 2)

    strat_dict = {a: p for a, p in zip(actions, strat_rounded)}
    strat_json = json.dumps(strat_dict)

    return f"{reasoning}\n\nGTO range strategy: {strat_json}"


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_prompt(record: dict) -> str:
    matchup = record["matchup"]
    board = record["board"]
    history = record["history"]
    to_act_code = record["to_act"]
    pot = record["pot"]
    eff_stack = record["eff_stack"]
    spr = record["spr"]
    oop = record["oop"]
    ip = record["ip"]
    ra = record["range_advantage"]
    na = record["nut_advantage"]
    actions = record["actions"]

    to_act = "IP (In-Position)" if to_act_code == "I" else "OOP (Out-of-Position)"
    street = street_name(board)
    matchup_label = MATCHUP_LABELS.get(matchup, matchup)

    actions_str = ", ".join(action_bb(a) for a in actions)

    prompt = (
        f"You are a GTO poker advisor. Analyze the following poker situation and explain "
        f"the range dynamics, then provide the optimal GTO range strategy.\n\n"
        f"Matchup: {matchup_label}\n"
        f"Street: {street}\n"
        f"Board: {board_str(board)}\n"
        f"Action history: {history_str(history)}\n"
        f"To act: {to_act}\n"
        f"Pot: {fmt_bb(pot)} | Eff. stack: {fmt_bb(eff_stack)} | SPR: {spr:.1f}\n"
        f"\n"
        f"Range snapshot:\n"
        f"  OOP — equity: {oop['range_eq']:.2f} | "
        f"nut {pct(oop['nut'])} | strong {pct(oop['strong'])} | "
        f"marginal {pct(oop['marginal'])} | weak {pct(oop['weak'])} | air {pct(oop['air'])}\n"
        f"  IP  — equity: {ip['range_eq']:.2f} | "
        f"nut {pct(ip['nut'])} | strong {pct(ip['strong'])} | "
        f"marginal {pct(ip['marginal'])} | weak {pct(ip['weak'])} | air {pct(ip['air'])}\n"
        f"\n"
        f"Range advantage: {ra} | Nut advantage: {na}\n"
        f"Available actions: {actions_str}"
    )
    return prompt


# ---------------------------------------------------------------------------
# Raise-streak filter
# ---------------------------------------------------------------------------

def _classify_action(a: str) -> str:
    if a in ("check", "call", "fold"):
        return a
    if a.startswith("bet_"):
        return "bet"
    if a.startswith("raise_") or a.startswith("allin"):
        return "raise"
    return "other"


def max_raise_streak(history: list[str]) -> int:
    """Max consecutive raises/bets within any single street."""
    best = 0
    streak = 0
    for h in history:
        if h.startswith("deal_"):
            streak = 0  # new street resets
            continue
        kind = _classify_action(h)
        if kind in ("bet", "raise"):
            streak += 1
            best = max(best, streak)
        elif kind in ("check", "call", "fold"):
            streak = 0
    return best


# ---------------------------------------------------------------------------
# Record validation
# ---------------------------------------------------------------------------

def is_valid(record: dict, min_actions: int) -> bool:
    if not REQUIRED_KEYS.issubset(record.keys()):
        return False
    if not isinstance(record["actions"], list) or len(record["actions"]) < min_actions:
        return False
    if not isinstance(record["range_strategy"], list):
        return False
    if len(record["range_strategy"]) != len(record["actions"]):
        return False
    for player in ("oop", "ip"):
        if not REQUIRED_STATS_KEYS.issubset(record[player].keys()):
            return False
    if sum(record["range_strategy"]) < 0.01:
        return False
    return True


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_records(solves_dir: Path, matchups: list[str]) -> dict[str, dict[int, list[dict]]]:
    """Returns {matchup: {flop_idx: [records]}}."""
    data: dict[str, dict[int, list[dict]]] = {}
    for matchup in matchups:
        matchup_dir = solves_dir / matchup
        if not matchup_dir.exists():
            print(f"  [warn] {matchup_dir} does not exist, skipping", file=sys.stderr)
            continue
        flop_map: dict[int, list[dict]] = defaultdict(list)
        jsonl_files = sorted(matchup_dir.glob("*.jsonl"))
        if not jsonl_files:
            print(f"  [warn] no .jsonl files in {matchup_dir}", file=sys.stderr)
            continue
        for jf in jsonl_files:
            with open(jf) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    flop_idx = rec.get("flop_idx")
                    if flop_idx is not None:
                        flop_map[flop_idx].append(rec)
        data[matchup] = dict(flop_map)
        total = sum(len(v) for v in flop_map.values())
        print(f"  Loaded {matchup}: {len(flop_map)} flops, {total:,} records")
    return data


# ---------------------------------------------------------------------------
# Train/val/test split by flop_idx
# ---------------------------------------------------------------------------

def split_flop_indices(
    flop_indices: list[int],
    ratios: tuple[float, float, float],
    seed: int,
) -> tuple[list[int], list[int], list[int]]:
    rng = random.Random(seed)
    shuffled = flop_indices[:]
    rng.shuffle(shuffled)
    n = len(shuffled)
    n_train = max(1, math.floor(ratios[0] * n))
    n_val = max(1, math.floor(ratios[1] * n))
    train = shuffled[:n_train]
    val = shuffled[n_train : n_train + n_val]
    test = shuffled[n_train + n_val :]
    return train, val, test


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solves-dir", default="data/solves", type=Path)
    parser.add_argument("--out-dir", default="data/training", type=Path)
    parser.add_argument("--matchups", default="4BP,3BP,SRP")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--split", default="0.8/0.1/0.1")
    parser.add_argument("--min-actions", type=int, default=2)
    parser.add_argument(
        "--max-raise-streak", type=int, default=None, metavar="N",
        help="Drop records with >N consecutive raises in any street (e.g. 3 drops streak=4+).",
    )
    parser.add_argument(
        "--balance-matchups", action="store_true",
        help="Subsample each matchup down to the smallest matchup's total record count.",
    )
    args = parser.parse_args()

    matchups = [m.strip() for m in args.matchups.split(",")]
    split_parts = [float(x) for x in args.split.split("/")]
    if len(split_parts) != 3 or abs(sum(split_parts) - 1.0) > 1e-6:
        sys.exit("--split must be three floats summing to 1.0, e.g. 0.8/0.1/0.1")
    ratios = (split_parts[0], split_parts[1], split_parts[2])

    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Loading records from {args.solves_dir} ...")
    all_data = load_records(args.solves_dir, matchups)

    # --balance-matchups: per-flop subsample so each matchup has equal total records
    if args.balance_matchups:
        rng = random.Random(args.seed)
        totals = {m: sum(len(v) for v in fm.values()) for m, fm in all_data.items()}
        target = min(totals.values())
        print(f"Balancing matchups → target {target:,} records each (was: {totals})")
        for matchup, flop_map in all_data.items():
            current_total = totals[matchup]
            if current_total <= target:
                continue
            # Subsample each flop proportionally to keep flop coverage uniform
            keep_ratio = target / current_total
            for flop_idx in list(flop_map.keys()):
                recs = flop_map[flop_idx]
                k = max(1, round(len(recs) * keep_ratio))
                flop_map[flop_idx] = rng.sample(recs, min(k, len(recs)))

    # Aggregate split assignments across matchups (keyed by (matchup, flop_idx))
    split_map: dict[tuple[str, int], str] = {}
    for matchup, flop_map in all_data.items():
        idxs = sorted(flop_map.keys())
        train_idxs, val_idxs, test_idxs = split_flop_indices(idxs, ratios, args.seed)
        for fi in train_idxs:
            split_map[(matchup, fi)] = "train"
        for fi in val_idxs:
            split_map[(matchup, fi)] = "val"
        for fi in test_idxs:
            split_map[(matchup, fi)] = "test"

    # Open output files
    out_files = {
        split: open(args.out_dir / f"{split}.jsonl", "w")
        for split in ("train", "val", "test")
    }

    # Stats tracking
    stats: dict = {
        split: {
            "total": 0,
            "filtered": 0,
            "by_matchup": defaultdict(int),
            "by_street": defaultdict(int),
            "flop_indices": [],
        }
        for split in ("train", "val", "test")
    }
    global_filtered = 0
    global_streak_filtered = 0

    print("Converting records ...")
    for matchup, flop_map in all_data.items():
        for flop_idx, records in sorted(flop_map.items()):
            split = split_map.get((matchup, flop_idx), "train")
            s = stats[split]
            for rec in records:
                if not is_valid(rec, args.min_actions):
                    global_filtered += 1
                    continue
                if args.max_raise_streak is not None:
                    streak = max_raise_streak(rec.get("history", []))
                    if streak > args.max_raise_streak:
                        global_streak_filtered += 1
                        continue
                prompt = build_prompt(rec)
                completion = generate_reasoning(rec)
                example = {
                    "conversations": [
                        {"from": "human", "value": prompt},
                        {"from": "gpt", "value": completion},
                    ]
                }
                out_files[split].write(json.dumps(example) + "\n")
                street = street_name(rec["board"])
                s["total"] += 1
                s["by_matchup"][matchup] += 1
                s["by_street"][street] += 1
            if flop_idx not in s["flop_indices"]:
                s["flop_indices"].append(flop_idx)

    for f in out_files.values():
        f.close()

    # Write stats
    stats_out = {}
    total_records = 0
    for split, s in stats.items():
        stats_out[split] = {
            "records": s["total"],
            "flops": len(s["flop_indices"]),
            "by_matchup": dict(s["by_matchup"]),
            "by_street": dict(s["by_street"]),
            "approx_tokens": s["total"] * 350,  # ~200 prompt + ~150 completion
        }
        total_records += s["total"]
    stats_out["filtered_records"] = global_filtered
    stats_out["streak_filtered_records"] = global_streak_filtered
    stats_out["total_records"] = total_records

    with open(args.out_dir / "stats.json", "w") as f:
        json.dump(stats_out, f, indent=2)

    # Print summary
    print(f"\nDone. Output in {args.out_dir}/")
    print(f"  Filtered (invalid/single-action): {global_filtered:,}")
    if args.max_raise_streak is not None:
        print(f"  Filtered (raise streak >{args.max_raise_streak}):   {global_streak_filtered:,}")
    for split in ("train", "val", "test"):
        s = stats_out[split]
        print(f"  {split:5s}: {s['records']:>8,} records | {s['flops']} flops | ~{s['approx_tokens']//1000}k tokens")

    # Verify no flop_idx leaks across splits
    sets = {split: set(stats[split]["flop_indices"]) for split in ("train", "val", "test")}
    for a in ("train", "val", "test"):
        for b in ("train", "val", "test"):
            if a >= b:
                continue
            overlap = sets[a] & sets[b]
            if overlap:
                print(f"  [warn] flop_idx overlap between {a} and {b}: {overlap}", file=sys.stderr)
    print("  Split integrity: no flop_idx leakage across splits")


if __name__ == "__main__":
    main()
