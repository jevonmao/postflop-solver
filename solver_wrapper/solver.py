"""Main Solver class — lazy LRU file cache and public query API."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Optional

from .canonicalize import (
    card_from_str,
    card_to_str,
    canonical_flop_and_perm,
    canonical_flop_str,
    apply_suit_perm,
    parse_hole_cards,
    validate_no_overlap,
)
from .lookup import (
    SolverLookupError,
    load_flop_records,
    navigate_history,
)
from .types import ActionFrequency, QueryResult, RangeStats

MATCHUPS = ("SRP", "3BP", "4BP")

CHIPS_PER_BB = 100


class Solver:
    """Query GTO strategies from a pre-solved postflop dataset.

    Usage::

        solver = Solver("data/solves")
        result = solver.query(
            board=["Ah", "Kc", "7d"],
            history=["check", "bet 33%"],
            matchup="SRP",
        )
        print(result)
        print(result.best_action())
        print(result.sample_action())

    Args:
        data_dir:   Path to the data/solves directory containing SRP/, 3BP/, 4BP/ subdirs.
        cache_size: Maximum number of JSONL files to keep loaded in RAM (LRU eviction).
                    Each file is 1–30 MB depending on matchup. Default 100.
    """

    def __init__(self, data_dir: str | Path, cache_size: int = 100) -> None:
        self._data_dir = Path(data_dir)
        self._cache_size = cache_size
        self._file_index: dict[tuple[str, str], Path] = {}
        self._cache: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._build_file_index()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query(
        self,
        board: list[str],
        history: list[str],
        matchup: str,
        hole_cards: Optional[str] = None,
    ) -> QueryResult:
        """Look up the GTO strategy for a given game state.

        Args:
            board:      3–5 card strings (flop, optionally turn and river).
                        Flop order does not matter. e.g. ["Ah","Kc","7d"] or
                        ["Ah","Kc","7d","Qs"] for flop+turn.
            history:    Flat list of betting actions taken so far (no deal events).
                        Accepted formats: "check", "fold", "call", "allin",
                        "bet 33%", "bet 16.5BB", "bet 1650", "raise to 33%",
                        "raise_to_11250", etc.
            matchup:    One of "SRP", "3BP", "4BP".
            hole_cards: Optional two-card string e.g. "AhKd". Used only for
                        validation (no board blocking). Range-level strategy is
                        always returned; a note is added to the result.

        Returns:
            QueryResult with strategy, range stats, and any fallback notes.

        Raises:
            ValueError:         Malformed input (bad card, duplicate card, etc.)
            SolverLookupError:  No matching record found for this board/history.
        """
        if matchup not in MATCHUPS:
            raise ValueError(f"matchup must be one of {MATCHUPS}, got {matchup!r}")
        if not (3 <= len(board) <= 5):
            raise ValueError(f"board must have 3–5 cards, got {len(board)}")

        # Parse and canonicalize
        flop_ints = [card_from_str(c) for c in board[:3]]
        turn_int: Optional[int] = card_from_str(board[3]) if len(board) >= 4 else None
        river_int: Optional[int] = card_from_str(board[4]) if len(board) == 5 else None

        hole: Optional[tuple[int, int]] = None
        if hole_cards is not None:
            hole = parse_hole_cards(hole_cards)

        canonical_ints, perm = canonical_flop_and_perm(flop_ints)
        canonical_turn = apply_suit_perm(turn_int, perm) if turn_int is not None else None
        canonical_river = apply_suit_perm(river_int, perm) if river_int is not None else None
        canonical_hole = (
            (apply_suit_perm(hole[0], perm), apply_suit_perm(hole[1], perm))
            if hole is not None else None
        )

        validate_no_overlap(canonical_ints, canonical_turn, canonical_river, canonical_hole)

        flop_str = canonical_flop_str(canonical_ints)
        index = self._load_flop(matchup, flop_str)

        record, notes = navigate_history(
            index, canonical_ints, canonical_turn, canonical_river, history
        )

        if hole_cards is not None:
            notes.append(
                f"Hole cards {hole_cards} accepted. "
                "Range-level strategy returned (per-combo data not in dataset)."
            )

        return self._build_result(record, notes)

    def query_best(
        self,
        board: list[str],
        history: list[str],
        matchup: str,
    ) -> str:
        """Convenience: return just the highest-frequency action label."""
        return self.query(board, history, matchup).best_action().action

    def available_matchups(self, board: list[str]) -> list[str]:
        """Return which matchups have a solved file for this board's canonical flop."""
        flop_ints = [card_from_str(c) for c in board[:3]]
        canonical_ints, _ = canonical_flop_and_perm(flop_ints)
        flop_str = canonical_flop_str(canonical_ints)
        return [m for m in MATCHUPS if (m, flop_str) in self._file_index]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_file_index(self) -> None:
        for matchup in MATCHUPS:
            matchup_dir = self._data_dir / matchup
            if not matchup_dir.exists():
                continue
            for p in matchup_dir.glob("*.jsonl"):
                # Filename: "0042_2c2d2h.jsonl" — flop_str is everything after first "_"
                flop_str = p.stem.split("_", 1)[1]
                self._file_index[(matchup, flop_str)] = p

    def _load_flop(self, matchup: str, flop_str: str) -> dict:
        key = (matchup, flop_str)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        path = self._file_index.get(key)
        if path is None:
            raise SolverLookupError(
                f"No dataset file for matchup={matchup!r} flop={flop_str!r}. "
                f"Canonical flop: {' '.join(flop_str[i:i+2] for i in range(0, 6, 2))}. "
                "Check that the solve has been generated for this spot."
            )

        index = load_flop_records(path)
        self._cache[key] = index
        self._cache.move_to_end(key)
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return index

    def _build_result(self, record: dict, notes: list[str]) -> QueryResult:
        pot = record["pot"]
        actions = []
        for label, freq in zip(record["actions"], record["range_strategy"]):
            actions.append(_build_action_frequency(label, freq, pot))

        return QueryResult(
            matchup=record["matchup"],
            board_canonical=record["board"],
            to_act="OOP" if record["to_act"] == "O" else "IP",
            pot_chips=pot,
            eff_stack_chips=record["eff_stack"],
            spr=record["spr"],
            actions=actions,
            range_advantage=record["range_advantage"],
            nut_advantage=record["nut_advantage"],
            oop_stats=_build_range_stats(record["oop"]),
            ip_stats=_build_range_stats(record["ip"]),
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------

def _build_action_frequency(label: str, freq: float, pot: int) -> ActionFrequency:
    if label == "check":
        return ActionFrequency("check", "check", None, None, None, freq)
    if label == "fold":
        return ActionFrequency("fold", "fold", None, None, None, freq)
    if label == "call":
        return ActionFrequency("call", "call", None, None, None, freq)

    if label.startswith("bet_"):
        chips = int(label[4:])
        return ActionFrequency(
            label, "bet", chips,
            chips / CHIPS_PER_BB,
            chips / pot if pot else None,
            freq,
        )
    if label.startswith("raise_to_"):
        chips = int(label[9:])
        return ActionFrequency(
            label, "raise", chips,
            chips / CHIPS_PER_BB,
            chips / pot if pot else None,
            freq,
        )
    if label.startswith("allin_"):
        chips = int(label[6:])
        return ActionFrequency(
            label, "allin", chips,
            chips / CHIPS_PER_BB,
            chips / pot if pot else None,
            freq,
        )
    # Fallback
    return ActionFrequency(label, "other", None, None, None, freq)


def _build_range_stats(d: dict) -> RangeStats:
    return RangeStats(
        range_eq=d["range_eq"],
        nut=d["nut"],
        strong=d["strong"],
        marginal=d["marginal"],
        weak=d["weak"],
        air=d["air"],
    )
