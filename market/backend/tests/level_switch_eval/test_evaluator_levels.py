"""Levels mode: the legacy evaluator does not grade, but its loop keeps the
archive / daily email / signals running (§3.4)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import evaluator, matrix_daily_summary, matrix_signals


class _NoGradeDB:
    def fetch_pending_evaluations(self, *a, **k):
        raise AssertionError("levels mode must not grade legacy decisions")

    def fetch_regime_replay(self, *a, **k):
        raise AssertionError("levels mode must not rehydrate the legacy regime")

    def insert_outcome(self, *a, **k):
        raise AssertionError("levels mode must not write outcomes")


@pytest.fixture
def on_eval_calls(monkeypatch):
    calls = []

    async def _rec(db, poller_state, settings):
        calls.append(settings)
        return []
    monkeypatch.setattr(matrix_signals, "on_evaluation", _rec)
    return calls


async def _one_pass(monkeypatch, db, poller, settings):
    """Run evaluator_loop until its first sleep."""
    monkeypatch.setattr(evaluator, "archive_completed_days", lambda db: 0)
    ticks = []
    monkeypatch.setattr(matrix_daily_summary, "tick", lambda db, s: ticks.append(1))

    async def _stop(_):
        raise asyncio.CancelledError
    monkeypatch.setattr(evaluator.asyncio, "sleep", _stop)
    with pytest.raises(asyncio.CancelledError):
        await evaluator.evaluator_loop(db, poller, settings)
    return ticks


async def test_evaluate_pending_does_not_grade_in_levels_mode(levels, on_eval_calls):
    poller = SimpleNamespace(latest_by_symbol={"SPX": {}}, evaluation_active=True,
                             market_open=True)
    assert await evaluator.evaluate_pending(_NoGradeDB(), poller, levels) == 0
    assert len(on_eval_calls) == 1, "signals still run, fed by level grading"


@pytest.mark.parametrize("active", [True, False])
async def test_loop_runs_signals_every_pass_even_market_closed(levels, on_eval_calls,
                                                               monkeypatch, active):
    poller = SimpleNamespace(latest_by_symbol={"SPX": {}}, evaluation_active=active,
                             market_open=active)
    evaluator.state.last_archive_date = None
    ticks = await _one_pass(monkeypatch, _NoGradeDB(), poller, levels)
    assert ticks == [1], "daily email tick keeps running"
    assert len(on_eval_calls) == 1
    assert evaluator.state.last_run_at is not None
    assert evaluator.state.last_error is None


async def test_engine_mode_loop_unchanged_when_market_closed(on_eval_calls, monkeypatch):
    from app.config import Settings
    poller = SimpleNamespace(latest_by_symbol={"SPX": {}}, evaluation_active=False,
                             market_open=False)
    evaluator.state.last_archive_date = None
    ticks = await _one_pass(monkeypatch, _NoGradeDB(), poller, Settings())
    assert ticks == [1]
    assert on_eval_calls == [], "engine mode: closed market → no sweep, no signals"


async def test_engine_mode_still_grades(on_eval_calls):
    class _DB:
        called = False

        def fetch_pending_evaluations(self, *a, **k):
            _DB.called = True
            return []
    from app.config import Settings
    poller = SimpleNamespace(latest_by_symbol={}, evaluation_active=True, market_open=True)
    assert await evaluator.evaluate_pending(_DB(), poller, Settings()) == 0
    assert _DB.called and on_eval_calls == []   # empty sweep returns before signals
