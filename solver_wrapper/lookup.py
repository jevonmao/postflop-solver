"""Index building, history traversal, action matching, and runout fallback."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from .canonicalize import card_from_str, card_to_str, apply_suit_perm

# (flop_actions, turn_card, turn_actions, river_card, river_actions)
HistoryKey = tuple[tuple[str, ...], Optional[str], tuple[str, ...], Optional[str], tuple[str, ...]]

ROOT_KEY: HistoryKey = ((), None, (), None, ())


class SolverLookupError(Exception):
    pass


# ---------------------------------------------------------------------------
# JSONL loading
# ---------------------------------------------------------------------------

def load_flop_records(path: Path) -> dict[HistoryKey, dict]:
    """Load all records from one JSONL file into a history-keyed index."""
    index: dict[HistoryKey, dict] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            key = _history_to_key(rec["history"])
            index[key] = rec
    return index


def _history_to_key(history: list[str]) -> HistoryKey:
    """Split a flat history list (with deal_X tokens) into a structured key."""
    flop_a: list[str] = []
    turn_a: list[str] = []
    river_a: list[str] = []
    turn_card: Optional[str] = None
    river_card: Optional[str] = None
    street = "flop"
    for tok in history:
        if tok.startswith("deal_"):
            card_str = tok[5:]  # strip "deal_"
            if street == "flop":
                turn_card = card_str
                street = "turn"
            elif street == "turn":
                river_card = card_str
                street = "river"
        else:
            if street == "flop":
                flop_a.append(tok)
            elif street == "turn":
                turn_a.append(tok)
            else:
                river_a.append(tok)
    return (tuple(flop_a), turn_card, tuple(turn_a), river_card, tuple(river_a))


# ---------------------------------------------------------------------------
# Action parsing and matching
# ---------------------------------------------------------------------------

def parse_action_str(s: str) -> tuple[str, Optional[float], str]:
    """Parse a user action string → (action_type, raw_value, unit).

    Supported formats:
      "check", "x"                       → ("check", None, "")
      "fold", "f"                        → ("fold", None, "")
      "call"                             → ("call", None, "")
      "allin", "all in", "all-in", "a"  → ("allin", None, "")
      "bet_1650", "bet 1650"             → ("bet", 1650.0, "chips")
      "bet 33%"                          → ("bet", 33.0, "pct")
      "bet 16.5bb", "bet 16.5BB"         → ("bet", 16.5, "bb")
      "raise_to_11250", "raise to 11250" → ("raise", 11250.0, "chips")
      "raise to 33%"                     → ("raise", 33.0, "pct")
    """
    s = s.strip().lower()

    # Aliases
    if s in ("check", "x"):
        return ("check", None, "")
    if s in ("fold", "f"):
        return ("fold", None, "")
    if s == "call":
        return ("call", None, "")
    if s in ("allin", "all in", "all-in", "a", "shove"):
        return ("allin", None, "")

    # Bet patterns
    m = re.fullmatch(r"bet[_ ]?([\d.]+)(%|bb)?", s)
    if m:
        val = float(m.group(1))
        unit = (m.group(2) or "chips").lower()
        return ("bet", val, unit)

    # Raise patterns
    m = re.fullmatch(r"raise(?:[_ ]?to)?[_ ]?([\d.]+)(%|bb)?", s)
    if m:
        val = float(m.group(1))
        unit = (m.group(2) or "chips").lower()
        return ("raise", val, unit)

    # Dataset-style labels (already normalised)
    if s.startswith("bet_"):
        return ("bet", float(s[4:]), "chips")
    if s.startswith("raise_to_"):
        return ("raise", float(s[9:]), "chips")
    if s.startswith("allin_"):
        return ("allin", None, "")

    raise ValueError(
        f"Unrecognised action: {s!r}. "
        "Expected one of: check, fold, call, allin, "
        "'bet N', 'bet N%', 'bet NBB', 'raise to N', 'raise to N%'"
    )


def _to_chips(val: float, unit: str, pot: int) -> int:
    if unit == "pct" or unit == "%":
        return round(val / 100.0 * pot)
    if unit == "bb":
        return round(val * 100)
    return round(val)


def match_action_to_available(
    user_action: str,
    available_actions: list[str],
    current_pot: int,
) -> tuple[str, bool, str]:
    """Match a user action to the best available dataset action.

    Returns (matched_label, was_exact, note_str).
    """
    atype, val, unit = parse_action_str(user_action)

    if atype == "check":
        if "check" in available_actions:
            return ("check", True, "")
        raise SolverLookupError(
            f"'check' not available at this node; available: {available_actions}"
        )

    if atype == "fold":
        if "fold" in available_actions:
            return ("fold", True, "")
        raise SolverLookupError(
            f"'fold' not available at this node; available: {available_actions}"
        )

    if atype == "call":
        if "call" in available_actions:
            return ("call", True, "")
        raise SolverLookupError(
            f"'call' not available at this node; available: {available_actions}"
        )

    if atype == "allin":
        allin_actions = [a for a in available_actions if a.startswith("allin_")]
        if allin_actions:
            return (allin_actions[0], True, "")
        raise SolverLookupError(
            f"'allin' not available at this node; available: {available_actions}"
        )

    # bet or raise — nearest by chip distance
    requested_chips = _to_chips(val, unit, current_pot)
    prefix = "bet_" if atype == "bet" else "raise_to_"
    candidates = [a for a in available_actions if a.startswith(prefix)]

    # Also allow falling back to allin if no bet/raise candidates
    if not candidates:
        allin_actions = [a for a in available_actions if a.startswith("allin_")]
        if allin_actions:
            note = (
                f"No {atype} actions available; using all-in instead "
                f"({allin_actions[0]})"
            )
            return (allin_actions[0], False, note)
        raise SolverLookupError(
            f"No {atype} or allin actions available; available: {available_actions}"
        )

    def chip_amount(label: str) -> int:
        return int(label.split("_")[-1])

    best = min(candidates, key=lambda a: abs(chip_amount(a) - requested_chips))
    best_chips = chip_amount(best)
    exact = (best_chips == requested_chips)
    note = "" if exact else (
        f"{atype} {val}{unit} (≈{requested_chips} chips) → {best} "
        f"(nearest available, {best_chips/current_pot*100:.0f}% pot)"
    )
    return (best, exact, note)


# ---------------------------------------------------------------------------
# Turn/river card fallback
# ---------------------------------------------------------------------------

def _available_deal_cards(
    index: dict[HistoryKey, dict],
    partial_key: HistoryKey,
    looking_for: str,  # "turn" or "river"
) -> set[int]:
    """Scan the index for all deal cards under a given prefix."""
    fa, tc, ta, rc, ra = partial_key
    result: set[int] = set()
    for key in index:
        kfa, ktc, kta, krc, kra = key
        if looking_for == "turn":
            if kfa == fa and ktc is not None:
                try:
                    result.add(card_from_str(ktc))
                except ValueError:
                    pass
        else:  # river
            if kfa == fa and ktc == tc and kta == ta and krc is not None:
                try:
                    result.add(card_from_str(krc))
                except ValueError:
                    pass
    return result


def find_nearest_sampled_card(
    requested: int,
    available: set[int],
    flop_ints: list[int],
) -> tuple[int, str]:
    """Find the closest sampled card to the requested canonical card.

    Priority: exact → same rank → adjacent rank same suit-class → adjacent rank.
    Returns (nearest_card_int, note_str).
    """
    if requested in available:
        return (requested, "")

    req_rank = requested >> 2
    req_suit = requested & 3
    flop_suits = {c & 3 for c in flop_ints}

    def suit_class(suit: int) -> str:
        return "flush" if suit in flop_suits else "offsuit"

    req_suit_class = suit_class(req_suit)

    def score(card: int) -> tuple[int, int, int]:
        rank = card >> 2
        suit = card & 3
        rank_dist = abs(rank - req_rank)
        suit_class_match = 0 if suit_class(suit) == req_suit_class else 1
        suit_dist = abs(suit - req_suit)
        return (rank_dist, suit_class_match, suit_dist)

    best = min(available, key=score)
    note = (
        f"{card_to_str(requested)} not sampled for this line; "
        f"using {card_to_str(best)} (nearest by rank/suit)"
    )
    return (best, note)


# ---------------------------------------------------------------------------
# Main traversal
# ---------------------------------------------------------------------------

def navigate_history(
    index: dict[HistoryKey, dict],
    flop_ints: list[int],
    canonical_turn: Optional[int],
    canonical_river: Optional[int],
    user_history: list[str],
) -> tuple[dict, list[str]]:
    """Traverse the index using a flat user action history.

    Deal events are inserted automatically at street boundaries based on the
    canonical turn/river cards provided. Returns (record, notes).

    Raises SolverLookupError if the path cannot be found.
    """
    notes: list[str] = []

    # Current structured key position
    flop_a: list[str] = []
    turn_card: Optional[str] = None
    turn_a: list[str] = []
    river_card: Optional[str] = None
    river_a: list[str] = []

    def current_key() -> HistoryKey:
        return (
            tuple(flop_a), turn_card,
            tuple(turn_a), river_card,
            tuple(river_a),
        )

    def current_street() -> str:
        if river_card is not None:
            return "river"
        if turn_card is not None:
            return "turn"
        return "flop"

    def append_action(action: str) -> None:
        s = current_street()
        if s == "flop":
            flop_a.append(action)
        elif s == "turn":
            turn_a.append(action)
        else:
            river_a.append(action)

    def pop_action() -> None:
        s = current_street()
        if s == "flop":
            flop_a.pop()
        elif s == "turn":
            turn_a.pop()
        else:
            river_a.pop()

    # Check root exists
    if ROOT_KEY not in index:
        raise SolverLookupError("Root node not found in dataset index.")

    for user_act in user_history:
        key = current_key()
        rec = index.get(key)
        if rec is None:
            raise SolverLookupError(
                f"No record found at history key {key}. "
                "The action sequence may not exist in the dataset."
            )

        matched, exact, note = match_action_to_available(
            user_act, rec["actions"], rec["pot"]
        )
        if note:
            notes.append(note)

        append_action(matched)
        new_key = current_key()

        if new_key in index:
            continue

        # Not found — action appended but resulting key isn't a decision node.
        # This happens when the appended action is the last one in a street,
        # meaning a deal event (turn or river) should follow.
        # DO NOT pop the action; instead try inserting deal events.

        # Try inserting turn card
        if turn_card is None and canonical_turn is not None:
            avail = _available_deal_cards(index, current_key(), "turn")
            if avail:
                best_turn, fallback_note = find_nearest_sampled_card(
                    canonical_turn, avail, flop_ints
                )
                if fallback_note:
                    notes.append(fallback_note)
                turn_card = card_to_str(best_turn)
                if current_key() in index:
                    continue
                turn_card = None  # reset on failure

        # Try inserting river card
        if river_card is None and canonical_river is not None and turn_card is not None:
            partial: HistoryKey = (tuple(flop_a), turn_card, tuple(turn_a), None, ())
            avail = _available_deal_cards(index, partial, "river")
            if avail:
                best_river, fallback_note = find_nearest_sampled_card(
                    canonical_river, avail, flop_ints
                )
                if fallback_note:
                    notes.append(fallback_note)
                river_card = card_to_str(best_river)
                if current_key() in index:
                    continue
                river_card = None  # reset on failure

        raise SolverLookupError(
            f"No record found after action {matched!r} at key {current_key()}. "
            "This action sequence may not exist in the dataset, or "
            "a turn/river card may be needed in the board parameter."
        )

    # After consuming all user actions, possibly insert remaining deal events
    # so the final key points to a decision node (not a street boundary).
    key = current_key()

    # Try inserting turn if not yet dealt and turn card is known
    if key not in index and turn_card is None and canonical_turn is not None:
        avail = _available_deal_cards(index, key, "turn")
        if avail:
            best_turn, fallback_note = find_nearest_sampled_card(
                canonical_turn, avail, flop_ints
            )
            if fallback_note:
                notes.append(fallback_note)
            turn_card = card_to_str(best_turn)
            key = current_key()

    # Try inserting river if not yet dealt and river card is known
    if key not in index and river_card is None and canonical_river is not None:
        partial: HistoryKey = (tuple(flop_a), turn_card, tuple(turn_a), None, ())
        avail = _available_deal_cards(index, partial, "river")
        if avail:
            best_river, fallback_note = find_nearest_sampled_card(
                canonical_river, avail, flop_ints
            )
            if fallback_note:
                notes.append(fallback_note)
            river_card = card_to_str(best_river)
            key = current_key()

    rec = index.get(key)
    if rec is None:
        raise SolverLookupError(
            f"No record found at final key {key}. "
            "The board/history combination may not exist in the dataset."
        )
    return rec, notes
