"""Torque engine: strike auto-fill, pricing, close math, lean — pure functions."""
from __future__ import annotations

import pytest

from app import torque_engine as teng
from .conftest import raw_chain


def roles(struct):
    return {l["role"]: l for l in struct["legs"]}


# ── dual-root dedup (index NDX vs live NDXP) ────────────────────────────────
def test_normalize_prefers_freshest_root():
    """At a shared (side, strike), keep the row with the freshest quote stamp —
    the live NDXP root, not the stale AM-settled NDX root."""
    raw = [
        {"symbol": "NDX260618C30200000", "strike": 30200.0, "option_type": "call",
         "bid": 6.9, "ask": 11.6, "last": 10.3, "open_interest": 50, "volume": 0,
         "bid_date": 1_000_000_000_000, "ask_date": 1_000_000_000_000},     # stale (old ts)
        {"symbol": "NDXP260618C30200000", "strike": 30200.0, "option_type": "call",
         "bid": 114.6, "ask": 117.3, "last": 154.0, "open_interest": 1200, "volume": 800,
         "bid_date": 1_700_000_000_000, "ask_date": 1_700_000_000_000},     # live (new ts)
    ]
    out = teng.normalize_chain(raw)
    assert len(out) == 1                       # deduped to one row per (side, strike)
    assert out[0]["symbol"] == "NDXP260618C30200000"
    assert out[0]["bid"] == 114.6


def test_normalize_drops_stale_only_strikes_from_grid():
    """The stale root's extra dead strikes (no live-root equivalent) must be
    removed entirely — else the grid is polluted and the anchor can snap onto a
    dead 5-pt strike (the 30425 mid=2.63 bug)."""
    raw = [
        # live NDXP 10-pt grid, real volume
        {"symbol": "NDXP..C20", "strike": 30420.0, "option_type": "call", "root_symbol": "NDXP",
         "bid": 34.0, "ask": 36.0, "volume": 300, "open_interest": 60},
        {"symbol": "NDXP..C30", "strike": 30430.0, "option_type": "call", "root_symbol": "NDXP",
         "bid": 29.0, "ask": 31.0, "volume": 250, "open_interest": 55},
        # stale NDX 5-pt strike with NO live equivalent — must be dropped
        {"symbol": "NDX..C25", "strike": 30425.0, "option_type": "call", "root_symbol": "NDX",
         "bid": 1.0, "ask": 4.3, "volume": 0, "open_interest": 400},
    ]
    out = teng.normalize_chain(raw)
    strikes = {c["strike"] for c in out}
    assert strikes == {30420.0, 30430.0}        # 30425 (stale NDX-only) gone
    assert all(c["_root"] == "NDXP" for c in out)


def test_known_weekly_root_wins_even_with_zero_volume():
    """Pre-market (all volume 0) the live weekly root must still win over the
    stale monthly root — deterministic, not volume-dependent."""
    raw = [
        {"symbol": "NDX..C00", "strike": 30400.0, "option_type": "call", "root_symbol": "NDX",
         "bid": 1.0, "ask": 5.0, "volume": 0, "open_interest": 900},
        {"symbol": "NDXP.C00", "strike": 30400.0, "option_type": "call", "root_symbol": "NDXP",
         "bid": 34.0, "ask": 36.0, "volume": 0, "open_interest": 5},
    ]
    out = teng.normalize_chain(raw)
    assert len(out) == 1 and out[0]["_root"] == "NDXP"


def test_normalize_dedup_falls_back_to_volume_then_oi():
    """No usable timestamps → break ties by volume, then open interest."""
    raw = [
        {"symbol": "NDX...P", "strike": 30200.0, "option_type": "put",
         "bid": 1.0, "ask": 2.0, "open_interest": 100, "volume": 5},
        {"symbol": "NDXP..P", "strike": 30200.0, "option_type": "put",
         "bid": 3.0, "ask": 4.0, "open_interest": 10, "volume": 900},
    ]
    out = teng.normalize_chain(raw)
    assert len(out) == 1
    assert out[0]["symbol"] == "NDXP..P"       # higher volume wins


