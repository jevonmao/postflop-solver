from __future__ import annotations

from dataclasses import dataclass as _dataclass

@_dataclass
class SolveResult:
    matchup: str
    flop: list[str]
    solve_ms: int
    exploitability_pct: float

    def __str__(self) -> str:
        flop_str = "".join(self.flop)
        return (
            f"Solved {self.matchup} {flop_str} in {self.solve_ms/1000:.2f}s "
            f"(exploitability: {self.exploitability_pct:.2f}%)"
        )



import random as _random
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RangeStats:
    range_eq: float
    nut: float
    strong: float
    marginal: float
    weak: float
    air: float


@dataclass
class ActionFrequency:
    action: str                     # raw dataset label e.g. "bet_1650"
    action_type: str                # "check"|"bet"|"raise"|"call"|"fold"|"allin"
    amount_chips: Optional[int]     # None for check/fold/call
    amount_bb: Optional[float]
    amount_pct_pot: Optional[float]
    frequency: float                # 0.0–1.0

    def __str__(self) -> str:
        if self.amount_bb is not None:
            return f"{self.action_type} {self.amount_bb:.1f}BB ({self.frequency*100:.0f}%)"
        return f"{self.action_type} ({self.frequency*100:.0f}%)"


@dataclass
class QueryResult:
    matchup: str
    board_canonical: list[str]
    to_act: str                     # "OOP" | "IP"
    pot_chips: int
    eff_stack_chips: int
    spr: float
    actions: list[ActionFrequency]
    range_advantage: str            # "OOP" | "IP" | "EVEN"
    nut_advantage: str
    oop_stats: RangeStats
    ip_stats: RangeStats
    notes: list[str] = field(default_factory=list)

    def best_action(self) -> ActionFrequency:
        return max(self.actions, key=lambda a: a.frequency)

    def sample_action(self, rng: Optional[_random.Random] = None) -> ActionFrequency:
        r = rng or _random
        weights = [a.frequency for a in self.actions]
        return r.choices(self.actions, weights=weights, k=1)[0]

    def __str__(self) -> str:
        board_str = " ".join(self.board_canonical)
        strat = "  ".join(str(a) for a in self.actions)
        lines = [
            f"[{self.matchup}] {board_str}  pot={self.pot_chips/100:.1f}BB  "
            f"SPR={self.spr:.1f}  to_act={self.to_act}",
            f"  range_adv={self.range_advantage}  nut_adv={self.nut_advantage}",
            f"  strategy: {strat}",
        ]
        if self.notes:
            for note in self.notes:
                lines.append(f"  [note] {note}")
        return "\n".join(lines)
