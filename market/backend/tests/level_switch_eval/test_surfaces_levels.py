"""Levels mode on the Matrix read surfaces: /matrix/state + /matrix/snap, the
ledger row, the snap cards' names, and the daily email."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app import level_accuracy, matrix_daily_summary as mds, matrix_ledger, matrix_snap
from app.db import Database
from app.poller import state as poller_state
from app.routes import matrix as mroute

from .conftest import PICK_ID, decision_with_pick, level_pick_dict, results_stats

DAY = date(2026, 11, 20)


# ── /matrix/state + /matrix/snap ────────────────────────────────────────────

async def test_state_pick_and_grid_follow_the_level_engine(levels, upstream):
    upstream.decision(decision_with_pick())
    before = poller_state.latest_by_symbol["SPX"]["engine_pick"]["strategy"]
    pick = await mroute.matrix_state_block("SPX", block="pick")
    assert pick["data"]["pick"]["pick_id"] == PICK_ID
    grid = await mroute.matrix_state_block("SPX", block="grid")
    assert list(grid["data"]["grid"]) == ["call_butterfly"]
    assert grid["data"]["grid"]["call_butterfly"]["health"] == "healthy"
    assert poller_state.latest_by_symbol["SPX"]["engine_pick"]["strategy"] == before == "bear_call"


async def test_state_wait_shows_no_pick_and_empty_grid(levels, upstream):
    upstream.decision({"call": "wait", "pick": None, "status": "ok"})
    assert (await mroute.matrix_state_block("SPX", block="pick"))["data"]["pick"] is None
    assert (await mroute.matrix_state_block("SPX", block="grid"))["data"]["grid"] == {}


async def test_state_win_eval_from_level_stats(levels, upstream):
    upstream.stats(results_stats(["win"] * 9 + ["loss"] * 3, trade_date="2026-11-20"))
    out = (await mroute.matrix_state_block("SPX", block="win_eval"))["data"]
    assert out["stats"] == {"n": 12, "wins": 9, "losses": 3, "neutrals": 0,
                            "accuracy_pct": 75.0}
    assert out["trust"]["state"] == "in_sync" and out["trust"]["tier"] == "green"


async def test_state_win_eval_unavailable_is_explicit(levels, upstream):
    out = (await mroute.matrix_state_block("SPX", block="win_eval"))["data"]
    assert out["stats"]["n"] == 0 and out["stats"]["error"] == "level engine unavailable"


async def test_snap_card_renders_the_level_pick(levels, upstream):
    upstream.decision(decision_with_pick())
    html = (await mroute.matrix_snap("SPX", view="engine_pick")).body.decode()
    assert "Call Fly" in html and "Risk / reward" in html and "Bear Call" not in html


async def test_engine_mode_state_reads_the_poller_cache(upstream):
    pick = await mroute.matrix_state_block("SPX", block="pick")
    assert pick["data"]["pick"]["strategy"] == "bear_call"
    assert upstream.calls == []


# ── ledger ──────────────────────────────────────────────────────────────────

def test_ledger_row_uses_the_given_record(monkeypatch):
    def _boom(sym):
        raise AssertionError("precomputed record must be used")
    monkeypatch.setattr(mroute, "_accuracy_view", _boom)
    trust = {"state": "in_sync", "win_rate": 75.0, "graded": 12, "tier": "green"}
    stats = {"n": 12, "wins": 9, "losses": 3, "neutrals": 0, "accuracy_pct": 75.0}
    snap = {"symbol": "SPX", "expiration": "2026-11-23", "engine_pick": level_pick_dict(),
            "strategies": {}, "bias": {}}
    row = matrix_ledger.build_row(event_id="e", symbol="SPX", state="win_rate_notable",
                                  expiry="", attrs={}, snap=snap, accuracy=(trust, stats))
    assert row["data"]["win_eval"]["stats"] == stats
    assert "75%" in row["snap_html"]


def test_sync_accuracy_view_uses_cached_level_stats(levels):
    level_accuracy._last_good = results_stats(["win"] * 10, trade_date="2026-11-20")
    trust, stats = mroute._accuracy_view("SPX")
    assert stats["n"] == 10 and trust["state"] == "in_sync"


def test_strategy_names_cover_the_level_grid():
    from level_engine.strategy import NAMES
    for key in NAMES:
        assert key in matrix_snap._STRATEGY_NAMES, key
        assert key in mds._NAMES, key


# ── daily email ─────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "daily.duckdb")
    d.connect()
    return d


def _row(i, result, strategy, level, call="up", reason=None):
    t = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc) + timedelta(minutes=30 * i)
    pick = dict(level_pick_dict(f"p{i}"), strategy=strategy,
                target_level={"price": 7280.0 + i, "name": level})
    return {"id": f"2026-11-20:{i}", "trade_date": "2026-11-20",
            "opened_at": t.isoformat(),
            "closed_at": (t + timedelta(minutes=20)).isoformat() if result else None,
            "call": call, "result": result, "close_reason": reason,
            "entry_spot": 7255.0, "exit_spot": 7260.0, "strategy_pnl": 1.0, "pick": pick}


def _day():
    return {"trade_date": "2026-11-20", "levels": [], "legs": [
        _row(0, "win", "call_butterfly", "R1", reason="target"),
        _row(1, "loss", "call_butterfly", "R2", reason="invalidation"),
        _row(2, "loss", "bear_put", "S1", call="down", reason="flipped"),
        _row(3, "loss", "bear_put", "S1", call="down", reason="session_close"),
        _row(4, "win", "long_call", "R1", reason="target"),
        _row(5, None, "straddle", "R1"),                       # open — not counted
    ], "day_summary": {"close": 7266.5, "legs": {"n": 5}}}


def test_level_report_strategy_and_level_tables(levels, db):
    r = mds.build_report(db, levels, DAY, level_day=_day())
    assert r["source"] == "levels" and r["any_picks"] is True
    (spx,) = r["symbols"]
    assert (spx["picks"], spx["wins"], spx["losses"], spx["neutrals"]) == (5, 2, 3, 0)
    assert spx["right_pct"] == 40
    assert spx["table"] == {"Call Fly": {"win": 1, "loss": 1},
                            "Bear Put": {"win": 0, "loss": 2},
                            "Long Call": {"win": 1, "loss": 0}}
    assert spx["by_level"] == {"R1": {"win": 2, "loss": 0}, "S1": {"win": 0, "loss": 2},
                               "R2": {"win": 0, "loss": 1}}
    assert spx["pauses"] == 1          # 3 losses in a row trip the legacy threshold
    assert spx["day_summary"]["close"] == 7266.5


def test_level_email_is_win_loss_not_right_wrong_flat(levels, db):
    subject, html = mds.render_email(mds.build_report(db, levels, DAY, level_day=_day()))
    assert subject == "Matrix daily · Nov 20: SPX 2–3 (win–loss)"
    assert "win 2 · loss 3 (5 legs) · 40% win rate" in html
    assert ">Win<" in html and ">Loss<" in html and ">Flat<" not in html
    assert ">Level<" in html and "S1" in html and "Long Call" in html
    assert "Closed 7,266.50" in html and "not advice" in html


async def test_send_for_retries_when_the_level_engine_is_down(levels, db, upstream, monkeypatch):
    async def _rcpts():
        raise AssertionError("no report → no recipients lookup")
    monkeypatch.setattr(mds, "recipients", _rcpts)
    assert await mds.send_for(db, levels, DAY) == "retry"
    assert not db.matrix_daily_email_handled(DAY)


async def test_send_for_levels_sends_the_level_report(levels, db, upstream, monkeypatch):
    upstream.day("2026-11-20", _day())
    sent = []

    async def _rcpts():
        return ["a@example.com"]

    async def _send(to, subject, html, **k):
        sent.append(subject)
        return True
    monkeypatch.setattr(mds, "recipients", _rcpts)
    monkeypatch.setattr(mds.emailer, "send_email", _send)
    assert await mds.send_for(db, levels, DAY) == "sent 1/1"
    assert sent == ["Matrix daily · Nov 20: SPX 2–3 (win–loss)"]
    assert db.matrix_daily_email_handled(DAY)


async def test_send_for_levels_no_legs_is_skipped(levels, db, upstream, monkeypatch):
    upstream.day("2026-11-20", {"trade_date": "2026-11-20", "legs": [], "day_summary": None})
    assert await mds.send_for(db, levels, DAY) == "skipped"


def test_engine_mode_report_unchanged_shape(db):
    from app.config import Settings
    r = mds.build_report(db, Settings(symbols=["SPX", "NDX"]), DAY)
    assert "source" not in r and [s["symbol"] for s in r["symbols"]] == ["SPX", "NDX"]
