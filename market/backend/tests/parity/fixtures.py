"""Synthetic chain fixtures for parity testing.

Each fixture is a deterministic chain — no randomness — so JSX and Python
outputs are bitwise reproducible. Fixtures cover the math layer's interesting
branches:

  * put-wall above / below / at spot (drives bullish, bearish, pin reads)
  * call-wall above / below / at spot (resistance, blocking, pin)
  * call wall BETWEEN spot and put wall (resistance discounts magnet)
  * mixed bilateral wall (similar-magnitude put and call)
  * sparse chain (4 strikes)
  * dense chain (40 strikes with high OI)
  * flat chain (no clear wall)
  * multi-day DTE 5 (barrier model, not EdgelaneProvider)
  * intraday DTE 0.01 (near-close — reach amplifier)
  * multi-day long-gamma  (netGex > 0)
  * multi-day short-gamma (netGex < 0)
"""
from __future__ import annotations
from typing import Callable

# Default expiration used by every fixture — keeps fixtures deterministic and
# the parity comparator simple (single-expiration walk on both sides).
DEFAULT_EXPIRATION = "2026-06-04"


def _mk_contract(
    strike: float,
    side: str,
    gamma: float,
    oi: int,
    delta: float,
    theta: float = -0.5,
    vega: float = 10.0,
    iv: float = 0.20,
    bid: float | None = None,
    ask: float | None = None,
    expiration: str = DEFAULT_EXPIRATION,
    spot_for_mid: float = 7580.0,
) -> dict:
    """Build one contract dict in the shape the engine expects.

    Greek and OI numbers are caller-supplied so each fixture can hand-craft
    its wall geometry. Bid/ask default to a crude intrinsic + time-value
    estimate clamped to >0.05 so the strike-finder doesn't reject it.
    """
    if bid is None or ask is None:
        intrinsic = max(0.0, (spot_for_mid - strike) if side == "call" else (strike - spot_for_mid))
        time_val = max(0.10, 5.0 - abs(strike - spot_for_mid) / 30.0)
        mid = intrinsic + time_val
        bid = round(max(0.05, mid - 0.05), 2)
        ask = round(mid + 0.05, 2)
    sign = "C" if side == "call" else "P"
    return {
        "symbol": f"SPXW260604{sign}{int(strike * 1000):08d}",
        "strike": float(strike),
        "side": side,
        "expiration": expiration,
        "gamma": float(gamma),
        # JSX uses call_delta positive, put_delta negative — caller passes raw delta value.
        "delta": float(delta),
        "theta": float(theta),
        "vega": float(vega),
        "iv": float(iv),
        "open_interest": int(oi),
        "volume": 0,
        "bid": float(bid),
        "ask": float(ask),
        "last": float((bid + ask) / 2),
    }


def _build_chain(
    spot: float,
    strike_lo: float,
    strike_hi: float,
    step: float,
    oi_for_strike: Callable[[float, str], int],
    expiration: str = DEFAULT_EXPIRATION,
    gamma: float = 0.04,
    theta: float = -0.5,
    vega: float = 10.0,
    iv: float = 0.20,
) -> list[dict]:
    """Build a deterministic call+put chain around `spot`.

    oi_for_strike(strike, side) returns the per-strike OI to use. The delta
    is a simple linear function of moneyness — enough resolution for the
    bias/wall math, not pretending to be Black-Scholes.
    """
    contracts: list[dict] = []
    n = int(round((strike_hi - strike_lo) / step)) + 1
    for i in range(n):
        strike = strike_lo + i * step
        # Crude linear delta: ATM ~0.50, deep ITM ~0.95, deep OTM ~0.05
        call_delta = max(0.05, min(0.95, 0.5 + (spot - strike) / 100))
        put_delta = call_delta - 1.0
        contracts.append(_mk_contract(
            strike, "call",
            gamma=gamma, oi=oi_for_strike(strike, "call"),
            delta=call_delta, theta=theta, vega=vega, iv=iv,
            expiration=expiration, spot_for_mid=spot,
        ))
        contracts.append(_mk_contract(
            strike, "put",
            gamma=gamma, oi=oi_for_strike(strike, "put"),
            delta=put_delta, theta=theta, vega=vega, iv=iv,
            expiration=expiration, spot_for_mid=spot,
        ))
    return contracts


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

