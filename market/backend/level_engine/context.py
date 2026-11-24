"""The seam between the tick loop (runtime) and the brain (agents + manager).

Spec: docs/level_engine.md §8 (tick order), §12 (grading).

The runtime (tick.py) owns: feed → ratio → levels/clusters → state machine →
scenario → persisting the tick + level-state transitions. It then builds a
TickContext and calls `brain(ctx) -> BrainOutput | None`. The runtime persists
what the brain returns (evidence, belief, pick-on-change, leg opens/updates)
— the brain itself never touches the store directly, which is what makes
replay (§14) exact: the same contexts produce the same outputs, and the runtime
decides whether they land in live tables or lvl_replay.

The brain may keep its OWN in-memory state across ticks (log-odds, current
call, leg in progress, baselines); `day_memory` gives read access to the day's
earlier evidence/beliefs (from the store) for restarts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Protocol

from .config import LevelConfig
from .levels import Cluster
from .scenario import Scenario
from .state_machine import LevelMap


@dataclass
class TickContext:
    cfg: LevelConfig
    trade_date: date
    ts: datetime                     # feed ts (tz-aware UTC) — the tick's clock
    spot: float                      # SPX
    prev_spot: float | None
    spy: float | None
    ratio: float | None
    ratio_locked: bool
    clusters: list[Cluster]          # sorted ascending
    level_map: LevelMap              # already stepped for this tick
    scenario: Scenario               # map_scenario(level_map, spot) for this tick
    transitions: list[dict]          # this tick's level-state transitions
    feed: dict                       # the raw feed (contracts, bias, expected_move, dte, expiration, engine_pick, ...)
    levels_changed: bool             # level set / prices changed since last tick (§8.4 strike rebuild)
    session_close: bool = False      # last tick of the session (grading → loss / zone_held, §12/§13)
    day_memory: Callable[[str], list[dict]] = field(default=lambda table: [])  # store.list_rows(table, trade_date)


@dataclass
class BrainOutput:
    evidence: list[dict] = field(default_factory=list)   # lvl_evidence rows (sans trade_date/ts — runtime fills)
    belief: dict | None = None                           # lvl_beliefs row (sans trade_date/ts)
    pick: dict | None = None                             # the §11 pick (runtime writes lvl_picks only when pick_id changes)
    leg_open: dict | None = None                         # lvl_legs row to insert (must carry id)
    leg_updates: list[dict] = field(default_factory=list)  # [{id, ...fields}] → store.update_leg
    day_summary: dict | None = None                      # at session close (§13)
    decision: dict[str, Any] = field(default_factory=dict)  # the read-API "latest decision" payload (§3.3)


class Brain(Protocol):
    def __call__(self, ctx: TickContext) -> BrainOutput | None: ...
