"""The win rate and the regime streak count PICKS, not polls.

The poller writes a bias_decisions row every ~15s, so a pick held for 45 minutes
produces ~170 graded rows while one that dies in 30s produces two. Counting each
row as a separate trade made the score a measure of how long a pick lingered.
These tests pin the collapse to one result per pick episode.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db import Database

SYM = "SPX"
T0 = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc).replace(tzinfo=None)

LEGS_A = '[{"strike": 7780, "side": "call"}]'
LEGS_B = '[{"strike": 7800, "side": "call"}]'
LEGS_C = '[{"strike": 7820, "side": "call"}]'


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "eval.duckdb")
    d.connect()
    return d


def _poll(db, i: int, legs: str | None, result: str | None, symbol: str = SYM):
    """One poll: a decision row, optionally graded."""
    did = db.insert_bias_decision({
        "ts": T0 + timedelta(seconds=16 * i),
        "symbol": symbol, "expiration": "2026-09-02", "spot_at_decision": 7790.0,
        "score": 10.0, "label": "mild_bearish", "confidence": "medium",
        "put_wall_strike": None, "put_wall_strength": None, "put_wall_net_gex": None,
        "call_wall_strike": None, "call_wall_strength": None, "call_wall_net_gex": None,
        "recommended_strategies": "bear_call",
        "pick_legs": legs, "pick_entry_mid": 1.0 if legs else None,
        "pick_spread_type": "credit" if legs else None,
        "pick_strategy": "bear_call" if legs else None,
    })
    if result is not None:
        db.insert_outcome({
            "decision_id": int(did),
            "evaluated_at": T0 + timedelta(seconds=16 * i + 180),
            "spot_at_eval": 7790.0, "elapsed_minutes": 3.0,
            "predicted_direction": "down", "actual_move_pct": 0.0,
            "result": result, "entry_net_premium": 1.0, "eval_net_premium": 1.0,
            "favorable_delta": 0.1 if result == "win" else -0.1,
            "friction_band": 0.05, "spread_type": "credit",
        })
    return did


def test_a_long_held_pick_counts_once(db):
    """The core fix: 30 winning re-checks of ONE pick is one win, not 30."""
    for i in range(30):
        _poll(db, i, LEGS_A, "win")
    _poll(db, 30, LEGS_B, "loss")      # a second pick, so the first is closed
    _poll(db, 31, LEGS_C, None)        # newest = still live, excluded

    acc = db.fetch_accuracy(SYM, 20)
    assert acc["n"] == 2, "each pick must contribute exactly one result"
    assert acc["wins"] == 1 and acc["losses"] == 1
    assert acc["accuracy_pct"] == 50.0   # was 30/31 = 97% when counting polls


def test_episode_takes_its_final_grade(db):
    """A pick that was green early and red at the end ends up a loss —
    the result is 'how did this idea end up', not how it looked mid-flight."""
    for i in range(4):
        _poll(db, i, LEGS_A, "win")
    _poll(db, 4, LEGS_A, "loss")       # last grade before the pick changed
    _poll(db, 5, LEGS_B, "win")
    _poll(db, 6, LEGS_C, None)         # live

    acc = db.fetch_accuracy(SYM, 20)
    assert acc["n"] == 2
    assert acc["losses"] == 1 and acc["wins"] == 1


def test_live_pick_is_not_counted_yet(db):
    """An idea still on screen has no final result."""
    _poll(db, 0, LEGS_A, "win")
    for i in range(1, 5):
        _poll(db, i, LEGS_B, "loss")   # newest episode = live

    acc = db.fetch_accuracy(SYM, 20)
    assert acc["n"] == 1
    assert acc["wins"] == 1 and acc["losses"] == 0


def test_same_pick_returning_later_is_two_ideas(db):
    """A → B → A is three decisions, not one merged run."""
    _poll(db, 0, LEGS_A, "win")
    _poll(db, 1, LEGS_B, "loss")
    _poll(db, 2, LEGS_A, "win")
    _poll(db, 3, LEGS_C, None)         # live

    acc = db.fetch_accuracy(SYM, 20)
    assert acc["n"] == 3


def test_a_gap_with_no_pick_breaks_the_run(db):
    """Pick A → no pick → pick A is two separate ideas."""
    _poll(db, 0, LEGS_A, "win")
    _poll(db, 1, None, None)           # engine had no pick
    _poll(db, 2, LEGS_A, "loss")
    _poll(db, 3, LEGS_C, None)         # live

    acc = db.fetch_accuracy(SYM, 20)
    assert acc["n"] == 2
    assert acc["wins"] == 1 and acc["losses"] == 1


def test_regime_replay_is_one_result_per_pick(db):
    """'3 losses in a row' must mean three bad picks — not one held pick
    showing red at three consecutive 15s re-checks."""
    for i in range(6):
        _poll(db, i, LEGS_A, "loss")   # ONE bad pick, re-checked six times
    _poll(db, 6, LEGS_B, None)         # live

    rows = [r for r in db.fetch_regime_replay() if r[0] == SYM]
    assert [r[1] for r in rows] == ["loss"], "six re-checks of one pick = one loss"


def test_episodes_are_isolated_per_symbol(db):
    _poll(db, 0, LEGS_A, "win", symbol="SPX")
    _poll(db, 1, LEGS_A, "loss", symbol="NDX")
    _poll(db, 2, LEGS_B, "win", symbol="SPX")
    _poll(db, 3, LEGS_C, None, symbol="SPX")
    _poll(db, 4, LEGS_C, None, symbol="NDX")

    assert db.fetch_accuracy("SPX", 20)["n"] == 2
    assert db.fetch_accuracy("NDX", 20)["n"] == 1
