"""Smart debit picker (strike_profiles.py) — OTM-OTM selection, profile knobs,
and the legacy-fallback guarantee that keeps JSX parity intact.

These do NOT touch the parity suite: the picker only activates when a profile +
spot are passed, which parity callers never do.
"""
from __future__ import annotations

import math

from app.strike_profiles import (
    StrikeProfile, pick_debit_strikes, SPX_PROFILE, DEFAULT_PROFILE,
)
from app.strategy_engine import build_vertical, generate_candidates


def _bs_delta(side: str, spot: float, strike: float) -> float:
    """Crude monotonic delta proxy good enough to order strikes by moneyness."""
    x = (spot - strike) / (spot * 0.01)  # ~1% vol unit
    cdf = 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return cdf if side == "call" else -(1 - cdf)


def _price(side: str, spot: float, strike: float) -> float:
    """Intrinsic + a Gaussian time-value bump so mids vary by strike (a flat
    chain would make every debit net to ~0 and score_vertical would reject it)."""
    intrinsic = max(spot - strike, 0.0) if side == "call" else max(strike - spot, 0.0)
    tv = 8.0 * math.exp(-(((strike - spot) / (spot * 0.02)) ** 2))
    return intrinsic + tv + 0.5


def _spx_chain(spot: float = 5000.0, lo: int = 4800, hi: int = 5200, step: int = 5):
    """Synthetic SPX-like chain: strikes 5 apart, every strike tradeable + liquid."""
    out = []
    k = lo
    while k <= hi:
        for side in ("call", "put"):
            mid = _price(side, spot, float(k))
            out.append({
                "symbol": f"SPX{k}{side[0].upper()}",
                "strike": float(k),
                "side": side,
                "delta": _bs_delta(side, spot, float(k)),
                "gamma": 0.001,
                "open_interest": 500,
                "volume": 200,
                "bid": max(0.05, mid - 0.2),
                "ask": mid + 0.2,
            })
        k += step
    return out


def test_bull_call_is_otm_otm_not_deep_itm():
    spot = 5000.0
    chain = _spx_chain(spot)
    picked = pick_debit_strikes("bull_call", chain, spot, expected_move=40.0,
                                walls=None, profile=SPX_PROFILE)
    assert picked is not None
    long_k = float(picked["long"]["strike"])
    short_k = float(picked["short"]["strike"])
    # Both legs OTM (above spot for a call debit); long below short.
    assert long_k > spot and short_k > long_k
    # Long is NEAR spot (gamma band), NOT deep ITM and not miles away.
    assert 0 < long_k - spot <= 60
    # Long delta lands in the configured band (with snap tolerance).
    assert SPX_PROFILE.long_delta_lo - 0.08 <= abs(picked["long"]["delta"]) <= SPX_PROFILE.long_delta_hi + 0.08
    # Strikes snapped to the 5-pt grid.
    assert long_k % 5 == 0 and short_k % 5 == 0


def test_bear_put_is_otm_otm_below_spot():
    spot = 5000.0
    chain = _spx_chain(spot)
    picked = pick_debit_strikes("bear_put", chain, spot, expected_move=40.0,
                                walls=None, profile=SPX_PROFILE)
    assert picked is not None
    long_k = float(picked["long"]["strike"])
    short_k = float(picked["short"]["strike"])
    # Put debit: both OTM below spot, long above short.
    assert long_k < spot and short_k < long_k
    assert 0 < spot - long_k <= 60


def test_short_sold_into_magnet_wall():
    spot = 5000.0
    chain = _spx_chain(spot)
    # Magnet 35 pts below spot -> short should land at/near 4965.
    walls = {"strike": 4965.0, "strength": "high", "net_gex": 1e9}
    picked = pick_debit_strikes("bear_put", chain, spot, expected_move=40.0,
                                walls=walls, profile=SPX_PROFILE)
    assert picked is not None
    assert abs(float(picked["short"]["strike"]) - 4965.0) <= 5.0
    assert "magnet wall" in picked["logic"]


def test_width_clamped_to_profile_max():
    spot = 5000.0
    chain = _spx_chain(spot)
    # Far magnet (200 pts) must be reined in to max_width_pts=50.
    walls = {"strike": 4800.0, "strength": "high", "net_gex": 1e9}
    picked = pick_debit_strikes("bear_put", chain, spot, expected_move=40.0,
                                walls=walls, profile=SPX_PROFILE)
    assert picked is not None
    width = abs(float(picked["short"]["strike"]) - float(picked["long"]["strike"]))
    assert width <= 50.0 * 1.0 + 1e-6  # aggressiveness 0 -> exact cap


def test_aggressiveness_widens_structure():
    spot = 5000.0
    chain = _spx_chain(spot)
    walls = {"strike": 4700.0, "strength": "high", "net_gex": 1e9}  # far -> width-bound
    cons = pick_debit_strikes("bear_put", chain, spot, 40.0, walls, SPX_PROFILE, aggressiveness=-1)
    aggr = pick_debit_strikes("bear_put", chain, spot, 40.0, walls, SPX_PROFILE, aggressiveness=1)
    w_cons = abs(float(cons["short"]["strike"]) - float(cons["long"]["strike"]))
    w_aggr = abs(float(aggr["short"]["strike"]) - float(aggr["long"]["strike"]))
    assert w_aggr >= w_cons


def test_disabled_profile_falls_back_to_legacy():
    spot = 5000.0
    chain = _spx_chain(spot)
    prof = StrikeProfile(symbol="SPX", enabled=False, round_snap=5.0)
    # Disabled -> build_vertical must use the legacy deep-ITM mirror (long ~0.8Δ).
    v = build_vertical("bull_call", chain, dte=0.5, expected_move=40.0,
                       target_delta=0.20, width_factor=1.0,
                       spot=spot, walls=None, profile=prof)
    assert v is not None
    assert v.get("strike_source") != "smart_picker"


def test_no_profile_is_legacy_identical():
    spot = 5000.0
    chain = _spx_chain(spot)
    legacy = build_vertical("bull_call", chain, 0.5, 40.0, 0.20, 1.0)
    withnone = build_vertical("bull_call", chain, 0.5, 40.0, 0.20, 1.0,
                              spot=spot, walls=None, profile=None)
    assert legacy["strikes"] == withnone["strikes"]


def test_generate_candidates_tags_picker_for_debit():
    spot = 5000.0
    chain = _spx_chain(spot)
    cands = generate_candidates("bull_call", chain, dte=0.5, expected_move=40.0,
                                target_delta=0.20, width_factor=1.0, walls=None,
                                spot=spot, profile=SPX_PROFILE)
    assert cands
    assert all(c.get("strike_source") == "smart_picker" for c in cands)
    # Three distinct structures from the aggressiveness fan.
    widths = {round(c["width"], 4) for c in cands}
    assert len(widths) >= 2
