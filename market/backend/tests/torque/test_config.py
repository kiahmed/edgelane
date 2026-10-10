"""Torque config: ticker rules and strategy registry, with every per-ticker
setting read from the database snapshot (public.torque_tickers_setup)."""
from __future__ import annotations

import asyncio

import pytest

from app import torque_config as tc

from ..conftest import torque_seed_rows


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


def _with_row(ticker, **fields):
    rows = [dict(r) for r in torque_seed_rows()]
    for r in rows:
        if r["ticker"] == ticker:
            r.update(fields)
    tc.apply_ticker_setup(rows)


def test_table_row_is_the_source_of_a_tickers_settings():
    """An edit in torque_tickers_setup — not code, not a file — is what moves a
    ticker's settings; untouched keys keep their seeded/base values."""
    _with_row("NDX", tick=0.10, offsets={"vertical": {"anchor": 5, "width": 100}})
    r = tc.ticker_rule("NDX", "bull_call")
    assert r["tick"] == 0.10 and r["anchor"] == 5 and r["width"] == 100
    assert tc.ticker_rule("NDX", "iron_condor")["short_offset"] == 50   # base, NDX row no longer sets it


def test_richness_floor_defaults_are_sized_per_ticker():
    # Index tickers (bigger dollar increments) get a looser floor than the
    # lower-nominal-premium ETFs — one flat number would misfire on both ends.
    assert tc.richness_floor("NDX") == 0.05
    assert tc.richness_floor("SPX") == 0.05
    assert tc.richness_floor("RUT") == 0.05
    assert tc.richness_floor("SPY") == 0.02
    assert tc.richness_floor("QQQ") == 0.02
    assert tc.richness_floor("DJX") == 0.02


def test_richness_floor_unknown_ticker_falls_back_to_default():
    assert tc.richness_floor("ZZZZ") == tc.DEFAULT_RICHNESS_FLOOR


def test_richness_floors_map_covers_every_configured_ticker():
    m = tc.richness_floors_map()
    assert set(m) == set(tc.tickers())
    assert m["SPY"] == 0.02


def test_stop_loss_defaults_map_djx_keeps_its_own_wider_default():
    # The UI now always sends an explicit stop_loss_pct (so 0 can mean "off"),
    # which means the server's own per-ticker stop_loss_default() is never
    # consulted from an omitted field anymore — this map is what lets the UI
    # prefill DJX's wider 50% instead of silently flattening every ticker to
    # the global 30%.
    m = tc.stop_loss_defaults_map()
    assert set(m) == set(tc.tickers())
    assert m["DJX"] == 50.0
    assert m["NDX"] == tc.DEFAULT_STOP_LOSS_PCT == 30.0
    assert m["SPY"] == tc.DEFAULT_STOP_LOSS_PCT


def test_richness_floor_from_the_table():
    _with_row("NDX", richness_floor=0.09)
    assert tc.richness_floor("NDX") == 0.09      # changed in the table
    assert tc.richness_floor("SPY") == 0.02      # untouched ticker keeps its value


def test_no_json_file_or_hardcoded_ticker_data_is_read_anymore():
    """Per-ticker data comes only from the table snapshot: with it empty,
    nothing is configured (fail closed), even for the old built-in names."""
    tc.apply_ticker_setup([])
    assert tc.tickers() == [] and tc.known_tickers() == []
    assert tc.yahoo_symbol("NDX") is None and tc.allowed_roots("DJX") == []
    assert tc.ticker_rule("NDX", "bull_call")["width"] == 50          # base shape only
    assert not hasattr(tc, "_load_overrides_file") and not hasattr(tc, "TICKERS")


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


# ── torque_tickers_setup: loading, refresh, fail-closed ─────────────────────
def test_seed_tickers_in_list_order_with_the_four_reflex_symbols():
    assert tc.tickers() == ["NDX", "SPX", "RUT", "SPY", "QQQ", "DJX", "IWM", "XLF", "XLE", "SMH"]
    for t in ("IWM", "XLF", "XLE", "SMH"):
        assert tc.yahoo_symbol(t) == t
        assert tc.ticker_rule(t, "long_call")["offset"] == 0          # ATM single leg for signals


def test_disabled_ticker_leaves_the_list_but_keeps_its_settings():
    _with_row("SMH", enabled=False)
    assert "SMH" not in tc.tickers()
    assert "SMH" in tc.known_tickers()                                # open positions still resolve
    assert tc.ticker_rule("SMH", "long_call")["tick"].at(5.0) == 0.05


def test_sort_order_drives_the_list_and_the_default_ticker():
    _with_row("SPY", sort_order=1)
    assert tc.tickers()[0] == "SPY"


def test_not_loaded_means_no_tickers():
    tc._SETUP.clear()
    tc._SETUP_STATE.update(loaded=False, loaded_at=0.0)
    assert tc.tickers() == [] and tc.ticker_setup_loaded() is False


def test_daily_range_columns_are_empty_until_filled():
    assert tc.daily_range("SPY") == (None, None)
    _with_row("SPY", daily_min_range=2.5, daily_max_range=9.0)
    assert tc.daily_range("SPY") == (2.5, 9.0)


