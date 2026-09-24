"""pick_result — closing the loop on every pick Matrix announces.

A pick_selected post says "the engine likes this"; pick_result says how it
actually went. It must grade exactly the way the win rate grades (the run's
LAST grade before the engine moved on), post wins AND losses (a feed that only
reports wins is marketing), and skip neutrals (no takeaway).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import matrix_events as me
from app import matrix_signals as ms
from app.db import Database

SYM = "SPX"
LEGS_A = '[{"strike": 7780.0, "side": "call"}]'
LEGS_B = '[{"strike": 7800.0, "side": "call"}]'
T0 = datetime(2026, 9, 2, 14, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean():
    ms.state.reset()
    yield
    ms.state.reset()


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    async def _fake(symbol, state, expiry=None, *, day=None, discriminator=None,
                    extra_attributes=None):
        calls.append({"state": state, "disc": discriminator,
                      "attrs": extra_attributes or {},
                      "event_id": me.event_id(symbol, state, None, discriminator)})
        return True

    monkeypatch.setattr(ms.matrix_events, "publish_transition", _fake)
    return calls


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "pr.duckdb")
    d.connect()
    return d


def _poll(db, ts, legs, result=None, delta=0.2):
    did = db.insert_bias_decision({
        "ts": ts, "symbol": SYM, "expiration": "2026-09-02", "spot_at_decision": 7790.0,
        "score": 10.0, "label": "bearish", "confidence": "high",
        "put_wall_strike": None, "put_wall_strength": None, "put_wall_net_gex": None,
        "call_wall_strike": None, "call_wall_strength": None, "call_wall_net_gex": None,
        "recommended_strategies": "bear_call", "pick_legs": legs,
        "pick_entry_mid": 3.0, "pick_spread_type": "credit", "pick_strategy": "bear_call",
    })
    if result is not None:
        db.insert_outcome({
            "decision_id": int(did), "evaluated_at": ts + timedelta(minutes=3),
            "spot_at_eval": 7790.0, "elapsed_minutes": 3.0,
            "predicted_direction": "down", "actual_move_pct": 0.0, "result": result,
            "entry_net_premium": 3.0, "eval_net_premium": 3.0 - delta,
            "favorable_delta": delta if result == "win" else -delta,
            "friction_band": 0.05, "spread_type": "credit",
        })


def _pend(key=LEGS_A, since=T0 - timedelta(minutes=1)):
    ms.state.pending_results[SYM] = [{
        "key": key, "expiry": "2026-09-02",
        "summary": {"strategy": "bear_call", "label": "Aggressive"},
        "since": since, "announced_at": datetime.now(timezone.utc),
    }]


def _run(db):
    return ms._resolve_pick_results(SYM, db)


# ── the lookup, against a real DB ───────────────────────────────────────────

def test_run_lookup_takes_the_last_grade_of_the_first_run(db):
    _poll(db, T0, LEGS_A, "loss")
    _poll(db, T0 + timedelta(seconds=16), LEGS_A, "loss")
    _poll(db, T0 + timedelta(seconds=32), LEGS_A, "win")      # how it ENDED
    _poll(db, T0 + timedelta(seconds=48), LEGS_B)             # engine moved on
    _poll(db, T0 + timedelta(seconds=64), LEGS_A, "loss")     # a LATER, separate run
    run = db.fetch_pick_run(SYM, LEGS_A, T0 - timedelta(minutes=1))
    assert run["closed"] is True
    assert run["polls"] == 3
    assert run["final"]["result"] == "win"


def test_an_open_run_is_not_closed(db):
    _poll(db, T0, LEGS_A, "win")
    run = db.fetch_pick_run(SYM, LEGS_A, T0 - timedelta(minutes=1))
    assert run["closed"] is False


# ── posting policy ──────────────────────────────────────────────────────────

async def test_a_winning_pick_reports_its_result(db, sent):
    _poll(db, T0, LEGS_A, "win", delta=0.35)
    _poll(db, T0 + timedelta(seconds=16), LEGS_B)
    _pend()
    _run(db); await ms.drain()
    ev = next(c for c in sent if c["state"] == "pick_result")
    assert ev["attrs"]["result"] == "win"
    assert ev["attrs"]["favorable_delta"] == "0.35"
    assert ev["attrs"]["strategy"] == "bear_call"
    assert ms.state.pending_results[SYM] == []


async def test_a_losing_pick_is_reported_too(db, sent):
    """Only reporting wins would be cherry-picking."""
    _poll(db, T0, LEGS_A, "loss")
    _poll(db, T0 + timedelta(seconds=16), LEGS_B)
    _pend()
    _run(db); await ms.drain()
    assert [c["attrs"]["result"] for c in sent] == ["loss"]


async def test_a_neutral_result_is_not_posted(db, sent):
    _poll(db, T0, LEGS_A, "neutral")
    _poll(db, T0 + timedelta(seconds=16), LEGS_B)
    _pend()
    _run(db); await ms.drain()
    assert sent == []
    assert ms.state.pending_results[SYM] == []          # resolved, not retried


async def test_a_pick_still_on_screen_waits(db, sent):
    _poll(db, T0, LEGS_A, "win")
    _pend()
    _run(db); await ms.drain()
    assert sent == []
    assert len(ms.state.pending_results[SYM]) == 1


async def test_it_waits_for_the_final_poll_to_be_graded(db, sent):
    """The grader lags ~3 min; reporting early would use a stale grade."""
    now = datetime.now(timezone.utc)
    _poll(db, now - timedelta(seconds=40), LEGS_A, "loss")
    _poll(db, now - timedelta(seconds=20), LEGS_A)          # final poll, not graded yet
    _poll(db, now - timedelta(seconds=5), LEGS_B)
    _pend(since=now - timedelta(minutes=2))
    _run(db); await ms.drain()
    assert sent == [], "must not report a result the grader hasn't finished"
    assert len(ms.state.pending_results[SYM]) == 1


# ── end to end: announce → result, threaded by event id ─────────────────────

async def test_the_result_threads_under_its_announcement(sent, monkeypatch):
    from app.evaluator import state as est
    est.regime_alert_active_by_symbol.clear()
    est.consec_wins_by_symbol["SPX"] = 1
    ms.state.last_trust_state["SPX"] = "in_sync"
    ms.state.last_win_rate["SPX"] = 64.0
    ms.state.last_graded["SPX"] = 20

    snap = {
        "symbol": SYM, "expiration": "2026-09-02",
        "bias": {"call_wall_strike": 7800.0},
        "engine_pick": {"strategy": "bear_call", "label": "Aggressive",
                        "legs": [{"strike": 7780.0, "side": "call"}],
                        "composite_score": 80.0, "health": "healthy",
                        "composite_verdict": {"label": "tradeable on limit"}},
        "strategies": {},
    }
    await ms.on_snapshot(snap, None); await ms.drain()
    announced = next(c for c in sent if c["state"] == "pick_selected")

    class _DB:
        def fetch_accuracy(self, *a, **k):
            return {"n": 20, "wins": 12, "losses": 4, "neutrals": 4, "accuracy_pct": 60.0}

        def fetch_pick_run(self, sym, legs, since, limit=5000):
            return {"closed": True, "polls": 5, "last_graded": True,
                    "first_ts": T0, "last_ts": T0 + timedelta(minutes=4),
                    "final": {"result": "win", "entry_net_premium": 3.0,
                              "eval_net_premium": 2.6, "favorable_delta": 0.4}}

    class _Poller:
        latest_by_symbol = {SYM: snap}

    class _S:
        eval_min_graded = 10; pill_green_pct = 60.0; pill_red_pct = 40.0
        eval_rolling_window = 20; pick_min_dwell_polls = 1

    await ms.on_evaluation(_DB(), _Poller(), _S()); await ms.drain()
    result = next(c for c in sent if c["state"] == "pick_result")
    assert result["disc"] == announced["disc"], "same pick hash → poster can thread it"
    assert result["attrs"]["held_minutes"] == "4"
