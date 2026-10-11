"""Simmer's DB-held setup (migration 0022): the two Supabase tables are the only
source for the ticker universe and engine tuning, refreshed in memory, fails
closed until loaded, and the seed reproduces the pre-DB resolved config.

The seed these tests load (SIMMER_TICKER_SEED / SIMMER_ENGINE_SEED) is read
straight from the migration file by conftest, so tests and database can't drift.
"""
from __future__ import annotations

import pytest

from app import simmer_config as sc
from app import supabase_admin

from .conftest import SIMMER_TICKER_SEED, SIMMER_ENGINE_SEED


# ── Tables are the only source ──────────────────────────────────────────────
def test_universe_comes_from_the_ticker_table():
    # The autouse fixture already loaded the ticker seed.
    assert sc.tickers() == [
        "AAPL", "AMD", "AMZN", "AVGO", "GOOGL", "META", "MSFT", "MU", "NFLX",
        "NVDA", "PLTR", "SMCI", "TSLA",
    ]
    assert sc.index_data_symbols() == ["SPY", "QQQ", "VIX"]
    assert sc.blocked_tickers() == {"SPX", "NDX", "RUT", "DJX", "VIX"}


def test_per_ticker_rule_from_the_table():
    assert sc.ticker_rule("NVDA")["min_open_interest"] == 1000     # seeded override
    assert sc.ticker_rule("TSLA")["min_open_interest"] == 1000
    assert sc.ticker_rule("AAPL")["min_open_interest"] == sc.gates()["min_open_interest"]
    assert sc.ticker_rule("SPY")["is_index"] is True
    assert sc.ticker_rule("SPY")["eligible"] is False
    spx = sc.ticker_rule("SPX")
    assert spx["eligible"] is False and spx["ineligible_reason"] == "index_product_excluded_from_trading"


def test_spot_symbol_map_from_the_table():
    assert sc.yahoo_symbol("VIX") == "^VIX"
    assert sc.yahoo_symbol("VIX3M") == "^VIX3M"
    assert sc.yahoo_symbol("NVDA") is None            # equities map 1:1 (bare)


def test_empty_snapshot_is_empty_universe():
    sc._TICKERS.clear()
    assert sc.tickers() == [] and sc.index_data_symbols() == [] and sc.blocked_tickers() == set()


# ── Engine sections deep-merge over code defaults ───────────────────────────
def test_engine_section_merges_over_code_defaults():
    sc.apply_engine_settings([{"section": "gates", "settings": {"dte_min": 0}}])
    g = sc.gates()
    assert g["dte_min"] == 0                      # from the table
    assert g["dte_max"] == sc.GATES["dte_max"]    # code default preserved
    assert g["iv_percentile_floor"] == sc.GATES["iv_percentile_floor"]


def test_seed_reproduces_production_resolved_config():
    """Loading the full engine seed yields exactly today's (JSON-era) values."""
    sc.apply_engine_settings(SIMMER_ENGINE_SEED)
    g = sc.gates()
    assert g["dte_min"] == 0 and g["dte_max"] == 45
    assert g["iv_percentile_floor"] == 40.0 and g["vrp_ratio_floor"] == 1.15
    s = sc.social()
    assert s["enabled"] is True and s["handle"] == "@facades_simmer"
    assert s["x_url"] == "https://x.com/facades_simmer"
    # code-default social keys still present via the deep-merge
    assert s["share_enabled"] is True and "x_share_url" in s
    assert sc.engine_version() == "simmer-engine-1.0.0"


# ── Fails closed / refresh resilience ───────────────────────────────────────
def test_setup_loaded_flag():
    sc._STATE["loaded"] = False
    assert sc.setup_loaded() is False
    sc.apply_setup(SIMMER_TICKER_SEED, SIMMER_ENGINE_SEED)
    assert sc.setup_loaded() is True


async def test_failed_refresh_keeps_last_copy(monkeypatch):
    sc.apply_setup(SIMMER_TICKER_SEED, SIMMER_ENGINE_SEED)
    before = dict(sc._TICKERS)

    async def _none(*a, **k):
        return None
    monkeypatch.setattr(supabase_admin, "get_simmer_tickers_setup", _none)
    monkeypatch.setattr(supabase_admin, "get_simmer_engine_settings", _none)
    assert await sc.refresh_setup() is False          # read failed
    assert sc._TICKERS == before and sc.setup_loaded() is True   # last good copy intact


async def test_refresh_loads_from_supabase(monkeypatch):
    sc._TICKERS.clear(); sc._ENGINE.clear(); sc._STATE["loaded"] = False

    async def _tickers(*a, **k):
        return [{"ticker": "NVDA", "sort_order": 1, "default_watchlist": True,
                 "enabled": True, "min_open_interest": 1000}]

    async def _engine(*a, **k):
        return [{"section": "gates", "settings": {"dte_min": 3}}]
    monkeypatch.setattr(supabase_admin, "get_simmer_tickers_setup", _tickers)
    monkeypatch.setattr(supabase_admin, "get_simmer_engine_settings", _engine)
    assert await sc.refresh_setup() is True
    assert sc.tickers() == ["NVDA"] and sc.gates()["dte_min"] == 3


# ── Fingerprint + locked-gate contract ──────────────────────────────────────
def test_fingerprint_changes_with_verdict_affecting_settings():
    sc.apply_engine_settings([])
    fp0 = sc.settings_fingerprint()
    sc.apply_engine_settings([{"section": "gates", "settings": {"dte_min": 0}}])
    assert sc.settings_fingerprint() != fp0


def test_locked_and_toggleable_gates_stay_in_code():
    # The safety contract is a code constant, not table-driven tuning.
    assert "catalyst_lockout" in sc.LOCKED_GATES
    assert "squeeze_veto" in sc.TOGGLEABLE_GATES


def test_resolved_carries_the_fingerprint():
    r = sc.resolved("NVDA")
    assert r["settings_fingerprint"] == sc.settings_fingerprint()
    assert r["engine_version"] == sc.engine_version()
