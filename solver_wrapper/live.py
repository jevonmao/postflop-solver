"""LiveSolver — drives the Rust solver in real-time via subprocess."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .lookup import SolverLookupError, match_action_to_available
from .solver import _build_action_frequency, _build_range_stats
from .types import QueryResult, RangeStats, SolveResult

_DEFAULT_BINARY_PATHS = [
    Path("target/release/examples/live_solver"),
    Path("target/release/examples/live_solver.exe"),
]


class LiveSolverError(Exception):
    pass


class LiveSolver:
    """Real-time GTO solver that runs CFR live for any board.

    Usage::

        solver = LiveSolver()
        result = solver.solve("4BP", ["2c", "2d", "2h"])
        print(result)   # "Solved 4BP 2c2d2h in 1.45s (exploitability: 1.83%)"

        r = solver.query()   # flop root
        print(r)
        print(r.best_action())

        r = solver.query(
            flop_actions=["check"],
            turn="3h",
            turn_actions=["check", "bet 75%"],   # % and BB accepted
        )
        print(r.best_action())

        solver.close()

    Args:
        binary: path to the compiled live_solver binary.
                Defaults to target/release/examples/live_solver.
    """

    def __init__(self, binary: str | Path | None = None) -> None:
        path = Path(binary) if binary else self._find_binary()
        if not path.exists():
            raise FileNotFoundError(
                f"live_solver binary not found at {path}. "
                "Run: cargo build --release --example live_solver"
            )
        self._proc = subprocess.Popen(
            [str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._current_matchup: Optional[str] = None
        self._current_flop: Optional[list[str]] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def solve(self, matchup: str, board: list[str]) -> SolveResult:
        """Solve a new flop. Blocks until CFR converges (~1.5s 4BP, ~12s 3BP, ~140s SRP).

        Args:
            matchup: "SRP", "3BP", or "4BP"
            board:   3 flop card strings e.g. ["Kh","7d","2c"]
        """
        flop_str = "".join(board[:3])
        print(f"Solving {matchup} {flop_str} ...", end=" ", flush=True)
        resp = self._send({"cmd": "solve", "matchup": matchup, "flop": flop_str})
        self._current_matchup = matchup
        self._current_flop = list(board[:3])
        print(f"done in {resp['solve_ms']/1000:.2f}s  "
              f"(exploit {resp['exploitability_pct']:.2f}%)")
        return SolveResult(
            matchup=matchup,
            flop=list(board[:3]),
            solve_ms=int(resp["solve_ms"]),
            exploitability_pct=float(resp["exploitability_pct"]),
        )

    def query(
        self,
        flop_actions: list[str] = [],
        turn: Optional[str] = None,
        turn_actions: list[str] = [],
        river: Optional[str] = None,
        river_actions: list[str] = [],
        hole_cards: Optional[str] = None,
    ) -> QueryResult:
        """Query the GTO strategy at any node in the current solved game.

        Args:
            flop_actions:  Betting actions taken on the flop (no deal events).
            turn:          Turn card string e.g. "3h", or None if not yet dealt.
            turn_actions:  Betting actions taken on the turn.
            river:         River card string, or None.
            river_actions: Betting actions taken on the river.
            hole_cards:    Optional e.g. "AhKd" — validates non-blocking, adds a note.

        Action formats accepted: "check", "fold", "call", "allin",
        "bet_1650", "bet 1650", "bet 33%", "bet 16.5BB",
        "raise_to_11250", "raise to 11250".

        Returns QueryResult with strategy, range stats, and any normalization notes.
        Raises LiveSolverError if the Rust binary returns an error.
        """
        notes: list[str] = []

        # Normalize each street's actions step-by-step (needed to resolve pot-% bets)
        resolved_fa = self._normalize_actions(
            flop_actions, [], None, [], None, [], notes
        )
        resolved_ta = self._normalize_actions(
            turn_actions, resolved_fa, turn, [], None, [], notes
        ) if turn is not None else []
        resolved_ra = self._normalize_actions(
            river_actions, resolved_fa, turn, resolved_ta, river, [], notes
        ) if river is not None else []

        resp = self._send({
            "cmd": "query",
            "flop_actions": resolved_fa,
            "turn": turn,
            "turn_actions": resolved_ta,
            "river": river,
            "river_actions": resolved_ra,
        })

        if hole_cards:
            notes.append(
                f"Hole cards {hole_cards} accepted. "
                "Range-level strategy returned (live solver does not expose per-combo data)."
            )

        return self._build_result(resp, notes)

    def close(self) -> None:
        """Shut down the solver subprocess."""
        try:
            self._send_raw({"cmd": "quit"})
        except Exception:
            pass
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_binary() -> Path:
        for p in _DEFAULT_BINARY_PATHS:
            if p.exists():
                return p
        return _DEFAULT_BINARY_PATHS[0]

    def _send_raw(self, cmd: dict) -> None:
        line = json.dumps(cmd) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

    def _send(self, cmd: dict) -> dict:
        self._send_raw(cmd)
        line = self._proc.stdout.readline()
        if not line:
            raise LiveSolverError("Solver process closed unexpectedly.")
        resp = json.loads(line)
        if resp.get("status") != "ok":
            raise LiveSolverError(resp.get("msg", "Unknown error from solver"))
        return resp

    def _normalize_actions(
        self,
        user_actions: list[str],
        flop_actions: list[str],
        turn: Optional[str],
        turn_actions: list[str],
        river: Optional[str],
        river_actions: list[str],
        notes: list[str],
    ) -> list[str]:
        """Resolve user action strings to exact dataset labels step by step.

        At each step, queries the current node to get pot + available actions,
        then uses match_action_to_available to convert e.g. "bet 33%" → "bet_1650".
        """
        resolved: list[str] = []
        fa = list(flop_actions)
        ta = list(turn_actions)
        ra = list(river_actions)

        for user_act in user_actions:
            resp = self._send({
                "cmd": "query",
                "flop_actions": fa,
                "turn": turn,
                "turn_actions": ta,
                "river": river,
                "river_actions": ra,
            })
            label, _exact, note = match_action_to_available(
                user_act, resp["actions"], resp["pot"]
            )
            if note:
                notes.append(note)
            resolved.append(label)

            # Append to the right street for the next step
            if river is not None:
                ra.append(label)
            elif turn is not None:
                ta.append(label)
            else:
                fa.append(label)

        return resolved

    def _build_result(self, resp: dict, notes: list[str]) -> QueryResult:
        pot = resp["pot"]
        actions = [
            _build_action_frequency(label, freq, pot)
            for label, freq in zip(resp["actions"], resp["strategy"])
        ]
        return QueryResult(
            matchup=self._current_matchup or "?",
            board_canonical=list(self._current_flop or []),
            to_act="OOP" if resp["to_act"] == "O" else "IP",
            pot_chips=pot,
            eff_stack_chips=resp["eff_stack"],
            spr=resp["spr"],
            actions=actions,
            range_advantage=resp["range_advantage"],
            nut_advantage=resp["nut_advantage"],
            oop_stats=_build_range_stats(resp["oop"]),
            ip_stats=_build_range_stats(resp["ip"]),
            notes=notes,
        )
