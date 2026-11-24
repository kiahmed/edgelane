"""matrix_signals in levels mode: picks keyed by pick_id, trust / win rate from
level stats, pick_result from the level leg, daily_recap from level legs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import matrix_signals as ms
from app.poller import state as poller_state

from .conftest import (OTHER_PICK_ID, PICK_ID, decision_with_pick, leg, level_pick_dict,
                       now_utc, results_stats, spx_snapshot, stats_body)


class _NoDB:
    def __getattr__(self, name):
        raise AssertionError(f"levels mode must not read the legacy grader ({name})")


@pytest.fixture(autouse=True)
def _no_hold(monkeypatch):
    monkeypatch.setattr(ms, "_ANNOUNCE_MIN_HOLD_MIN", 0.0)


def _poller():
    return SimpleNamespace(latest_by_symbol=poller_state.latest_by_symbol)


def _pend(pick_id=PICK_ID, since=None, announced=None, call="up"):
    since = since or now_utc() - timedelta(hours=1)
    pick = level_pick_dict(pick_id)
    return {"key": ms._pick_key(pick), "expiry": "2026-11-23",
            "summary": ms._pick_summary(pick), "pick": pick, "since": since,
            "announced_at": announced or since + timedelta(minutes=5),
            "pick_id": pick_id, "call": call}


def test_level_picks_are_keyed_by_pick_id_legacy_by_legs():
    assert ms._pick_key(level_pick_dict()) == f"pick_id:{PICK_ID}"
    legacy = spx_snapshot()["engine_pick"]
    assert ms._pick_key(legacy).startswith("[")          # unchanged: legs JSON


async def test_on_evaluation_seeds_trust_and_win_rate_from_level_stats(levels, upstream, sent):
    upstream.stats(results_stats(["win"] * 8 + ["loss"] * 2, trade_date="2026-11-20"))
    upstream.decision(decision_with_pick())
    await ms.on_evaluation(_NoDB(), _poller(), levels)
    await ms.drain()
    assert ms.state.last_trust_state["SPX"] == "in_sync"
    assert ms.state.last_win_rate["SPX"] == 80.0
    assert ms.state.last_graded["SPX"] == 10
    assert ms.state.level_regime["SPX"] == {"alert": False, "consec_wins": 0}
    assert sent == [], "first pass only seeds the baseline"


async def test_bias_aligned_fires_on_level_transition(levels, upstream, sent):
    upstream.stats(results_stats(["win"] * 7, trade_date="2026-11-20"))
    upstream.decision(decision_with_pick())
    await ms.on_evaluation(_NoDB(), _poller(), levels)          # calibrating
    from app import level_accuracy
    level_accuracy.clear_cache()
    upstream.stats(results_stats(["win"] * 9 + ["loss"] * 2, trade_date="2026-11-20"))
    out = await ms.on_evaluation(_NoDB(), _poller(), levels)     # in_sync at 82%
    await ms.drain()
    assert "bias_aligned" in out
    ev = next(c for c in sent if c["state"] == "bias_aligned")
    assert ev["attrs"]["graded"] == "11"


async def test_engine_unreachable_changes_nothing(levels, upstream, sent):
    ms.state.last_trust_state["SPX"] = "in_sync"
    assert await ms.on_evaluation(_NoDB(), _poller(), levels) == []
    assert ms.state.last_trust_state["SPX"] == "in_sync"


async def test_pick_selected_announces_the_level_pick_never_the_legacy(levels, upstream, sent):
    upstream.decision(decision_with_pick())
    ms.state.last_trust_state["SPX"] = "in_sync"
    ms.state.last_win_rate["SPX"] = 70.0
    ms.state.last_graded["SPX"] = 20
    raw = spx_snapshot()
    out = []
    for _ in range(ms._dwell(levels)):
        out += await ms.on_snapshot(raw, levels)
    await ms.drain()
    assert out.count("pick_selected") == 1
    ev = next(c for c in sent if c["state"] == "pick_selected")
    assert ev["attrs"]["strategy"] == "call_butterfly"
    assert ev["disc"] == ms.matrix_events.discriminator(f"pick_id:{PICK_ID}")
    pend = ms.state.pending_results["SPX"][0]
    assert pend["key"] == f"pick_id:{PICK_ID}" and pend["pick_id"] == PICK_ID
    assert pend["call"] == "up"
    assert raw["engine_pick"]["strategy"] == "bear_call", "poller snapshot not mutated"


async def test_pick_selected_silent_when_level_engine_down(levels, upstream, sent):
    ms.state.last_trust_state["SPX"] = "in_sync"
    ms.state.last_win_rate["SPX"] = 70.0
    ms.state.last_graded["SPX"] = 20
    for _ in range(ms._dwell(levels)):
        assert "pick_selected" not in await ms.on_snapshot(spx_snapshot(), levels)
    assert "SPX" not in ms.state.pending_results


async def test_capital_trap_level_pick_is_blocked(levels, upstream, sent):
    dec = decision_with_pick()
    dec["pick"]["health"] = "capital_trap"
    upstream.decision(dec)
    ms.state.last_trust_state["SPX"] = "in_sync"
    ms.state.last_win_rate["SPX"] = 70.0
    ms.state.last_graded["SPX"] = 20
    for _ in range(ms._dwell(levels)):
        assert "pick_selected" not in await ms.on_snapshot(spx_snapshot(), levels)
    assert ms.state.pick_blocked.get("SPX") is True


async def test_pick_result_resolves_by_pick_id(levels, upstream, sent):
    p = _pend()
    ms.state.pending_results["SPX"] = [p]
    opened = p["since"] + timedelta(minutes=1)
    win = leg(1, "win", opened=opened, minutes=22, pick_id=PICK_ID, pnl=4.5)
    other = leg(2, "loss", opened=opened, minutes=30, pick_id=OTHER_PICK_ID)
    upstream.stats(stats_body([win, other] + [leg(10 + i, "win", opened=opened,
                                                  trade_date="2026-11-20")
                                              for i in range(10)]))
    upstream.decision(decision_with_pick())
    out = await ms.on_evaluation(_NoDB(), _poller(), levels)
    await ms.drain()
    assert out.count("pick_result") == 1
    a = next(c for c in sent if c["state"] == "pick_result")["attrs"]
    assert a["result"] == "win" and a["pick_id"] == PICK_ID and a["leg_id"] == "leg-1"
    assert a["strategy_pnl"] == "4.5" and a["held_minutes"] == "22"
    assert a["close_reason"] == "target" and a["entry_spot"] == "7255.0"
    assert a["exit_spot"] == "7279.0" and a["target_level"] == "R1"
    assert next(c for c in sent if c["state"] == "pick_result")["disc"] == \
        ms.matrix_events.discriminator(p["key"])
    assert ms.state.pending_results["SPX"] == []


async def test_pick_result_waits_while_the_leg_is_open(levels, upstream, sent):
    p = _pend()
    ms.state.pending_results["SPX"] = [p]
    body = stats_body([], open_leg=leg(1, None, opened=p["since"], pick_id=PICK_ID))
    upstream.stats(body)
    upstream.decision(decision_with_pick())
    out = await ms.on_evaluation(_NoDB(), _poller(), levels)
    assert "pick_result" not in out
    assert ms.state.pending_results["SPX"] == [p]


async def test_pick_result_posts_losses_too(levels, upstream, sent):
    p = _pend()
    ms.state.pending_results["SPX"] = [p]
    upstream.stats(stats_body([leg(1, "loss", opened=p["since"], pick_id=PICK_ID,
                                   close_reason="session_close", pnl=-3.2)]))
    upstream.decision(decision_with_pick())
    await ms.on_evaluation(_NoDB(), _poller(), levels)
    await ms.drain()
    a = next(c for c in sent if c["state"] == "pick_result")["attrs"]
    assert a["result"] == "loss" and a["close_reason"] == "session_close"


async def test_pick_result_falls_back_to_the_live_leg_of_the_same_call(levels, upstream, sent):
    """Re-struck mid-leg: the announced pick_id isn't the leg's opening id."""
    p = _pend(pick_id="restruck00000000")
    ms.state.pending_results["SPX"] = [p]
    l = leg(1, "win", opened=p["announced_at"] - timedelta(minutes=10), minutes=30,
            pick_id=PICK_ID)
    upstream.stats(stats_body([l]))
    upstream.decision(decision_with_pick())
    await ms.on_evaluation(_NoDB(), _poller(), levels)
    await ms.drain()
    assert any(c["state"] == "pick_result" for c in sent)


async def test_daily_recap_from_level_legs(levels, upstream, sent):
    y = "2026-11-20"
    ms.state.day_date["SPX"] = y
    t = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)
    raw_rows = [  # /levels/day rows: strategy / pick_id live in the pick jsonb
        {"id": "a", "trade_date": y, "opened_at": t.isoformat(),
         "closed_at": (t + timedelta(minutes=20)).isoformat(), "call": "up",
         "result": "win", "close_reason": "target", "strategy_pnl": 6.0,
         "entry_spot": 7255.0, "exit_spot": 7280.0, "pick": level_pick_dict("pa")},
        {"id": "b", "trade_date": y, "opened_at": (t + timedelta(hours=1)).isoformat(),
         "closed_at": (t + timedelta(hours=2)).isoformat(), "call": "down",
         "result": "loss", "close_reason": "invalidation", "strategy_pnl": -2.0,
         "pick": dict(level_pick_dict("pb"), strategy="bear_put")},
    ]
    upstream.day(y, {"trade_date": y, "legs": raw_rows, "day_summary": None})
    upstream.stats(results_stats(["win"] * 3, trade_date=y))
    upstream.decision(decision_with_pick())
    out = await ms.on_evaluation(_NoDB(), _poller(), levels)
    await ms.drain()
    assert "daily_recap" in out
    a = next(c for c in sent if c["state"] == "daily_recap")["attrs"]
    assert a["session_date"] == y and a["wins"] == "1" and a["losses"] == "1"
    assert a["best_result"] == "win" and a["best_strategy"] == "call_butterfly"
    assert a["worst_strategy"] == "bear_put" and a["best_pick_id"] == "pa"
    assert ms.state.last_recap_date["SPX"] == y
    # once only
    assert "daily_recap" not in await ms.on_evaluation(_NoDB(), _poller(), levels)


async def test_daily_recap_retries_when_day_unreachable(levels, upstream, sent):
    ms.state.day_date["SPX"] = "2026-11-20"
    upstream.stats(results_stats(["win"]))
    upstream.decision(decision_with_pick())
    assert "daily_recap" not in await ms.on_evaluation(_NoDB(), _poller(), levels)
    assert "SPX" not in ms.state.last_recap_date


async def test_recovery_reads_the_level_regime(levels):
    ms.state.level_regime["SPX"] = {"alert": True, "consec_wins": 3}
    assert ms._recovery_earned("SPX") is False
    ms.state.level_regime["SPX"] = {"alert": False, "consec_wins": 1}
    assert ms._recovery_earned("SPX") is True
