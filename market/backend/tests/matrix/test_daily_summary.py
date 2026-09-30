"""Matrix daily summary email: built from Matrix's own self-eval tables, sent
after the close, once per session, only to Matrix users who opted in."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import matrix_daily_summary as mds
from app.config import Settings
from app.db import Database

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 30)


@pytest.fixture(autouse=True)
def _reset():
    mds._state.update(inflight=False, handled=None, task=None)
    yield


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "daily.duckdb")
    d.connect()
    return d


def _settings(**kw):
    base = dict(symbols=["SPX", "NDX"], pick_min_dwell_polls=1,
                regime_alert_consec_losses=3, regime_clear_consec_wins=2)
    base.update(kw)
    return Settings(**base)


def _pick(db, sym, t, legs, strategy, result, polls=2):
    """One pick = `polls` consecutive polls holding `legs`, last one graded."""
    for i in range(polls):
        ts = t + timedelta(seconds=16 * i)
        did = db.insert_bias_decision({
            "ts": ts, "symbol": sym, "expiration": "2026-09-30", "spot_at_decision": 7700.0,
            "score": 0.0, "label": "neutral", "confidence": "medium",
            "put_wall_strike": None, "put_wall_strength": None, "put_wall_net_gex": None,
            "call_wall_strike": None, "call_wall_strength": None, "call_wall_net_gex": None,
            "recommended_strategies": "", "pick_legs": legs, "pick_entry_mid": 1.0,
            "pick_spread_type": "credit", "pick_strategy": strategy})
        if i == polls - 1 and result:
            db.insert_outcome({
                "decision_id": int(did), "evaluated_at": ts + timedelta(minutes=3),
                "spot_at_eval": 7700.0, "elapsed_minutes": 3.0,
                "predicted_direction": "down", "actual_move_pct": 0.0, "result": result,
                "entry_net_premium": 1.0, "eval_net_premium": 1.0,
                "favorable_delta": 0.1, "friction_band": 0.05, "spread_type": "credit"})
    return t + timedelta(seconds=16 * polls)


def _at(h, m):
    return datetime(2026, 9, 30, h, m, tzinfo=ET).astimezone(timezone.utc)


def _seed_day(db):
    t = _at(10, 0)
    seq = [("bear_call", "win"), ("bear_call", "loss"), ("iron_condor", "loss"),
           ("iron_condor", "loss"), ("bear_put", "win"), ("bear_put", "win"),
           ("bear_call", "neutral")]
    for i, (strat, res) in enumerate(seq):
        t = _pick(db, "SPX", t, f'[{{"k":{i}}}]', strat, res)
    return seq


# ── the report ──────────────────────────────────────────────────────────────

def test_report_counts_one_result_per_pick_by_strategy(db):
    _seed_day(db)
    r = mds.build_report(db, _settings(), DAY)
    spx = next(s for s in r["symbols"] if s["symbol"] == "SPX")
    assert (spx["wins"], spx["losses"], spx["neutrals"], spx["picks"]) == (3, 3, 1, 7)
    assert spx["right_pct"] == 50                       # 3 ÷ (3+3); flats left out
    assert spx["table"]["Bear Call"] == {"win": 1, "loss": 1, "neutral": 1}
    assert spx["table"]["Iron Condor"] == {"win": 0, "loss": 2, "neutral": 0}
    assert spx["table"]["Bear Put"] == {"win": 2, "loss": 0, "neutral": 0}


def test_pauses_follow_the_engines_own_rule(db):
    """3 losing picks in a row pause it; 2 winning picks lift it."""
    t = _at(10, 0)
    for i, res in enumerate(["loss", "loss", "loss", "win", "win", "loss"]):
        t = _pick(db, "SPX", t, f'[{{"p":{i}}}]', "bear_call", res)
    spx = mds.build_report(db, _settings(), DAY)["symbols"][0]
    assert (spx["pauses"], spx["recoveries"]) == (1, 1)
    assert spx["longest_losing_streak"] == 3


def test_a_flicker_is_not_counted(db):
    _pick(db, "SPX", _at(10, 0), '[{"a":1}]', "bear_call", "win", polls=1)
    spx = mds.build_report(db, _settings(pick_min_dwell_polls=3), DAY)["symbols"][0]
    assert spx["picks"] == 0


def test_only_that_session_is_reported(db):
    _pick(db, "SPX", _at(10, 0) - timedelta(days=1), '[{"y":1}]', "bear_call", "win")
    assert mds.build_report(db, _settings(), DAY)["any_picks"] is False


# ── the email ───────────────────────────────────────────────────────────────

def test_email_is_short_and_says_the_takeaway(db):
    _seed_day(db)
    subject, html = mds.render_email(mds.build_report(db, _settings(), DAY))
    assert subject == "Matrix daily · Sep 30: SPX 3–3 (right–wrong)"
    assert "right 3 · wrong 3 · flat 1 (7 picks)" in html
    assert "50% right when it called it" in html
    assert "Iron Condor" in html and "NDX — no picks today." in html
    assert "not advice" in html and "Daily summary email" in html


# ── recipients: Matrix users who opted in, and nobody else ──────────────────

async def test_recipients_are_matrix_users_who_opted_in(monkeypatch):
    async def _tool(tool):
        assert tool == "market", "Matrix users only"
        return ["u-on", "u-off", "u-missing"]

    async def _settings_rows(table, select, filters=None, **k):
        assert table == "user_settings"
        return [{"user_id": "u-on", "notifications": {"email_daily_summary": True}},
                {"user_id": "u-off", "notifications": {"email_daily_summary": False}},
                {"user_id": "u-missing", "notifications": {}}]

    async def _email(uid):
        return f"{uid}@example.com"

    monkeypatch.setattr(mds.supabase_admin, "get_users_with_tool", _tool)
    monkeypatch.setattr(mds.supabase_admin, "select_many", _settings_rows)
    monkeypatch.setattr(mds.supabase_admin, "get_user_email", _email)
    assert await mds.recipients() == ["u-on@example.com"]


async def test_supabase_down_means_unknown_not_nobody(monkeypatch):
    async def _tool(tool):
        return ["u1"]

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(mds.supabase_admin, "get_users_with_tool", _tool)
    monkeypatch.setattr(mds.supabase_admin, "select_many", _none)
    assert await mds.recipients() is None


# ── when it fires ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("h,m,weekday_date,expect", [
    (16, 14, date(2026, 9, 30), None),          # a minute early
    (16, 15, date(2026, 9, 30), date(2026, 9, 30)),
    (20, 0, date(2026, 9, 30), date(2026, 9, 30)),    # backend was down at 4:15 — still sends
    (16, 30, date(2026, 10, 3), None),          # Saturday
])
def test_due_at_4_15_pm_et_on_weekdays(h, m, weekday_date, expect):
    now = datetime.combine(weekday_date, datetime.min.time()).replace(
        hour=h, minute=m, tzinfo=ET)
    assert mds.due(_settings(), now) == expect


async def test_sends_once_and_records_it(db, monkeypatch):
    _seed_day(db)
    sent = []

    async def _rcpts():
        return ["a@example.com", "b@example.com"]

    async def _send(to, subject, html, **k):
        sent.append((to, subject, k.get("from_email")))
        return True
    monkeypatch.setattr(mds, "recipients", _rcpts)
    monkeypatch.setattr(mds.emailer, "send_email", _send)

    assert await mds.send_for(db, _settings(), DAY) == "sent 2/2"
    assert [s[0] for s in sent] == ["a@example.com", "b@example.com"]
    assert sent[0][2] == "Facades Matrix <noreply@facades.trade>"

    sent.clear()
    mds._state["handled"] = None                     # simulate a restart
    assert await mds.send_for(db, _settings(), DAY) == "already handled"
    assert sent == [], "a restart after 4:15 must not send it twice"


async def test_a_day_with_no_picks_sends_nothing(db, monkeypatch):
    async def _rcpts():
        raise AssertionError("no picks → no recipient lookup, no email")
    monkeypatch.setattr(mds, "recipients", _rcpts)
    assert await mds.send_for(db, _settings(), DAY) == "skipped"
    assert db.matrix_daily_email_handled(DAY)


async def test_unknown_recipients_retry_later(db, monkeypatch):
    _seed_day(db)

    async def _unknown():
        return None
    monkeypatch.setattr(mds, "recipients", _unknown)
    assert await mds.send_for(db, _settings(), DAY) == "retry"
    assert not db.matrix_daily_email_handled(DAY), "not recorded → the next sweep retries"


# ── pre-commit review fixes ─────────────────────────────────────────────────

async def test_a_failed_record_write_never_loops_the_send(db, monkeypatch):
    """Emails went out, then the DuckDB record failed. tick() must NOT send
    again on the next sweep — that would spam every recipient every ~30s."""
    _seed_day(db)
    sent = []

    async def _rcpts():
        return ["a@example.com"]

    async def _send(to, subject, html, **k):
        sent.append(to)
        return True

    def _boom(*a, **k):
        raise RuntimeError("duckdb write failed")
    monkeypatch.setattr(mds, "recipients", _rcpts)
    monkeypatch.setattr(mds.emailer, "send_email", _send)
    monkeypatch.setattr(db, "log_matrix_daily_email", _boom)

    await mds.send_for(db, _settings(), DAY)
    assert sent == ["a@example.com"]
    now = datetime(2026, 9, 30, 16, 20, tzinfo=ET)
    assert mds.due(_settings(), now) is None, "handled — the next sweep must not resend"


async def test_a_send_that_raises_midway_is_not_retried(db, monkeypatch):
    _seed_day(db)
    calls = []

    async def _rcpts():
        return ["a@example.com", "b@example.com"]

    async def _send(to, subject, html, **k):
        calls.append(to)
        if to == "a@example.com":
            raise RuntimeError("smtp hiccup")
        return True
    monkeypatch.setattr(mds, "recipients", _rcpts)
    monkeypatch.setattr(mds.emailer, "send_email", _send)
    assert await mds.send_for(db, _settings(), DAY) == "sent 1/2"
    assert calls == ["a@example.com", "b@example.com"], "one failure doesn't stop the rest"
    assert mds.due(_settings(), datetime(2026, 9, 30, 16, 20, tzinfo=ET)) is None


def test_a_dev_instance_never_sends():
    """DEVMODE runs on sandbox data but would email REAL users (Supabase isn't
    DEVMODE-split). It must never be due."""
    now = datetime(2026, 9, 30, 16, 30, tzinfo=ET)
    assert mds.due(_settings(devmode=True), now) is None
    assert mds.due(_settings(devmode=False), now) == DAY