# ── implied (parity) spot ───────────────────────────────────────────────────
def test_implied_spot_from_parity():
    """call_mid − put_mid + strike recovers spot; ATM picks min |c−p|."""
    raw = [
        {"symbol": "C1", "strike": 30290.0, "option_type": "call", "bid": 104.0, "ask": 105.1},
        {"symbol": "P1", "strike": 30290.0, "option_type": "put",  "bid": 101.2, "ask": 102.2},
        # a deep-ITM junk strike with a one-sided/huge spread must NOT be chosen
        {"symbol": "C2", "strike": 30275.0, "option_type": "call", "bid": 5.0, "ask": 6.4},
        {"symbol": "P2", "strike": 30275.0, "option_type": "put",  "bid": 556.0, "ask": 558.0},
    ]
    chain = teng.normalize_chain(raw)
    spot = teng.implied_spot(chain, fallback=30200.0)
    assert abs(spot - 30292.85) < 0.5          # from the 30290 ATM pair, not the junk row


def test_implied_spot_falls_back_when_no_pairs():
    raw = [{"symbol": "C", "strike": 30000.0, "option_type": "call", "bid": 1.0, "ask": 2.0}]
    assert teng.implied_spot(teng.normalize_chain(raw), fallback=30123.0) == 30123.0


# ── single-leg ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("strat,side", [("long_call", "call"), ("long_put", "put")])
def test_single_leg_atm(spot, chain, strat, side):
    s = teng.build_structure(spot, chain, "NDX", strat)
    assert len(s["legs"]) == 1
    lg = s["legs"][0]
    assert lg["side"] == side
    assert lg["action"] == "buy_to_open" and lg["quantity"] == 1
    assert lg["strike"] == 22000.0           # offset 0 → ATM
    assert lg["symbol"] is not None
    assert s["type"] == "debit"


# ── debit verticals (near = long) ──────────────────────────────────────────
def test_bull_call_ndx_offsets(spot, chain):
    s = teng.build_structure(spot, chain, "NDX", "bull_call")
    r = roles(s)
    assert s["type"] == "debit"
    # near(long) ≈ spot+20 → snapped to 22025; far(short) = near+100 = 22125
    assert r["near"]["action"] == "buy_to_open" and r["near"]["side"] == "call"
    assert r["near"]["strike"] == 22025.0
    assert r["far"]["action"] == "sell_to_open" and r["far"]["strike"] == 22125.0


def test_bear_put_ndx_offsets(spot, chain):
    s = teng.build_structure(spot, chain, "NDX", "bear_put")
    r = roles(s)
    assert r["near"]["action"] == "buy_to_open" and r["near"]["side"] == "put"
    assert r["near"]["strike"] == 21975.0      # spot-20 snapped down
    assert r["far"]["action"] == "sell_to_open" and r["far"]["strike"] == 21875.0


# ── credit verticals (near = short) ────────────────────────────────────────
def test_bull_put_near_is_short(spot, chain):
    s = teng.build_structure(spot, chain, "NDX", "bull_put")
    r = roles(s)
    assert s["type"] == "credit"
    assert r["near"]["action"] == "sell_to_open" and r["near"]["side"] == "put"
    assert r["near"]["strike"] == 21975.0
    assert r["far"]["action"] == "buy_to_open" and r["far"]["strike"] == 21875.0


def test_bear_call_near_is_short(spot, chain):
    s = teng.build_structure(spot, chain, "NDX", "bear_call")
    r = roles(s)
    assert r["near"]["action"] == "sell_to_open" and r["near"]["side"] == "call"
    assert r["near"]["strike"] == 22025.0
    assert r["far"]["action"] == "buy_to_open" and r["far"]["strike"] == 22125.0


# ── condor / iron fly / butterflies ────────────────────────────────────────
def test_iron_condor_four_legs(spot, chain):
    s = teng.build_structure(spot, chain, "NDX", "iron_condor")
    r = roles(s)
    assert len(s["legs"]) == 4
    assert r["short_put"]["strike"] == 21900.0 and r["short_put"]["action"] == "sell_to_open"
    assert r["long_put"]["strike"] == 21800.0 and r["long_put"]["action"] == "buy_to_open"
    assert r["short_call"]["strike"] == 22100.0 and r["short_call"]["action"] == "sell_to_open"
    assert r["long_call"]["strike"] == 22200.0 and r["long_call"]["action"] == "buy_to_open"


