"""The shared board (§8): Evidence rows, this tick's posts, and the day memory.

Agents are plain functions/objects run in a fixed order each tick. Each one
READS the board (posts made earlier this tick, by this pass, plus the day's
memory: spot tape, raw observations, earlier evidence) and POSTS Evidence.
Ordering is what makes one agent's output the next agent's input — e.g. the
watchers read the derivatives agent's posts (§8.2 derivatives_agreement), and
the second watcher reads the first one's `progress` post to decide `stall`.

WEIGHTS is the single table of default evidence weights. Every number is a
starting point to tune in replay (§14, §17); all weights are clipped to [−1, 1]
when an Evidence is built.

Weight formulas (per tick, per post)
------------------------------------
statemachine  contested            range  +contested (0.6)                         §8.5
              lean                 lean   weight 0 — the §7.2 lean enters ℓ as
                                          LEAN_LOGODDS in manager step 2, the row
                                          is only there so the trace can cite it
derivatives   wall_pull            up/dn  ±wall_pull_max · strength_mult · _reach(|K−spot|/spot)
                                          (+ magnet: net GEX > 0, − barrier: net GEX < 0)
              dex_skew             up/dn  +dex_skew toward −sign(net DEX)
              flow                 up/dn  +flow_max · |flow score|/100 (compute_flow_direction_score)
              pin                  range  +pin
watchers      progress             side   progress_max · clip(½·progress_frac + ½·pace_ratio·reach)
              hold                 side   hold_max · f  (f = timer/HOLD_MIN while crossed,
                                          1 held/broken, ½ for the opposite failed_*)
              premium_build        side   premium_max · clip((ratio−1)/premium_full), only
                                          outside the ±8 % dead band (simmer tracker)
              retouch              side   −retouch
              derivatives_agreement side  D · (confirm·(1+agree_amp) − 1)  — D = net
                                          derivatives weight for the side this pass,
                                          confirm ∈ [0,1] from price (pace, hold)
              stall                range  +stall (both paces < stall_ratio over a full window)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

SCENARIOS = ("up", "down", "range")

WEIGHTS: dict = {
    # [1] state-machine agent
    "contested": 0.6,
    # [2] derivatives agent
    "wall_pull_max": 0.5,
    "wall_strength": {"high": 1.0, "medium": 0.6, "low": 0.3},
    "dex_skew": 0.15,
    "flow_max": 0.4,
    "pin": 0.3,
    # [3]/[4] watchers
    "progress_max": 0.4,
    "min_needed_ppm": 0.05,      # floor on the pace needed to reach T (pts/min)
    "min_pace_min": 2.0,         # need ≥ this many minutes of tape to measure pace
    "hold_max": 0.4,
    "hold_failed_opposite": 0.5,
    "premium_max": 0.4,
    "premium_full": 0.25,        # +25 % vs baseline = full weight
    "retouch": 0.4,
    "agree_amp": 0.5,
    "stall": 0.3,
    "stall_ratio": 0.25,         # pace floor, as a fraction of the pace needed to reach T
}


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass(frozen=True)
class Evidence:
    agent: str
    scenario: str
    weight: float
    kind: str
    detail: dict = field(default_factory=dict)
    reason: str = ""
    pass_no: int = 0

    def __post_init__(self) -> None:
        if self.scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of {SCENARIOS}: {self.scenario!r}")
        try:
            w = float(self.weight)
        except (TypeError, ValueError):
            w = 0.0
        if not math.isfinite(w):
            w = 0.0
        object.__setattr__(self, "weight", round(clip(w), 6))

    def to_row(self) -> dict:
        """lvl_evidence row (the runtime adds trade_date / ts)."""
        return {"agent": self.agent, "scenario": self.scenario, "weight": self.weight,
                "kind": self.kind, "detail": dict(self.detail), "reason": self.reason,
                "pass": self.pass_no}

    @classmethod
    def from_row(cls, row: dict) -> "Evidence | None":
        try:
            return cls(agent=str(row.get("agent") or ""), scenario=row.get("scenario"),
                       weight=float(row.get("weight") or 0.0), kind=str(row.get("kind") or ""),
                       detail=dict(row.get("detail") or {}), reason=str(row.get("reason") or ""),
                       pass_no=int(row.get("pass") or 0))
        except (TypeError, ValueError):
            return None


class Board:
    """This tick's posts + the day's memory. Owned by the brain, one per day."""

    def __init__(self) -> None:
        self.ts: datetime | None = None
        self.spot: float | None = None
        self.posts: list[Evidence] = []
        self.tape: list[tuple[datetime, float]] = []          # (ts, spot), whole day
        self.history: list[tuple[datetime, Evidence]] = []   # earlier ticks' posts
        self.obs: dict[str, list[tuple[datetime, float]]] = {}

    # ---- tick lifecycle ----
    def begin_tick(self, ts: datetime, spot: float) -> None:
        self.ts, self.spot = ts, float(spot)
        self.posts = []
        if not self.tape or self.tape[-1][0] < ts:
            self.tape.append((ts, float(spot)))

    def end_tick(self) -> None:
        self.history.extend((self.ts, e) for e in self.posts)
        self.posts = []

    # ---- writes ----
    def post(self, ev: Evidence) -> Evidence:
        self.posts.append(ev)
        return ev

    def post_all(self, evs) -> list[Evidence]:
        out = list(evs)
        self.posts.extend(out)
        return out

    def observe(self, name: str, value: float) -> None:
        """Record a raw per-tick reading (an agent's own memory, e.g. flow score)."""
        series = self.obs.setdefault(name, [])
        if series and series[-1][0] == self.ts:
            series[-1] = (self.ts, float(value))
        else:
            series.append((self.ts, float(value)))

    # ---- reads: this tick ----
    def this_tick(self, agent: str | None = None, kind: str | tuple | None = None,
                  scenario: str | None = None, pass_no: int | None = None) -> list[Evidence]:
        kinds = (kind,) if isinstance(kind, str) else kind
        return [e for e in self.posts
                if (agent is None or e.agent == agent)
                and (kinds is None or e.kind in kinds)
                and (scenario is None or e.scenario == scenario)
                and (pass_no is None or e.pass_no == pass_no)]

    # ---- reads: day memory, windowed ----
    def _since(self, minutes: float) -> datetime | None:
        if self.ts is None:
            return None
        return self.ts - timedelta(minutes=minutes)

    def spots(self, minutes: float) -> list[tuple[datetime, float]]:
        """Tape samples within the last `minutes` (current tick included)."""
        t0 = self._since(minutes)
        return [x for x in self.tape if t0 is None or x[0] >= t0]

    def observations(self, name: str, minutes: float) -> list[float]:
        t0 = self._since(minutes)
        return [v for t, v in self.obs.get(name, []) if t0 is None or t >= t0]

    def recent(self, minutes: float, agent: str | None = None, kind: str | None = None,
               scenario: str | None = None) -> list[tuple[datetime, Evidence]]:
        """Earlier ticks' posts within the last `minutes`."""
        t0 = self._since(minutes)
        return [(t, e) for t, e in self.history
                if (t0 is None or t >= t0)
                and (agent is None or e.agent == agent)
                and (kind is None or e.kind == kind)
                and (scenario is None or e.scenario == scenario)]