def fx_spx_0dte_put_wall_above():
    """Bullish setup: huge put wall 15 points ABOVE spot.

    EdgelaneProvider: put wall above = magnet pulls price up → bullish score.
    """
    spot = 7580.0
    def oi(strike, side):
        if side == "put" and strike == 7595:
            return 8000
        if side == "put":
            return 300
        return 400  # call
    return {
        "name": "spx_0dte_put_wall_above",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.5,
        "contracts": _build_chain(spot, 7530, 7640, 5, oi),
    }


def fx_spx_0dte_put_wall_below():
    """Bearish setup: huge put wall 15 points BELOW spot."""
    spot = 7580.0
    def oi(strike, side):
        if side == "put" and strike == 7565:
            return 8000
        if side == "put":
            return 300
        return 400
    return {
        "name": "spx_0dte_put_wall_below",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.5,
        "contracts": _build_chain(spot, 7530, 7640, 5, oi),
    }


def fx_spx_0dte_put_wall_at_spot():
    """Pin setup: put wall AT spot (within nearest strike)."""
    spot = 7580.0
    def oi(strike, side):
        if side == "put" and strike == 7580:
            return 10000
        if side == "put":
            return 250
        return 400
    return {
        "name": "spx_0dte_put_wall_at_spot",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.5,
        "contracts": _build_chain(spot, 7530, 7640, 5, oi),
    }


def fx_spx_0dte_call_wall_above():
    """Resistance setup: heavy call OI above spot."""
    spot = 7580.0
    def oi(strike, side):
        if side == "call" and strike == 7605:
            return 8000
        if side == "call":
            return 300
        return 400  # put
    return {
        "name": "spx_0dte_call_wall_above",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.5,
        "contracts": _build_chain(spot, 7530, 7640, 5, oi),
    }


def fx_spx_0dte_call_wall_below():
    """Heavy call OI BELOW spot (rare but possible — ITM call wall)."""
    spot = 7580.0
    def oi(strike, side):
        if side == "call" and strike == 7565:
            return 9000
        if side == "call":
            return 300
        return 400
    return {
        "name": "spx_0dte_call_wall_below",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.5,
        "contracts": _build_chain(spot, 7530, 7640, 5, oi),
    }


def fx_spx_0dte_call_blocks_put_magnet():
    """Call wall sits BETWEEN spot and put wall on same side.

    EdgelaneProvider: call same-side-closer → discounts magnet by block-ratio.
    Put wall 7610, call wall 7595, spot 7580. Both above; call closer.
    Expected: magnet attenuated relative to a pure put-wall-above fixture.
    """
    spot = 7580.0
    def oi(strike, side):
        if side == "put" and strike == 7610:
            return 8000
        if side == "call" and strike == 7595:
            return 6000
        if side == "put":
            return 300
        return 300
    return {
        "name": "spx_0dte_call_blocks_put_magnet",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.5,
        "contracts": _build_chain(spot, 7530, 7640, 5, oi),
    }


def fx_spx_0dte_mixed_walls():
    """Similar-magnitude put + call walls split across spot.

    Put wall below, call wall above — both meaningful. Forces bilateral
    finder to pick distinct walls; legacy single-wall finder picks whichever
    has the larger |net_gex|.
    """
    spot = 7580.0
    def oi(strike, side):
        if side == "put" and strike == 7560:
            return 6000
        if side == "call" and strike == 7600:
            return 5500
        if side == "put":
            return 300
        return 300
    return {
        "name": "spx_0dte_mixed_walls",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.5,
        "contracts": _build_chain(spot, 7530, 7640, 5, oi),
    }


def fx_spx_0dte_sparse_chain():
    """Only 4 strikes — exercises edge case in strength classification.

    Sparse chains often yield 'high' strength because sortedAbs has fewer
    runners-up for the ratio test.
    """
    spot = 7580.0
    def oi(strike, side):
        if side == "put" and strike == 7575:
            return 5000
        return 200
    return {
        "name": "spx_0dte_sparse_chain",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.5,
        "contracts": _build_chain(spot, 7570, 7585, 5, oi),
    }


