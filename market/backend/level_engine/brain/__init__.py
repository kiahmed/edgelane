"""The Level Engine brain (phase 2): agents + manager + traces + grading.

Spec: docs/level_engine.md §8 (agents, board), §8.5 (manager), §9 (acceptance),
§11 (pick), §12 (grading), §13 (end of day).

    brain = create_brain(cfg)
    out = brain(ctx)            # TickContext -> BrainOutput | None (see context.py)
    brain.reset_day(trade_date)

Modules: board (Evidence, Board, WEIGHTS) · statemachine_agent [1] ·
derivatives [2] · watchers [3]/[4] · manager [7] + pipeline · grading · day_summary.
Deterministic plain code — no LLM, no agent framework, no wall clock, no
randomness; everything is keyed off ctx.ts.
"""
from __future__ import annotations

from .board import SCENARIOS, WEIGHTS, Board, Evidence
from .manager import LevelBrain


def create_brain(cfg, strategy="auto") -> LevelBrain:
    """A fresh brain. `strategy`: "auto" (lazy import of level_engine.strategy,
    none if absent), None (no picks — shadow grading only), or a
    (pick_fn, reprice_fn) pair (tests / replay)."""
    return LevelBrain(cfg, strategy=strategy)


__all__ = ["create_brain", "LevelBrain", "Board", "Evidence", "WEIGHTS", "SCENARIOS"]
