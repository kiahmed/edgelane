"""Torque config: ticker rules, strategy registry, file overrides."""
from __future__ import annotations

import json

import pytest

from app import torque_config as tc


def test_all_strategies_present():
    keys = {s["key"] for s in tc.strategy_list()}
    assert keys == {
        "long_call", "long_put", "bull_call", "bear_put", "bull_put",
        "bear_call", "iron_condor", "iron_fly", "call_fly", "put_fly",
    }
    assert len(keys) == 10


def test_every_directional_strategy_has_a_dir():
    # Regression: long_call/long_put/call_fly/put_fly originally had no "dir",
    # so the frontend's Counter Read confirms/diverging check (sdef.dir/cdef.dir)
    # silently never fired for those pairs — including long_call, the DEFAULT
    # strategy. iron_condor/iron_fly are exempt: the frontend self-splits their
    # own legs (put wing/call wing) and hardcodes bull/bear there, no "dir" needed.
    for s in tc.strategy_list():
        if s["key"] in ("iron_condor", "iron_fly"):
            continue
        assert s.get("dir") in ("bull", "bear"), s["key"]


def test_default_ticker_and_strategy():
    assert tc.tickers()[0] == "NDX"
    assert tc.default_strategy() == "bull_call"


def test_ndx_vertical_rule_matches_spec():
    r = tc.ticker_rule("NDX", "bull_call")
    assert r["kind"] == "vertical"
    assert r["anchor"] == 20 and r["width"] == 100   # long 20pts off spot, short 100 beyond
    assert r["tick"] == 0.05
    assert r["type"] == "debit" and r["side"] == "call" and r["dir"] == "bull"


def test_ndx_condor_and_fly_rules():
    assert tc.ticker_rule("NDX", "iron_condor")["short_offset"] == 100
    assert tc.ticker_rule("NDX", "iron_condor")["width"] == 100
    assert tc.ticker_rule("NDX", "iron_fly")["width"] == 100
    assert tc.ticker_rule("NDX", "call_fly")["wing"] == 100


def test_per_ticker_offsets_scaled_to_instrument():
    # ETFs get tight verticals (1pt grid), not the base 50-wide
    spy = tc.ticker_rule("SPY", "bull_call")
    assert spy["anchor"] == 1 and spy["width"] == 5 and spy["tick"] == 0.01
    spx = tc.ticker_rule("SPX", "bull_call")
    assert spx["anchor"] == 5 and spx["width"] == 25
    rut = tc.ticker_rule("RUT", "bull_call")
    assert rut["anchor"] == 5 and rut["width"] == 20


def test_unconfigured_ticker_falls_back_to_base_offsets():
    # a ticker with no override → base defaults (anchor 0, width 50)
    r = tc.ticker_rule("ZZZZ", "bull_call")
    assert r["anchor"] == 0 and r["width"] == 50


def test_strategy_types_are_authoritative():
    types = {s["key"]: s["type"] for s in tc.strategy_list()}
    assert types["bull_call"] == "debit"
    assert types["call_fly"] == "debit" and types["put_fly"] == "debit"
    assert types["iron_condor"] == "credit" and types["iron_fly"] == "credit"
    assert types["bull_put"] == "credit" and types["bear_call"] == "credit"
    assert types["long_call"] == "debit" and types["long_put"] == "debit"


def test_unknown_strategy_raises():
    with pytest.raises(KeyError):
        tc.ticker_rule("NDX", "nope")


def test_file_override_merges(tmp_path, monkeypatch):
    cfg = {
        "tickers": ["ABC", "NDX"],
        "default_strategy": "long_put",
        "overrides": {"NDX": {"tick": 0.10, "vertical": {"anchor": 5}}},
    }
    p = tmp_path / "torque_tickers.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setenv("TORQUE_TICKERS_CONFIG", str(p))
    assert tc.tickers()[0] == "ABC"
    assert tc.default_strategy() == "long_put"
    r = tc.ticker_rule("NDX", "bull_call")
    assert r["tick"] == 0.10        # overridden
    assert r["anchor"] == 5         # overridden
    assert r["width"] == 100        # untouched base/ticker value preserved


# ── DJX (DJXW root) ────────────────────────────────────────────────────────
def test_djx_is_a_tradeable_ticker_with_yahoo_spot():
    assert "DJX" in tc.tickers()
    # ^DJX already quotes at the 1/100 DJIA scale the strike grid uses.
    assert tc.yahoo_symbol("DJX") == "^DJX"


def test_djx_vertical_is_wide_enough_to_clear_its_own_spread():
    r = tc.ticker_rule("DJX", "bull_call")
    assert r["anchor"] == 1
    # DJX market makers quote a ~$0.30 wide package on a ~$0.70 mid at width 5.
    # Narrow widths are structurally unclosable; width>=10 keeps pkg spread ~16%.
    assert r["width"] >= 10
    # DJX quotes in pennies (2.68 / 1.41 are not 0.05 multiples).
    assert r["tick"] == 0.01


def test_djx_trades_only_the_djxw_root():
    assert tc.allowed_roots("DJX") == ["DJXW"]
    # every other ticker is unrestricted (generic dedup handles them)
    assert tc.allowed_roots("SPX") == []
    assert tc.allowed_roots("NDX") == []


def test_djx_close_target_floor_exceeds_round_trip_spread_cost():
    # Measured live: DJX package spread is 16-43% of mid depending on width.
    # A 30% target would price the close order inside the spread.
    assert tc.close_target_min("DJX") >= 45.0
    assert tc.close_target_default("DJX") > tc.close_target_min("DJX")
    assert tc.close_target_default("DJX") > tc.DEFAULT_CLOSE_TARGET_PCT


def test_tight_tickers_keep_the_permissive_floor():
    for t in ("SPX", "NDX", "SPY"):
        assert tc.close_target_min(t) == 1.0
        assert tc.close_target_default(t) == tc.DEFAULT_CLOSE_TARGET_PCT


def test_close_targets_map_covers_every_ticker():
    m = tc.close_targets_map()
    assert set(m) == set(tc.tickers())
    assert m["DJX"]["min"] >= 45.0


def test_only_djx_opts_into_spread_scaled_floor():
    assert tc.close_target_spread_scaled("DJX") is True
    for t in ("SPX", "NDX", "RUT", "SPY", "QQQ"):
        assert tc.close_target_spread_scaled(t) is False


def test_untradeable_package_ceiling_is_configured():
    assert tc.MAX_AUTO_CLOSE_SPREAD_PCT == 100.0