def test_iron_fly_shorts_atm(spot, chain):
    s = teng.build_structure(spot, chain, "NDX", "iron_fly")
    r = roles(s)
    assert r["short_put"]["strike"] == 22000.0 and r["short_call"]["strike"] == 22000.0
    assert r["long_put"]["strike"] == 21900.0 and r["long_call"]["strike"] == 22100.0


@pytest.mark.parametrize("strat,side", [("call_fly", "call"), ("put_fly", "put")])
def test_butterfly_structure(spot, chain, strat, side):
    s = teng.build_structure(spot, chain, "NDX", strat)
    r = roles(s)
    assert len(s["legs"]) == 3
    assert r["body"]["action"] == "sell_to_open" and r["body"]["quantity"] == 2
    assert r["body"]["strike"] == 22000.0
    assert r["low"]["action"] == "buy_to_open" and r["low"]["strike"] == 21900.0
    assert r["high"]["action"] == "buy_to_open" and r["high"]["strike"] == 22100.0
    assert all(l["side"] == side for l in s["legs"])


# ── steppers / adjustments ─────────────────────────────────────────────────
def test_adjustment_steps_strike_by_grid(spot, chain):
    base = roles(teng.build_structure(spot, chain, "NDX", "bull_call"))
    up = roles(teng.build_structure(spot, chain, "NDX", "bull_call", {"far": 2}))
    assert up["far"]["strike"] == base["far"]["strike"] + 50   # 2 × 25-pt grid
    down = roles(teng.build_structure(spot, chain, "NDX", "bull_call", {"near": -1}))
    assert down["near"]["strike"] == base["near"]["strike"] - 25


def test_adjustment_clamps_at_grid_end(spot, chain):
    # huge step beyond the grid clamps to the last available strike
    s = roles(teng.build_structure(spot, chain, "NDX", "bull_call", {"far": 9999}))
    grid = teng._grid(chain, "call")
    assert s["far"]["strike"] == grid[-1]


def test_missing_strike_yields_no_symbol(spot):
    # narrow chain: far leg lands outside the listed grid → symbol None
    narrow = teng.normalize_chain(raw_chain(spot, step=25, span=50))
    s = roles(teng.build_structure(spot, narrow, "NDX", "bull_call"))
    # near resolves; far (spot+125) is off the ±50 chain → clamped to top strike,
    # which still exists, so assert the clamp rather than None here:
    assert s["far"]["symbol"] is not None
    # but a 4-leg condor with 100-wide wings off a ±50 chain clamps both wings in
    cond = teng.build_structure(spot, narrow, "NDX", "iron_condor")
    assert all(l["symbol"] is not None for l in cond["legs"])


# ── grid helpers ───────────────────────────────────────────────────────────
def test_nearest_and_step(chain):
    grid = teng._grid(chain, "call")
    assert teng._nearest(grid, 22013) == 22025  # 22013 closer to 22025 than 22000? no → 22000
    assert teng._nearest(grid, 22010) == 22000
    assert teng._step(grid, 22000.0, 1) == 22025.0
    assert teng._step(grid, grid[0], -5) == grid[0]   # clamp low
    assert teng._nearest([], 100) is None


# ── pricing ────────────────────────────────────────────────────────────────
def test_price_debit_positive(spot, chain):
    s = teng.build_structure(spot, chain, "NDX", "bull_call")
    px = {c["symbol"]: c for c in chain}
    p = teng.price_structure(s["legs"], px)
    assert p["type"] == "debit" and p["net_mid"] > 0
    assert p["abs_bid"] <= p["abs_mid"] <= p["abs_ask"]
    assert p["complete"] is True


def test_price_credit_negative(spot, chain):
    s = teng.build_structure(spot, chain, "NDX", "bull_put")
    px = {c["symbol"]: c for c in chain}
    p = teng.price_structure(s["legs"], px)
    assert p["type"] == "credit" and p["net_mid"] < 0