def fx_spx_0dte_dense_high_oi():
    """40+ strikes, several with 10k+ OI — stresses the dominant-wall picker."""
    spot = 7580.0
    def oi(strike, side):
        if side == "put" and strike == 7590:
            return 12000
        if side == "put" and strike == 7570:
            return 9000
        if side == "call" and strike == 7610:
            return 11000
        return 800
    return {
        "name": "spx_0dte_dense_high_oi",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.5,
        "contracts": _build_chain(spot, 7480, 7680, 5, oi),
    }


def fx_spx_0dte_flat_chain():
    """No clear wall — all OI roughly equal. Tests the no-signal path."""
    spot = 7580.0
    def oi(strike, side):
        return 400
    return {
        "name": "spx_0dte_flat_chain",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.5,
        "contracts": _build_chain(spot, 7530, 7640, 5, oi),
    }


def fx_spx_5dte_long_gamma():
    """Multi-day DTE, netGex > 0 (long-gamma).

    Heavy put OI dominates ⇒ positive net_gex (puts dominate dealer-sold).
    Wall above spot in long-gamma is the 'stabilizing' geometry — bias
    score uses barrier model with ×0.3 dampener.
    """
    spot = 7580.0
    def oi(strike, side):
        # Heavy puts above spot → positive net_gex contribution
        if side == "put" and strike >= 7580:
            return 5000
        if side == "put":
            return 500
        return 200  # light call OI keeps gamma puts-dominated
    return {
        "name": "spx_5dte_long_gamma",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 5.0,
        "contracts": _build_chain(spot, 7480, 7680, 10, oi),
    }


def fx_spx_5dte_short_gamma():
    """Multi-day DTE, netGex < 0 (short-gamma).

    Heavy call OI dominates ⇒ negative net_gex (calls dominate dealer-sold).
    Short-gamma multi-day = wall as barrier with ×1.2 amplifier.
    """
    spot = 7580.0
    def oi(strike, side):
        if side == "call" and strike >= 7580:
            return 5000
        if side == "call":
            return 500
        return 200
    return {
        "name": "spx_5dte_short_gamma",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 5.0,
        "contracts": _build_chain(spot, 7480, 7680, 10, oi),
    }


def fx_spx_intraday_near_close():
    """DTE = 0.01 — pure intraday pinning regime, max reach amplifier."""
    spot = 7580.0
    def oi(strike, side):
        if side == "put" and strike == 7585:
            return 7000
        return 300
    return {
        "name": "spx_intraday_near_close",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.01,
        "contracts": _build_chain(spot, 7560, 7600, 5, oi),
    }


def fx_spx_0dte_wall_far_unreachable():
    """0DTE with wall >1.5% away — triggers unreachable-intraday confidence gate."""
    spot = 7580.0
    def oi(strike, side):
        if side == "put" and strike == 7720:
            return 8000
        return 300
    return {
        "name": "spx_0dte_wall_far_unreachable",
        "spot": spot, "expiration": DEFAULT_EXPIRATION, "chosen_dte": 0.5,
        "contracts": _build_chain(spot, 7530, 7740, 10, oi),
    }


# Master list — pytest parametrizes over this directly.
ALL_FIXTURES: list[Callable[[], dict]] = [
    fx_spx_0dte_put_wall_above,
    fx_spx_0dte_put_wall_below,
    fx_spx_0dte_put_wall_at_spot,
    fx_spx_0dte_call_wall_above,
    fx_spx_0dte_call_wall_below,
    fx_spx_0dte_call_blocks_put_magnet,
    fx_spx_0dte_mixed_walls,
    fx_spx_0dte_sparse_chain,
    fx_spx_0dte_dense_high_oi,
    fx_spx_0dte_flat_chain,
    fx_spx_5dte_long_gamma,
    fx_spx_5dte_short_gamma,
    fx_spx_intraday_near_close,
    fx_spx_0dte_wall_far_unreachable,
]
