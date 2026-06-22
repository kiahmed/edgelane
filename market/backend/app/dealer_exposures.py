"""Per-strike dealer-exposure aggregator — Python port of _computeDealerExposures
from spread_optimizer_v4_7_html.jsx.

Input shape:
    contracts: list of dict, each with
        symbol, strike, side ('call'|'put'), expiration,
        delta, gamma, theta, vega, open_interest, volume,
        bid, ask, last, iv
    spot: float

Output shape (matches JSX exactly):
    {
        exposures_by_date: { 'YYYY-MM-DD': { by_strike: [...], totals: {...} } },
        portfolio_totals: { net_gex, net_dex, net_vex, net_tex },
        key_levels: { call_wall, put_wall, vex_wall, tex_wall },
    }
"""
from __future__ import annotations
from typing import Any, Iterable
from .lookup_tables import DEALER_CONTRACT_MULT


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _empty_dealer_exposures() -> dict:
    return {
        "exposures_by_date": {},
        "portfolio_totals": {"net_gex": 0.0, "net_dex": 0.0, "net_vex": 0.0, "net_tex": 0.0},
        "key_levels": {"call_wall": None, "put_wall": None, "vex_wall": None, "tex_wall": None},
    }


def compute_dealer_exposures(
    contracts: Iterable[dict], spot: float, gex_weight: str = "oi"
) -> dict:
    """Aggregate GEX/DEX/VEX/TEX per (expiration, strike).

    Sign convention (matches JSX):
        net_gex = put_gex − call_gex    (positive = puts dominate)
        net_dex = call_dex − put_dex    (positive = call delta dominates)
        net_vex = put_vex − call_vex
        net_tex = put_tex − call_tex

    These signs are what the bias engine and wall finder expect downstream.

    gex_weight selects the contract-count term for the GEX leg ONLY:
        "oi"     -> gamma × open_interest  (resting positioning; the default,
                    and what the 56 parity tests lock — do not change)
        "volume" -> gamma × volume         (today's traded gamma; lands closer
                    to the flow-based provider walls for 0DTE)
    DEX/VEX/TEX always stay on open_interest — only the magnet/wall layer
    (which reads net_gex) is affected by the volume swap.
    """
    use_volume = (gex_weight or "oi").lower() == "volume"
    contracts = list(contracts)
    if not contracts or not spot or spot <= 0:
        return _empty_dealer_exposures()

    # buckets: {exp: {strike: {'call': contract, 'put': contract}}}
    buckets: dict[str, dict[float, dict[str, dict]]] = {}
    for c in contracts:
        exp = c.get("expiration")
        strike = _num(c.get("strike"))
        side = (c.get("side") or "").lower()
        if exp is None or strike is None or side not in ("call", "put"):
            continue
        buckets.setdefault(exp, {}).setdefault(strike, {})[side] = c

    if not buckets:
        return _empty_dealer_exposures()

    spot_sq = spot * spot

    exposures_by_date: dict[str, dict] = {}
    port_net_gex = port_net_dex = port_net_vex = port_net_tex = 0.0
    all_strikes_gex: dict[float, float] = {}
    all_strikes_vex: dict[float, float] = {}
    all_strikes_tex: dict[float, float] = {}

    for exp, sm in buckets.items():
        sorted_strikes = sorted(sm.keys())
        by_strike: list[dict] = []
        exp_net_gex = exp_net_dex = exp_net_vex = exp_net_tex = 0.0

        for strike in sorted_strikes:
            sides = sm[strike]
            call = sides.get("call") or {}
            put = sides.get("put") or {}
            c_g = _num(call.get("gamma")) or 0.0
            p_g = _num(put.get("gamma")) or 0.0
            c_d = _num(call.get("delta")) or 0.0
            p_d = _num(put.get("delta")) or 0.0
            c_v = _num(call.get("vega")) or 0.0
            p_v = _num(put.get("vega")) or 0.0
            c_t = _num(call.get("theta")) or 0.0
            p_t = _num(put.get("theta")) or 0.0
            c_oi = int(_num(call.get("open_interest")) or 0)
            p_oi = int(_num(put.get("open_interest")) or 0)

            # GEX leg weights by OI (default) or today's traded volume.
            if use_volume:
                c_gw = int(_num(call.get("volume")) or 0)
                p_gw = int(_num(put.get("volume")) or 0)
            else:
                c_gw, p_gw = c_oi, p_oi

            call_gex = c_g * c_gw * DEALER_CONTRACT_MULT * spot_sq
            put_gex  = p_g * p_gw * DEALER_CONTRACT_MULT * spot_sq
            net_gex  = put_gex - call_gex

            call_dex = c_d * c_oi * DEALER_CONTRACT_MULT * spot
            put_dex  = p_d * p_oi * DEALER_CONTRACT_MULT * spot
            net_dex  = call_dex - put_dex

            call_vex = c_v * c_oi * DEALER_CONTRACT_MULT
            put_vex  = p_v * p_oi * DEALER_CONTRACT_MULT
            net_vex  = put_vex - call_vex

            call_tex = c_t * c_oi * DEALER_CONTRACT_MULT
            put_tex  = p_t * p_oi * DEALER_CONTRACT_MULT
            net_tex  = put_tex - call_tex

            by_strike.append({
                "strike": strike,
                "call_gex": call_gex, "put_gex": put_gex, "net_gex": net_gex,
                "call_dex": call_dex, "put_dex": put_dex, "net_dex": net_dex,
                "call_vex": call_vex, "put_vex": put_vex, "net_vex": net_vex,
                "call_tex": call_tex, "put_tex": put_tex, "net_tex": net_tex,
                "call_oi": c_oi, "put_oi": p_oi,
                "call_vol": int(_num(call.get("volume")) or 0),
                "put_vol":  int(_num(put.get("volume")) or 0),
                "gex_weight": "volume" if use_volume else "oi",
            })

            exp_net_gex += net_gex
            exp_net_dex += net_dex
            exp_net_vex += net_vex
            exp_net_tex += net_tex
            all_strikes_gex[strike] = all_strikes_gex.get(strike, 0.0) + net_gex
            all_strikes_vex[strike] = all_strikes_vex.get(strike, 0.0) + net_vex
            all_strikes_tex[strike] = all_strikes_tex.get(strike, 0.0) + net_tex

        exposures_by_date[exp] = {
            "by_strike": by_strike,
            "totals": {
                "net_gex": exp_net_gex,
                "net_dex": exp_net_dex,
                "net_vex": exp_net_vex,
                "net_tex": exp_net_tex,
            },
        }
        port_net_gex += exp_net_gex
        port_net_dex += exp_net_dex
        port_net_vex += exp_net_vex
        port_net_tex += exp_net_tex

    # Key levels — call wall (most-negative net_gex above spot),
    # put wall (most-positive net_gex below spot), vex/tex peak by |abs|
    call_wall = put_wall = None
    best_call_gex = best_put_gex = 0.0
    for strike, gex in all_strikes_gex.items():
        if strike > spot and gex < best_call_gex:
            best_call_gex = gex
            call_wall = {"strike": strike, "gex": gex}
        if strike < spot and gex > best_put_gex:
            best_put_gex = gex
            put_wall = {"strike": strike, "gex": gex}

    vex_wall = None
    best_abs_vex = 0.0
    for strike, vex in all_strikes_vex.items():
        if abs(vex) > best_abs_vex:
            best_abs_vex = abs(vex)
            vex_wall = {"strike": strike, "vex": vex}

    tex_wall = None
    best_abs_tex = 0.0
    for strike, tex in all_strikes_tex.items():
        if abs(tex) > best_abs_tex:
            best_abs_tex = abs(tex)
            tex_wall = {"strike": strike, "tex": tex}

    return {
        "exposures_by_date": exposures_by_date,
        "portfolio_totals": {
            "net_gex": port_net_gex, "net_dex": port_net_dex,
            "net_vex": port_net_vex, "net_tex": port_net_tex,
        },
        "key_levels": {
            "call_wall": call_wall, "put_wall": put_wall,
            "vex_wall": vex_wall, "tex_wall": tex_wall,
        },
    }