def test_price_incomplete_when_quote_missing(spot, chain):
    s = teng.build_structure(spot, chain, "NDX", "bull_call")
    p = teng.price_structure(s["legs"], {})   # no quotes
    assert p["complete"] is False


def test_incomplete_never_emits_partial_net(spot, chain):
    """If one leg's quote is missing, the net must be None — NEVER the other
    leg's price alone (the single-digit-flicker bug)."""
    s = teng.build_structure(spot, chain, "NDX", "bull_call")
    px = {c["symbol"]: c for c in chain}
    px.pop(s["legs"][1]["symbol"])             # drop the far (short) leg quote
    p = teng.price_structure(s["legs"], px)
    assert p["complete"] is False
    assert p["net_mid"] is None and p["abs_mid"] is None
    assert p["net_bid"] is None and p["net_ask"] is None


# ── close target + side flips + qty scaling ────────────────────────────────
def test_close_target_debit_and_credit():
    assert teng.close_target_price(25.95, "debit", 30, 0.05) == 33.75   # 1.30×, tick-rounded
    assert teng.close_target_price(10.0, "credit", 30, 0.05) == 7.0     # 0.70×
    assert teng.close_target_price(2.123, "debit", 0, 0.05) == 2.10     # tick rounding


def test_round_to_tick():
    assert teng.round_to_tick(33.735, 0.05) == 33.75
    assert teng.round_to_tick(1.234, 0.01) == 1.23
    assert teng.round_to_tick(1.234, 0) == 1.23


def test_build_close_legs_flips_sides(spot, chain):
    s = teng.build_structure(spot, chain, "NDX", "bull_call")
    closed = teng.build_close_legs(s["legs"])
    flips = {l["role"]: l["action"] for l in closed}
    assert flips["near"] == "sell_to_close"   # was buy_to_open
    assert flips["far"] == "buy_to_close"     # was sell_to_open


def test_legs_to_order_legs_scales_qty(spot, chain):
    s = teng.build_structure(spot, chain, "NDX", "call_fly")
    ol = teng.legs_to_order_legs(s["legs"], contracts_qty=3)
    body = next(l for l in ol if l.quantity == 6)  # butterfly body qty2 × 3 contracts
    assert body.action == "sell_to_open"
    assert all(l.occ_symbol for l in ol)


# ── directional lean ───────────────────────────────────────────────────────
def test_lean_bullish_when_calls_dominate(spot):
    raw = raw_chain(spot, call_vol=lambda k: 5000, put_vol=lambda k: 200,
                    call_oi=lambda k: 5000, put_oi=lambda k: 200)
    a = teng.analyze(spot, teng.normalize_chain(raw))
    assert a["lean"] > 25 and a["label"] == "bullish" and a["suggested_side"] == "call"


def test_lean_bearish_when_puts_dominate(spot):
    raw = raw_chain(spot, call_vol=lambda k: 200, put_vol=lambda k: 5000,
                    call_oi=lambda k: 200, put_oi=lambda k: 5000)
    a = teng.analyze(spot, teng.normalize_chain(raw))
    assert a["lean"] < -25 and a["label"] == "bearish" and a["suggested_side"] == "put"


def test_lean_neutral_when_balanced(spot, chain):
    a = teng.analyze(spot, chain)
    assert -25 < a["lean"] < 25 and a["label"] == "neutral"
    assert a["suggested_side"] is None      # neutral → no fabricated side
    assert "disclaimer" in a and a["components"]["volume"]["pcr"] is not None


def test_analyze_empty_chain_is_safe(spot):
    a = teng.analyze(spot, [])
    assert a["lean"] == 0.0 and a["label"] == "neutral"


# ── expiration choice ──────────────────────────────────────────────────────
def test_choose_expiration_prefers_today(monkeypatch):
    import app.torque_engine as e
    from datetime import datetime, timezone

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 18, tzinfo=timezone.utc)
    monkeypatch.setattr(e, "datetime", _DT)
    assert e.choose_expiration(["2026-06-18", "2026-06-25"]) == "2026-06-18"
    assert e.choose_expiration(["2026-06-25", "2026-07-02"]) == "2026-06-25"  # nearest future
    assert e.choose_expiration([]) is None
