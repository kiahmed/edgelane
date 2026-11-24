"""/accuracy/SPX in levels mode: the same contract, from level legs (§3.4, §12)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app import level_accuracy as la
from app.db import Database
from app.evaluator import state as evaluator_state
from app.routes import accuracy as acc_route

from .conftest import leg, now_utc, results_stats, stats_body, use_settings


@pytest.fixture
def engine_db(tmp_path, monkeypatch):
    from app import main as _main
    d = Database(tmp_path / "acc.duckdb")
    d.connect()
    monkeypatch.setattr(_main, "_db", d, raising=False)
    return d


async def test_contract_keys_identical_in_both_modes(engine_db, upstream, monkeypatch):
    engine = await acc_route.get_accuracy("SPX")
    use_settings(monkeypatch, matrix_decision_source="levels")
    upstream.stats(results_stats(["win", "loss", "win"]))
    levels = await acc_route.get_accuracy("SPX")
    assert set(levels) == set(engine)
    assert levels["recent_outcomes"], "level legs become recent_outcomes rows"
    legacy_row_keys = {'id', 'ts', 'label', 'spot_at_decision', 'evaluated_at',
                       'spot_at_eval', 'actual_move_pct', 'predicted_direction',
                       'result', 'pick_strategy', 'spread_type', 'entry_net_premium',
                       'eval_net_premium', 'favorable_delta', 'friction_band'}
    row = levels["recent_outcomes"][0]
    assert legacy_row_keys <= set(row)
    assert {"close_reason", "target_level", "progress_pct"} <= set(row)


async def test_engine_mode_ignores_the_level_engine(engine_db, upstream):
    upstream.stats(results_stats(["win"] * 12))
    out = await acc_route.get_accuracy("SPX")
    assert out["n"] == 0 and out["evaluator_error"] is None
    assert upstream.calls == [], "engine mode must never call the level engine"


async def test_levels_counts_are_binary_over_the_rolling_window(levels, upstream, engine_db):
    seq = ["loss"] * 5 + ["win"] * 15 + ["loss"] * 5     # 25 legs; window = 20
    upstream.stats(results_stats(seq, trade_date="2026-11-20"))
    out = await acc_route.get_accuracy("SPX")
    assert (out["n"], out["wins"], out["losses"], out["neutrals"]) == (20, 15, 5, 0)
    assert out["accuracy_pct"] == 75.0 and out["tier"] == "green"
    assert out["window"] == 20
    assert out["state"] == "in_sync" and out["win_rate"] == 75.0
    assert out["evaluator_error"] is None
    assert out["last_evaluated_at"]
    assert upstream.calls[0] == ("/levels/stats/SPX", {"limit": 50})


async def test_recent_outcome_row_mapping(levels, upstream, engine_db):
    t = now_utc() - timedelta(hours=1)
    l = leg(7, "win", opened=t, pick_id="p7", strategy="bull_call", call="up", pnl=2.5)
    upstream.stats(stats_body([l]))
    row = (await acc_route.get_accuracy("SPX"))["recent_outcomes"][0]
    assert row["id"] == "leg-7"
    assert row["ts"] == l["opened_at"] and row["evaluated_at"] == l["closed_at"]
    assert row["spot_at_decision"] == 7255.0 and row["spot_at_eval"] == 7279.0
    assert row["actual_move_pct"] == round((7279.0 - 7255.0) / 7255.0 * 100, 4)
    assert row["predicted_direction"] == "up" and row["result"] == "win"
    assert row["pick_strategy"] == "bull_call" and row["spread_type"] == "debit"
    assert row["favorable_delta"] == 2.5
    assert row["entry_net_premium"] is None and row["friction_band"] is None
    assert row["close_reason"] == "target" and row["target_level"]["name"] == "R1"
    assert row["pick_id"] == "p7" and row["label"] == "Near"


async def test_pre_binary_results_count_as_losses_and_open_legs_do_not_count(levels, upstream, engine_db):
    t = now_utc() - timedelta(hours=3)
    legs = [leg(0, "win", opened=t), leg(1, "flipped", opened=t + timedelta(minutes=30)),
            leg(2, "unresolved", opened=t + timedelta(minutes=60))]
    body = stats_body(legs, open_leg=leg(3, None, opened=t + timedelta(minutes=90)))
    upstream.stats(body)
    out = await acc_route.get_accuracy("SPX")
    assert (out["n"], out["wins"], out["losses"]) == (3, 1, 2)
    assert all(r["result"] in ("win", "loss") for r in out["recent_outcomes"])


async def test_calibrating_until_min_graded(levels, upstream, engine_db):
    upstream.stats(results_stats(["win"] * 9, trade_date="2026-11-20"))
    out = await acc_route.get_accuracy("SPX")
    assert out["state"] == "calibrating" and out["win_rate"] is None
    assert out["display_text"] == "Calibrating — 9/10 graded"


async def test_regime_replays_todays_legs_with_legacy_thresholds(levels, upstream, engine_db):
    upstream.stats(results_stats(["win"] * 10 + ["loss"] * 3))
    out = await acc_route.get_accuracy("SPX")
    assert out["regime_alert"] is True and out["state"] == "paused"
    assert out["regime_consec_losses"] == 3 and out["regime_consec_wins"] == 0
    assert out["wins_to_resume"] == 2 and out["win_rate"] is None
    assert out["regime_triggered_at"] == out["recent_outcomes"][0]["evaluated_at"]

    la.clear_cache()
    upstream.stats(results_stats(["win"] * 10 + ["loss"] * 3 + ["win"]))
    out = await acc_route.get_accuracy("SPX")
    assert out["regime_alert"] is True and out["wins_to_resume"] == 1

    la.clear_cache()
    upstream.stats(results_stats(["win"] * 10 + ["loss"] * 3 + ["win"] * 2))
    out = await acc_route.get_accuracy("SPX")
    assert out["regime_alert"] is False and out["regime_triggered_at"] is None
    assert out["state"] == "in_sync"


async def test_yesterdays_streak_does_not_pause_today(levels, upstream, engine_db):
    upstream.stats(results_stats(["win"] * 10 + ["loss"] * 3, trade_date="2026-11-20"))
    out = await acc_route.get_accuracy("SPX")
    assert out["regime_alert"] is False and out["regime_consec_losses"] == 0


async def test_unreachable_is_unavailable_not_legacy_numbers(levels, upstream, engine_db):
    evaluator_state.regime_alert_active_by_symbol["SPX"] = True
    evaluator_state.consec_losses_by_symbol["SPX"] = 7
    out = await acc_route.get_accuracy("SPX")         # no fake route → unreachable
    assert out["evaluator_error"] == "level engine unavailable"
    assert out["n"] == 0 and out["recent_outcomes"] == []
    assert out["regime_alert"] is False and out["regime_consec_losses"] == 0
    assert out["display_text"] == "Level engine unavailable"


async def test_stats_are_cached_briefly(levels, upstream):
    upstream.stats(results_stats(["win"]))
    a = await la.level_stats(levels)
    b = await la.level_stats(levels)
    assert a == b and len(upstream.calls) == 1
    assert la.cached_stats() == a


async def test_level_stats_never_raises(levels, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(la.level_pick, "get_upstream", _boom)
    assert await la.level_stats(levels) is None


async def test_ndx_stays_on_the_legacy_path_in_levels_mode(levels, upstream, engine_db):
    out = await acc_route.get_accuracy("NDX")
    assert out["symbol"] == "NDX" and upstream.calls == []
