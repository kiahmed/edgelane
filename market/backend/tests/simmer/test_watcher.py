"""Watcher sweep: resilience, tier-1 TTLs, mandatory invalidation, dispersion,
union-of-watchlists dedup. No network — FakeSimmerTradier + a temp DuckDB."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import simmer_config
from app import simmer_watcher as sw
from app import supabase_admin

from .conftest import EXP, FakeSimmerTradier, readiness_env


def _naive_utc(**delta) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**delta)


async def _union(pairs):
    return list(pairs)


# ── Sweep resilience ────────────────────────────────────────────────────────
async def test_one_bad_symbol_does_not_kill_the_sweep(fresh_db, monkeypatch):
    client = FakeSimmerTradier(fail_symbols={"BAD"})
    monkeypatch.setattr(sw, "watchlist_union",
                        lambda: _union([("NVDA", None), ("BAD", None), ("AMD", None)]))
    results = await sw.sweep(client, fresh_db)

    good_keys = {k.split("|")[0] for k in results}
    assert good_keys == {"NVDA", "AMD"}
    assert "BAD" in sw.state.last_error_by_symbol
    assert "synthetic quote failure" in sw.state.last_error_by_symbol["BAD"]
    # good symbols landed in the route-facing cache and cleared their error slot
    assert f"NVDA|{EXP}" in sw.state.latest_by_key
    assert "NVDA" not in sw.state.last_error_by_symbol
    assert sw.state.last_sweep_at is not None
    assert sw.state.sweep_count == 1


async def test_sweep_persists_readiness_rows(fresh_db, monkeypatch):
    client = FakeSimmerTradier()
    monkeypatch.setattr(sw, "watchlist_union", lambda: _union([("NVDA", None)]))
    await sw.sweep(client, fresh_db)
    row = fresh_db.latest_simmer_readiness("NVDA", EXP)
    assert row is not None
    assert row["symbol"] == "NVDA"
    assert row["engine_version"] == simmer_config.engine_version()
    # cold start: no IV history → the volatility gate vetoes, score stays NULL
    assert row["vetoed"] is True
    assert row["score"] is None


# ── Tier-1 cache TTLs ───────────────────────────────────────────────────────
@pytest.fixture()
def refresh_counters(monkeypatch):
    calls = {"news": 0, "catalysts": 0, "daily": 0}

    async def _news(db, symbol, row):
        calls["news"] += 1
        return {"news_at": _naive_utc()}

    async def _cats(db, symbol, row):
        calls["catalysts"] += 1
        return {"catalyst_at": _naive_utc(), "_catalysts": []}

    async def _daily(db, symbol, row, provider=None):
        calls["daily"] += 1
        return {"daily_at": _naive_utc()}

    monkeypatch.setattr(sw, "_refresh_news", _news)
    monkeypatch.setattr(sw, "_refresh_catalysts", _cats)
    monkeypatch.setattr(sw, "_refresh_daily", _daily)
    return calls


async def test_fresh_cache_skips_every_refresh(fresh_db, refresh_counters):
    fresh_db.upsert_simmer_research({
        "symbol": "NVDA",
        "news_at": _naive_utc(minutes=1),
        "catalyst_at": _naive_utc(minutes=5),
        "daily_at": _naive_utc(hours=1),
        "rv20_yz": 0.25, "iv_history_json": "[0.3]",
    })
    row = await sw.load_research(fresh_db, "NVDA")
    assert refresh_counters == {"news": 0, "catalysts": 0, "daily": 0}
    assert row["_invalidated"] == []
    # cache hit still hydrates the engine-facing transients from persisted twins
    assert row["_rv_yz"] == 0.25
    assert row["_iv_history"] == [0.3]


async def test_pre_upgrade_row_forces_daily_refresh(fresh_db, refresh_counters):
    """A fresh daily_at with NO persisted vol block is the pre-schema-upgrade
    (and formerly live-broken) state: it must refresh, not serve an empty vol
    block that vetoes every candidate with vrp_unavailable."""
    fresh_db.upsert_simmer_research({
        "symbol": "NVDA",
        "news_at": _naive_utc(minutes=1),
        "catalyst_at": _naive_utc(minutes=5),
        "daily_at": _naive_utc(hours=1),        # fresh stamp, but...
        # rv20_yz / iv_history_json absent
    })
    await sw.load_research(fresh_db, "NVDA")
    assert refresh_counters["daily"] == 1


async def test_expired_groups_refresh_independently(fresh_db, refresh_counters):
    fresh_db.upsert_simmer_research({
        "symbol": "NVDA",
        "news_at": _naive_utc(minutes=30),      # > 900s ticker_sentiment TTL
        "catalyst_at": _naive_utc(minutes=5),   # fresh (24h TTL)
        "daily_at": _naive_utc(hours=1),        # fresh (24h TTL)
        # a genuinely-fresh daily group carries its persisted vol block; a
        # fresh stamp WITHOUT it is the pre-upgrade/broken state and refreshes
        "rv20_yz": 0.25, "iv_history_json": "[0.3]",
    })
    await sw.load_research(fresh_db, "NVDA")
    assert refresh_counters == {"news": 1, "catalysts": 0, "daily": 0}


async def test_empty_cache_refreshes_everything(fresh_db, refresh_counters):
    await sw.load_research(fresh_db, "NVDA")
    assert refresh_counters == {"news": 1, "catalysts": 1, "daily": 1}


# ── Mandatory invalidation ──────────────────────────────────────────────────
async def test_hard_block_filing_voids_fresh_ttls(fresh_db, refresh_counters):
    from app.simmer_catalysts import Catalyst, persist_catalysts
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    fresh_db.upsert_simmer_research({
        "symbol": "NVDA",
        "news_at": _naive_utc(minutes=1),
        "catalyst_at": _naive_utc(minutes=1),
        "daily_at": _naive_utc(minutes=1),
    })
    # A blocking 8-K accepted NOW (after catalyst_at) — every TTL is voided.
    persist_catalysts(fresh_db, [Catalyst(
        id="0001104659-26-000001", symbol="NVDA", blocking=True,
        acceptance_at=dt.now(ZoneInfo("America/New_York")) + timedelta(minutes=5),
    )])
    row = await sw.load_research(fresh_db, "NVDA")
    assert "hard_block_filing" in row["_invalidated"]
    assert refresh_counters == {"news": 1, "catalysts": 1, "daily": 1}


async def test_velocity_burst_invalidates(fresh_db, refresh_counters):
    fresh_db.upsert_simmer_research({
        "symbol": "NVDA",
        "velocity_tier": "alert",
        "news_at": _naive_utc(minutes=1),
        "catalyst_at": _naive_utc(minutes=1),
        "daily_at": _naive_utc(minutes=1),
    })
    row = await sw.load_research(fresh_db, "NVDA")
    assert "velocity_burst" in row["_invalidated"]
    assert refresh_counters["daily"] == 1


async def test_earnings_date_change_invalidates(fresh_db, refresh_counters):
    import json
    fresh_db.upsert_simmer_research({
        "symbol": "NVDA",
        "catalyst_flags": json.dumps({"earnings_date": "2026-08-20"}),
        "news_at": _naive_utc(minutes=1),
        "catalyst_at": _naive_utc(minutes=1),
        "daily_at": _naive_utc(minutes=1),
    })
    # DB now says earnings landed on a DIFFERENT date than the cache believes.
    conn = fresh_db.connect()
    with fresh_db._lock:
        conn.execute(
            "INSERT INTO simmer_catalysts (id, symbol, event_type, event_date, "
            "confirmed, blocking, acceptance_at, created_at) "
            "VALUES ('ern-1', 'NVDA', 'earnings', DATE '2026-08-27', true, false, "
            "now(), now())")
    row = await sw.load_research(fresh_db, "NVDA")
    assert "earnings_date_changed" in row["_invalidated"]


# ── Cross-sectional dispersion ──────────────────────────────────────────────
def _results(specs):
    return {f"{sym}|{EXP}": readiness_env(sym, ivp_eff=ivp) for sym, ivp in specs}


def test_dispersion_triggers_in_calm_regime():
    results = _results([("NVDA", 90.0), ("AMD", 88.0), ("MU", 85.0), ("KO", 30.0)])
    disp = sw.apply_dispersion(results, {"calm": True, "state": "contango"})
    assert disp["triggered"] is True
    assert set(disp["spiking"]) == {"NVDA", "AMD", "MU"}
    # default sector tag is "unknown" → one cluster covering all four names
    for env in results.values():
        assert env.get("sector_dispersion_suppressed") is True
        assert "sector_dispersion" in env["avoid_if"]


def test_dispersion_does_not_trigger_when_regime_not_calm():
    results = _results([("NVDA", 90.0), ("AMD", 88.0), ("MU", 85.0)])
    disp = sw.apply_dispersion(results, {"calm": False, "state": "backwardation"})
    assert disp["triggered"] is False
    assert all(not env.get("sector_dispersion_suppressed") for env in results.values())


def test_dispersion_below_threshold_is_quiet():
    results = _results([("NVDA", 90.0), ("AMD", 30.0), ("MU", 25.0), ("KO", 20.0)])
    disp = sw.apply_dispersion(results, {"calm": True, "state": "contango"})
    assert disp["triggered"] is False


def test_dispersion_suppression_scoped_to_sector(monkeypatch):
    sectors = {"NVDA": "semis", "AMD": "semis", "MU": "semis", "KO": "staples"}
    real_rule = simmer_config.ticker_rule
    monkeypatch.setattr(
        simmer_config, "ticker_rule",
        lambda sym: {**real_rule(sym), "sector": sectors.get(sym.upper(), "unknown")})
    results = _results([("NVDA", 90.0), ("AMD", 88.0), ("MU", 85.0), ("KO", 30.0)])
    sw.apply_dispersion(results, {"calm": True, "state": "contango"})
    assert results[f"NVDA|{EXP}"].get("sector_dispersion_suppressed") is True
    assert results[f"KO|{EXP}"].get("sector_dispersion_suppressed") is None


# ── Union-of-watchlists dedup ───────────────────────────────────────────────
async def test_watchlist_union_dedups_across_users(monkeypatch):
    async def fake_select_many(table, select="*", filters=None, **kw):
        assert table == "simmer_watchlist"
        assert filters == {"active": "eq.true"}
        return [   # three users; NVDA watched twice at the same (null) expiry,
                   # AMD at two DIFFERENT expiries → distinct tier-2 keys
            {"symbol": "NVDA", "expiration": None, "active": True},
            {"symbol": "nvda", "expiration": None, "active": True},
            {"symbol": "AMD", "expiration": "2026-09-18", "active": True},
            {"symbol": "AMD", "expiration": "2026-10-16", "active": True},
            {"symbol": "AMD", "expiration": "2026-09-18", "active": True},
        ]
    monkeypatch.setattr(supabase_admin, "select_many", fake_select_many)
    pairs = await sw.watchlist_union()
    assert pairs == [("NVDA", None), ("AMD", "2026-09-18"), ("AMD", "2026-10-16")]
    assert sw.state.watchlist_source == "supabase"


async def test_watchlist_union_falls_back_to_config(monkeypatch):
    async def none(*a, **kw):
        return None
    monkeypatch.setattr(supabase_admin, "select_many", none)
    pairs = await sw.watchlist_union()
    assert pairs == [(t, None) for t in simmer_config.tickers()]
    assert sw.state.watchlist_source == "config_fallback"


# ── Regime degradation ──────────────────────────────────────────────────────
async def test_regime_degrades_to_calm_without_vix3m():
    client = FakeSimmerTradier(vix=15.0, vix3m=None)
    reg = await sw.compute_regime(client)
    assert reg["state"] == "calm" and reg["calm"] is True
    assert reg["vix_ts"] is None
    assert "no_term_structure_degraded_to_calm" in reg["data_quality"]


async def test_regime_backwardation_from_term_structure():
    client = FakeSimmerTradier(vix=22.0, vix3m=20.0)   # ratio 1.10 → stressed band edge
    reg = await sw.compute_regime(client)
    assert reg["vix_ts"] == pytest.approx(1.10)
    assert reg["state"] in ("backwardation", "stressed")
    assert reg["calm"] is False
    assert "bull_put" in reg["suppress"]


# ── IV-history close-capture window ──────────────────────────────────────────
from datetime import datetime as _dt
from zoneinfo import ZoneInfo as _ZI

_ET = _ZI("America/New_York")


def test_capture_window_weekday_only_after_the_close():
    # Monday 2026-08-17
    assert sw._is_capture_window(now=_dt(2026, 8, 17, 16, 5, tzinfo=_ET)) is True
    assert sw._is_capture_window(now=_dt(2026, 8, 17, 10, 0, tzinfo=_ET)) is False
    assert sw._is_capture_window(now=_dt(2026, 8, 17, 8, 0, tzinfo=_ET)) is False


def test_capture_window_weekend_captures_last_session():
    assert sw._is_capture_window(now=_dt(2026, 8, 15, 11, 0, tzinfo=_ET)) is True  # Sat
    assert sw._is_capture_window(now=_dt(2026, 8, 16, 20, 0, tzinfo=_ET)) is True  # Sun


async def test_record_iv_history_skips_mid_session(fresh_db, monkeypatch):
    monkeypatch.setattr(sw, "_is_capture_window", lambda *a, **k: False)
    results = {f"NVDA|{EXP}": readiness_env("NVDA")}
    n = await sw.record_iv_history(fresh_db, results)
    assert n == 0
    # NOT stamped mid-session → the same day still captures once it closes.
    assert sw.state.last_iv_history_date is None
    assert fresh_db.fetch_simmer_iv_history("NVDA", 5) == []


async def test_record_iv_history_captures_after_close(fresh_db, monkeypatch):
    monkeypatch.setattr(sw, "_is_capture_window", lambda *a, **k: True)
    results = {f"NVDA|{EXP}": readiness_env("NVDA")}
    n = await sw.record_iv_history(fresh_db, results)
    assert n == 1
    hist = fresh_db.fetch_simmer_iv_history("NVDA", 5)
    assert len(hist) == 1 and hist[0]["symbol"] == "NVDA"
