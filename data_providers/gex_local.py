"""
Local dealer-GEX aggregator.

Takes a normalized options chain (per-contract: strike, side, gamma, delta,
theta, open_interest, expiration) and a spot price, returns a greek-exposures
structure in the shape the engine expects:

    {
      "exposures_by_date": {
        "<YYYY-MM-DD>": {
          "by_strike": [
            {"strike": K, "call_gex": ..., "put_gex": ..., "net_gex": ...,
             "call_dex": ..., "put_dex": ..., "net_dex": ...,
             "call_oi": ..., "put_oi": ...},
            ...
          ],
          "totals": {"net_gex": ..., "net_dex": ...},
        },
        ...
      },
      "portfolio_totals": {"net_gex": ..., "net_dex": ...},
      "key_levels": {
        "call_wall": {"strike": K, "gex": ...},
        "put_wall":  {"strike": K, "gex": ...},
      },
    }

Convention (matches SpotGamma / SqueezeMetrics / public GEX literature):
    Public is net long calls   → dealer is short calls → dealer call gamma is NEGATIVE
    Public is net short puts   → dealer is long puts   → dealer put gamma is POSITIVE

    dealer_gamma_at_strike(K) = put_gamma(K) × put_OI(K) − call_gamma(K) × call_OI(K)
    dealer_GEX_dollars(K)     = dealer_gamma_at_strike(K) × 100 × spot²
    dealer_delta_at_strike(K) = call_delta(K) × call_OI(K) − put_delta(K) × put_OI(K)
    dealer_DEX_dollars(K)     = dealer_delta_at_strike(K) × 100 × spot

    call_wall = strike above spot with most NEGATIVE dealer_GEX (largest forced-buy zone if pierced)
    put_wall  = strike below spot with most POSITIVE dealer_GEX (largest forced-sell zone if pierced)

These conventions mirror the dealer-GEX math the engine expects, matching
the public GEX literature referenced above.
"""
from collections import defaultdict
from typing import Iterable


# Standard equity-option contract multiplier. Mini-options use 10; future
# work should consult contract_size per-contract instead of assuming 100.
CONTRACT_MULTIPLIER = 100


def compute_dealer_exposures(contracts: Iterable[dict], spot: float) -> dict:
    """Aggregate dealer GEX/DEX from a flat list of normalized contracts.

    Each contract should have at minimum:
        strike (float), side ('call'|'put'), expiration (str YYYY-MM-DD),
        gamma (float), delta (float), open_interest (int)

    Missing greeks or zero OI strikes are tolerated — they contribute 0 to
    the exposure but still appear in by_strike if they have any OI on the
    other side. Strikes with both sides missing are dropped.
    """
    if not contracts or not spot or spot <= 0:
        return _empty_result()

    # Bucket contracts by (expiration, strike, side)
    buckets: dict = defaultdict(lambda: defaultdict(dict))
    for c in contracts:
        exp = c.get("expiration")
        strike = c.get("strike")
        side = c.get("side", "").lower()
        if exp is None or strike is None or side not in ("call", "put"):
            continue
        buckets[exp].setdefault(strike, {})[side] = c

    if not buckets:
        return _empty_result()

    spot_sq = spot * spot
    exposures_by_date: dict = {}
    portfolio_net_gex = 0.0
    portfolio_net_dex = 0.0

    # All-strike dealer-GEX rolled up across expirations, for wall selection
    all_strikes_gex: dict = defaultdict(float)

    for exp, strike_map in buckets.items():
        by_strike = []
        exp_net_gex = 0.0
        exp_net_dex = 0.0

        for strike in sorted(strike_map.keys()):
            sides = strike_map[strike]
            call = sides.get("call", {})
            put = sides.get("put", {})

            call_gamma = _num(call.get("gamma"))
            put_gamma = _num(put.get("gamma"))
            call_delta = _num(call.get("delta"))
            put_delta = _num(put.get("delta"))
            call_oi = _num(call.get("open_interest")) or 0
            put_oi = _num(put.get("open_interest")) or 0

            call_gex = call_gamma * call_oi * CONTRACT_MULTIPLIER * spot_sq if call_gamma else 0.0
            put_gex = put_gamma * put_oi * CONTRACT_MULTIPLIER * spot_sq if put_gamma else 0.0
            # Dealer net at strike: put long − call short (standard convention)
            net_gex = put_gex - call_gex

            call_dex = call_delta * call_oi * CONTRACT_MULTIPLIER * spot if call_delta else 0.0
            put_dex = put_delta * put_oi * CONTRACT_MULTIPLIER * spot if put_delta else 0.0
            # Dealer net delta: call long − put short (mirror convention)
            net_dex = call_dex - put_dex

            by_strike.append({
                "strike": strike,
                "call_gex": call_gex, "put_gex": put_gex, "net_gex": net_gex,
                "call_dex": call_dex, "put_dex": put_dex, "net_dex": net_dex,
                "call_oi": call_oi, "put_oi": put_oi,
            })
            exp_net_gex += net_gex
            exp_net_dex += net_dex
            all_strikes_gex[strike] += net_gex

        exposures_by_date[exp] = {
            "by_strike": by_strike,
            "totals": {"net_gex": exp_net_gex, "net_dex": exp_net_dex},
        }
        portfolio_net_gex += exp_net_gex
        portfolio_net_dex += exp_net_dex

    # Wall selection — operate on aggregated cross-expiration GEX per strike
    call_wall = _select_call_wall(all_strikes_gex, spot)
    put_wall = _select_put_wall(all_strikes_gex, spot)

    return {
        "exposures_by_date": exposures_by_date,
        "portfolio_totals": {
            "net_gex": portfolio_net_gex,
            "net_dex": portfolio_net_dex,
        },
        "key_levels": {
            "call_wall": call_wall,
            "put_wall": put_wall,
        },
    }


def _select_call_wall(strike_gex: dict, spot: float) -> dict | None:
    """Strike ABOVE spot with the most-NEGATIVE dealer_gex.
    Negative = forced-buy zone (call wall acting as resistance via mechanical hedging).
    Tie-break: lower strike wins (closer to spot is the more relevant level)."""
    candidates = [(k, g) for k, g in strike_gex.items() if k > spot and g < 0]
    if not candidates:
        return None
    # Sort by (most-negative gex, then lower strike)
    best = min(candidates, key=lambda x: (x[1], x[0]))
    return {"strike": best[0], "gex": best[1]}


def _select_put_wall(strike_gex: dict, spot: float) -> dict | None:
    """Strike BELOW spot with the most-POSITIVE dealer_gex.
    Positive = forced-sell zone (put wall acting as support).
    Tie-break: higher strike wins."""
    candidates = [(k, g) for k, g in strike_gex.items() if k < spot and g > 0]
    if not candidates:
        return None
    best = max(candidates, key=lambda x: (x[1], x[0]))
    return {"strike": best[0], "gex": best[1]}


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return f if f == f else None  # filter NaN
    except (TypeError, ValueError):
        return None


def _empty_result() -> dict:
    return {
        "exposures_by_date": {},
        "portfolio_totals": {"net_gex": 0.0, "net_dex": 0.0},
        "key_levels": {"call_wall": None, "put_wall": None},
    }
