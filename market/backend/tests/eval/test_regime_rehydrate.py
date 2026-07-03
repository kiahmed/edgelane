"""Startup rehydration of the in-memory regime counters from the outcomes table.

A restart wipes consec loss/win + the pause flag (they live only on the state
singleton). rehydrate_regime() must replay each ticker's recent graded outcomes
(oldest→newest) through the same state machine as _update_regime, PER SYMBOL, so
a redeploy doesn't silently reset an active pause / loss streak.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.evaluator import rehydrate_regime, state as evaluator_state, _SESSION_TZ

SETTINGS = SimpleNamespace(
    regime_alert_consec_losses=3,
    regime_clear_consec_wins=2,
    eval_min_graded=10,
)

SYMS = ("AAA", "BBB", "CCC")


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.since = "unset"

    def fetch_regime_replay(self, per_symbol: int = 200, since=None):
        # Caller expects (symbol, result), symbol ASC then oldest→newest.
        self.since = since
        return list(self._rows)


@pytest.fixture(autouse=True)
def _reset():
    for d in (evaluator_state.consec_losses_by_symbol,
              evaluator_state.consec_wins_by_symbol,
              evaluator_state.regime_alert_active_by_symbol,
              evaluator_state.regime_alert_triggered_at):
        for s in SYMS:
            d.pop(s, None)
    yield
    for d in (evaluator_state.consec_losses_by_symbol,
              evaluator_state.consec_wins_by_symbol,
              evaluator_state.regime_alert_active_by_symbol,
              evaluator_state.regime_alert_triggered_at):
        for s in SYMS:
            d.pop(s, None)


def test_restores_active_pause_from_loss_streak():
    # 4 losses (oldest→newest) → paused restored, 2 wins to resume.
    rehydrate_regime(_FakeDB([("AAA", "loss")] * 4), SETTINGS)
    assert evaluator_state.regime_alert_active_by_symbol["AAA"] is True
    assert evaluator_state.consec_losses_by_symbol["AAA"] == 4
    assert evaluator_state.consec_wins_by_symbol["AAA"] == 0


def test_pause_clears_when_history_ends_in_two_wins():
    # 3 losses (paused) then 2 wins (clears) → not paused on resume.
    rows = [("AAA", "loss")] * 3 + [("AAA", "win")] * 2
    rehydrate_regime(_FakeDB(rows), SETTINGS)
    assert evaluator_state.regime_alert_active_by_symbol["AAA"] is False
    assert evaluator_state.consec_wins_by_symbol["AAA"] == 2
    assert evaluator_state.consec_losses_by_symbol["AAA"] == 0


def test_single_win_after_streak_stays_paused_one_to_go():
    rows = [("AAA", "loss")] * 3 + [("AAA", "win")]
    rehydrate_regime(_FakeDB(rows), SETTINGS)
    assert evaluator_state.regime_alert_active_by_symbol["AAA"] is True
    assert evaluator_state.consec_wins_by_symbol["AAA"] == 1


def test_neutral_does_not_break_the_streak():
    rows = [("AAA", "loss"), ("AAA", "neutral"), ("AAA", "loss"), ("AAA", "loss")]
    rehydrate_regime(_FakeDB(rows), SETTINGS)
    assert evaluator_state.regime_alert_active_by_symbol["AAA"] is True
    assert evaluator_state.consec_losses_by_symbol["AAA"] == 3


def test_counters_are_independent_per_ticker():
    # Dropdown-switch guarantee: each symbol's streak/pause is isolated.
    rows = (
        [("AAA", "loss")] * 3 +                       # AAA paused
        [("BBB", "win")] * 2 +                        # BBB clean, 2 wins
        [("CCC", "loss"), ("CCC", "win"), ("CCC", "loss")]  # CCC: 1 loss, not paused
    )
    rehydrate_regime(_FakeDB(rows), SETTINGS)

    assert evaluator_state.regime_alert_active_by_symbol["AAA"] is True
    assert evaluator_state.consec_losses_by_symbol["AAA"] == 3

    assert evaluator_state.regime_alert_active_by_symbol["BBB"] is False
    assert evaluator_state.consec_wins_by_symbol["BBB"] == 2
    assert evaluator_state.consec_losses_by_symbol["BBB"] == 0

    assert evaluator_state.regime_alert_active_by_symbol["CCC"] is False
    assert evaluator_state.consec_losses_by_symbol["CCC"] == 1
    assert evaluator_state.consec_wins_by_symbol["CCC"] == 0


def test_fetch_failure_is_non_fatal():
    class _Boom:
        def fetch_regime_replay(self, per_symbol: int = 200, since=None):
            raise RuntimeError("db down")

    rehydrate_regime(_Boom(), SETTINGS)  # must not raise
    assert "AAA" not in evaluator_state.regime_alert_active_by_symbol


def test_replay_is_scoped_to_start_of_today_et():
    # The session-gap guard: rehydration must ask the DB only for outcomes since
    # ET midnight today (yesterday's streak belongs to a different regime).
    db = _FakeDB([("AAA", "loss")] * 3)
    rehydrate_regime(db, SETTINGS)

    since = db.since
    assert isinstance(since, datetime)
    assert since.tzinfo is not None                       # tz-aware (UTC instant)
    assert since <= datetime.now(timezone.utc)            # not in the future
    # Localized back to ET it must be exactly midnight of today's ET date.
    et = since.astimezone(_SESSION_TZ)
    assert (et.hour, et.minute, et.second, et.microsecond) == (0, 0, 0, 0)
    assert et.date() == datetime.now(ZoneInfo("America/New_York")).date()
