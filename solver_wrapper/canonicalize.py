"""Card encoding and suit-permutation canonicalization.

Card encoding (matches Rust postflop-solver): card = (rank << 2) | suit
  rank ∈ 0..13  (0=2, 1=3, …, 12=A)
  suit ∈ 0..4   (0=c, 1=d, 2=h, 3=s — alphabetical)
"""

from __future__ import annotations
from typing import Optional

_RANKS = "23456789TJQKA"
_SUITS = "cdhs"

# Verbatim from src/canonical.rs SUIT_PERMUTATIONS
SUIT_PERMUTATIONS: list[tuple[int, int, int, int]] = [
    (0,1,2,3),(0,1,3,2),(0,2,1,3),(0,2,3,1),(0,3,1,2),(0,3,2,1),
    (1,0,2,3),(1,0,3,2),(1,2,0,3),(1,2,3,0),(1,3,0,2),(1,3,2,0),
    (2,0,1,3),(2,0,3,1),(2,1,0,3),(2,1,3,0),(2,3,0,1),(2,3,1,0),
    (3,0,1,2),(3,0,2,1),(3,1,0,2),(3,1,2,0),(3,2,0,1),(3,2,1,0),
]


def card_from_str(s: str) -> int:
    """Parse a card string like 'Ah' or '2c' into a card integer."""
    s = s.strip()
    if len(s) != 2:
        raise ValueError(f"Invalid card string: {s!r}")
    rank_ch = s[0].upper()
    suit_ch = s[1].lower()
    if rank_ch not in _RANKS:
        raise ValueError(f"Invalid rank: {rank_ch!r} in {s!r}")
    if suit_ch not in _SUITS:
        raise ValueError(f"Invalid suit: {suit_ch!r} in {s!r}")
    return (_RANKS.index(rank_ch) << 2) | _SUITS.index(suit_ch)


def card_to_str(n: int) -> str:
    """Convert a card integer to a string like 'Ah' or '2c'."""
    return _RANKS[n >> 2] + _SUITS[n & 3]


def apply_suit_perm(card: int, perm: tuple[int, int, int, int]) -> int:
    """Relabel a card's suit using the given permutation."""
    return (card & ~3) | perm[card & 3]


def canonical_flop_and_perm(
    flop_ints: list[int],
) -> tuple[list[int], tuple[int, int, int, int]]:
    """Return (canonical_flop_ints, winning_perm).

    The canonical form is the lex-minimum sorted relabeling over all 24 suit
    permutations. The returned perm maps input suits → canonical suits, and must
    be applied consistently to turn, river, and hole cards so they all live in
    the same canonical suit space as the dataset.
    """
    best_cards: Optional[list[int]] = None
    best_perm: Optional[tuple] = None
    for perm in SUIT_PERMUTATIONS:
        relabeled = sorted(apply_suit_perm(c, perm) for c in flop_ints)
        if best_cards is None or relabeled < best_cards:
            best_cards = relabeled
            best_perm = perm
    return best_cards, best_perm  # type: ignore[return-value]


def canonical_flop_str(canonical_ints: list[int]) -> str:
    """Convert canonical flop card ints to the compact string used in filenames.

    e.g. [0, 1, 2] → '2c2d2h'  (sorted ascending by card int, no spaces)
    """
    return "".join(card_to_str(c) for c in sorted(canonical_ints))


def parse_hole_cards(s: str) -> tuple[int, int]:
    """Parse hole cards string like 'AhKd', 'Ah Kd', or 'Ah,Kd'."""
    s = s.strip().replace(",", " ").replace("  ", " ")
    if " " in s:
        parts = s.split()
    else:
        if len(s) != 4:
            raise ValueError(f"Cannot parse hole cards: {s!r}")
        parts = [s[:2], s[2:]]
    if len(parts) != 2:
        raise ValueError(f"Expected exactly 2 cards, got: {s!r}")
    return card_from_str(parts[0]), card_from_str(parts[1])


def validate_no_overlap(
    flop: list[int],
    turn: Optional[int],
    river: Optional[int],
    hole: Optional[tuple[int, int]],
) -> None:
    """Raise ValueError if any card appears more than once across all inputs."""
    seen: dict[int, str] = {}
    slots = [("flop[0]", flop[0]), ("flop[1]", flop[1]), ("flop[2]", flop[2])]
    if turn is not None:
        slots.append(("turn", turn))
    if river is not None:
        slots.append(("river", river))
    if hole is not None:
        slots.append(("hole[0]", hole[0]))
        slots.append(("hole[1]", hole[1]))
    for label, card in slots:
        if card in seen:
            raise ValueError(
                f"Duplicate card {card_to_str(card)!r}: appears as both "
                f"{seen[card]} and {label}"
            )
        seen[card] = label