async def test_refresh_applies_the_table_and_a_failed_read_keeps_the_last_good_copy(monkeypatch):
    rows = [{"ticker": "spy", "sort_order": 1, "tick": 0.01, "yahoo_symbol": "SPY"}]

    async def _ok():
        return rows
    from app import supabase_admin
    monkeypatch.setattr(supabase_admin, "get_torque_ticker_setup", _ok)
    assert await tc.refresh_ticker_setup() is True
    assert tc.tickers() == ["SPY"] and tc.ticker_setup_loaded()

    async def _down():
        return None
    monkeypatch.setattr(supabase_admin, "get_torque_ticker_setup", _down)
    assert await tc.refresh_ticker_setup() is False
    assert tc.tickers() == ["SPY"]                                    # last good copy kept

    async def _boom():
        raise RuntimeError("network")
    monkeypatch.setattr(supabase_admin, "get_torque_ticker_setup", _boom)
    assert await tc.refresh_ticker_setup() is False
    assert tc.tickers() == ["SPY"]


async def test_refresh_loop_picks_up_table_edits(monkeypatch):
    from app import supabase_admin
    versions = iter([[{"ticker": "SPY", "tick": 0.01}],
                     [{"ticker": "SPY", "tick": 0.01}, {"ticker": "IWM", "tick": 0.01}]])
    calls = []

    async def _rows():
        calls.append(1)
        return next(versions)
    monkeypatch.setattr(supabase_admin, "get_torque_ticker_setup", _rows)
    real_sleep = asyncio.sleep

    async def _fast_sleep(_):
        if len(calls) >= 2:
            raise asyncio.CancelledError
        await real_sleep(0)
    monkeypatch.setattr(tc.asyncio, "sleep", _fast_sleep)
    with pytest.raises(asyncio.CancelledError):
        await tc.ticker_setup_loop(60)
    assert tc.tickers() == ["IWM", "SPY"]                              # equal sort_order → by ticker


async def test_supabase_read_orders_by_sort_order(monkeypatch):
    from app import supabase_admin
    seen = {}

    async def _sel(table, select="*", filters=None, order=None, **kw):
        seen.update(table=table, order=order)
        return []
    monkeypatch.setattr(supabase_admin, "select_many", _sel)
    assert await supabase_admin.get_torque_ticker_setup() == []
    assert seen == {"table": "torque_tickers_setup", "order": "sort_order.asc,ticker.asc"}


# ── price steps (tick) per ticker ────────────────────────────────────────────
def test_penny_everywhere_tickers_step_a_cent_at_any_price():
    for t in ("SPY", "QQQ", "IWM", "DJX"):
        tick = tc.ticker_rule(t, "long_call")["tick"]
        assert tick == 0.01 and tick.at(2.0) == 0.01 and tick.at(12.0) == 0.01


def test_penny_pilot_tickers_step_up_at_three_dollars():
    for t in ("XLF", "XLE", "SMH"):
        tick = tc.ticker_rule(t, "long_call")["tick"]
        assert tick.at(2.99) == 0.01 and tick.at(3.0) == 0.05 and tick.at(7.5) == 0.05


def test_index_tickers_keep_their_nickel_step():
    for t in ("NDX", "SPX", "RUT"):
        assert tc.ticker_rule(t, "bull_call")["tick"] == 0.05


def test_unconfigured_ticker_falls_back_to_the_base_step():
    assert tc.tick_size("ZZZZ") == 0.05


# ── engine rounding honours the price-aware step ────────────────────────────
from app import torque_engine as teng   # noqa: E402


def _valid(price, tick):
    step = tick.at(price) if isinstance(tick, tc.TickSize) else tick
    return abs(round(price / step) * step - price) < 1e-9


@pytest.mark.parametrize("price", [0.07, 1.234, 2.37, 2.996, 3.0, 3.02, 3.37, 5.12, 11.98])
def test_every_rounding_helper_lands_on_a_legal_increment(price):
    tick = tc.tick_size("XLF")
    for f in (teng.round_to_tick, teng._ceil_tick, teng._floor_tick):
        out = f(price, tick)
        assert _valid(out, tick), (f.__name__, price, out)


def test_bracket_prices_crossing_three_dollars_are_all_legal():
    """XLF bought at 2.80: the +30% target lands above $3 (nickel step) while
    the stop stays below it (penny step) — each must be legal on its own side."""
    tick = tc.tick_size("XLF")
    target = teng.close_target_price(2.80, "debit", 30.0, tick)
    trigger, limit = teng.stop_limit_leg_prices(2.80, "debit", 30.0, tick, 3)
    assert target >= 3.0 and _valid(target, tick)
    assert trigger < 3.0 and _valid(trigger, tick) and _valid(limit, tick) and limit < trigger


def test_plain_float_ticks_behave_exactly_as_before():
    assert teng.round_to_tick(3.37, 0.05) == 3.35 and teng.round_to_tick(3.37, 0.01) == 3.37
    assert teng._ceil_tick(3.01, 0.05) == 3.05 and teng._floor_tick(3.09, 0.05) == 3.05
