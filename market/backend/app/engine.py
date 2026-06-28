"""End-to-end engine: chain → dealer exposures → bias → all-8-strategy candidates.

Single entry point: `compute_engine_output(symbol, spot, chain_contracts,
chosen_expiration, chosen_dte, expected_move=None)`.

Returns one dict containing everything the UI needs to render a single poll
cycle: spot, expected_move, full bias-signals payload, per-strategy best
candidate + all width variants, an engine_pick chosen from the bias-engine
recommended strategies, and a derived predicted_direction for self-eval.

The strategy engine already fans candidates into 3 width-variants (Conservative
/ Balanced / Aggressive) per width_factor.  To honor the spec ("backend runs
BOTH 'balanced' and 'generous' widths internally, picks higher composite"),
we run the fan-out twice — once with width_factor=1.0 (balanced base) and
once with 1.4 (generous base) — merge the union, and keep the single highest-
composite candidate per strategy.  The full union is also surfaced as
`all_widths` so the UI can inspect alternative variants if desired.
"""
from __future__ import annotations

from .dealer_exposures import compute_dealer_exposures
from .bias_engine import compute_bias_signals
from .strategy_engine import generate_candidates
from .lookup_tables import STRATEGIES, WIDTH_PREFS
from .external_gex import state as ext_state
from .config import get_settings


ALL_STRATEGIES: list[str] = [
    "bull_put",
    "bear_call",
    "iron_condor",
    "iron_butterfly",
    "bull_call",
    "bear_put",
    "call_butterfly",
    "put_butterfly",
]


def _direction_from_bias(bias_label: str) -> str:
    if bias_label in ("bullish", "mild_bullish"):
        return "up"
    if bias_label in ("bearish", "mild_bearish"):
        return "down"
    return "flat"


def _estimate_expected_move(contracts: list[dict], spot: float) -> float:
    """Estimate expected_move from the ATM straddle (call mid + put mid at strike
    closest to spot).  Falls back to spot*0.005 (~0.5%) when no ATM strike."""
    if not contracts or not spot:
        return max(0.01, (spot or 1.0) * 0.005)
    # Group contracts by strike, find strike closest to spot
    strikes: dict[float, dict[str, dict]] = {}
    for c in contracts:
        try:
            k = float(c.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if k <= 0:
            continue
        strikes.setdefault(k, {})[(c.get("side") or "").lower()] = c
    if not strikes:
        return max(0.01, spot * 0.005)
    atm_strike = min(strikes.keys(), key=lambda k: abs(k - spot))
    sides = strikes.get(atm_strike) or {}
    call = sides.get("call") or {}
    put = sides.get("put") or {}

    def mid(c: dict) -> float:
        m = c.get("mid")
        if m is not None:
            try:
                return float(m)
            except (TypeError, ValueError):
                pass
        try:
            return (float(c.get("bid") or 0) + float(c.get("ask") or 0)) / 2
        except (TypeError, ValueError):
            return 0.0

    em = mid(call) + mid(put)
    if em <= 0:
        return max(0.01, spot * 0.005)
    return em


def compute_engine_output(
    symbol: str,
    spot: float,
    chain_contracts: list[dict],
    chosen_expiration: str,
    chosen_dte: float,
    expected_move: float | None = None,
    strike_profile=None,
) -> dict:
    """Full pipeline.  See module docstring for return shape.

    `strike_profile` (StrikeProfile | None): when supplied + enabled, debit
    verticals use the OTM-OTM smart picker instead of the legacy deep-ITM mirror.
    None keeps legacy behavior (used by parity/unit callers)."""
    settings = get_settings()
    # 1. Dealer exposures (per-strike net_gex/net_dex/net_vex/net_tex).
    # GEX leg weights by OI (default) or today's traded volume per GEX_SOURCE.
    de = compute_dealer_exposures(chain_contracts, spot, gex_weight=settings.gex_source)
    exposures_for_chosen = de.get("exposures_by_date", {}).get(chosen_expiration)
    # Some chains may not include the chosen expiration in the bucket map
    # (e.g. if all contracts had no expiration field). Fall back to first.
    if exposures_for_chosen is None:
        # try any single bucket
        buckets = de.get("exposures_by_date") or {}
        if buckets:
            exposures_for_chosen = next(iter(buckets.values()))
        else:
            exposures_for_chosen = {"by_strike": [], "totals": {}}

    # --- External GEX overlay (EdgelaneProvider browser-extension webhook) -------
    # When fresh external data exists for this symbol, prefer it. If the payload
    # carries the full signed net-GEX-by-strike book, we replace the per-strike
    # rows the wall/bias layer reads (below). The explicit put_wall/call_wall
    # points are surfaced on the returned bias dict by the post-compute overlay
    # (find_gex_wall / find_gex_walls_bilateral scan per-strike rows only, not
    # key_levels). We DO NOT touch the math layer.
    external = None
    if settings.use_external_gex:
        external = ext_state.get_if_fresh(symbol, settings.external_gex_timeout_sec)

    # When the EdgelaneProvider payload carries the full signed net-GEX-by-strike book,
    # replace the per-strike rows the bias/wall layer reads (which otherwise come
    # from the Tradier OI chain) with the EdgelaneProvider profile. This makes the WHOLE
    # structure — every support floor and resistance ceiling, above and below
    # spot — drive the bias, not just two overlaid wall points. Strategy
    # generation still uses the Tradier contracts (it needs real option pricing).
    ext_profile = False
    if external:
        _p = external["payload"]
        _s = _p.get("strikes")
        _g = _p.get("net_gex_by_strike")
        if _s and _g and len(_s) == len(_g) and len(_s) >= 5:
            exposures_for_chosen = {
                "by_strike": [
                    {"strike": float(s), "net_gex": float(g)}
                    for s, g in zip(_s, _g)
                ],
                "totals": {},
            }
            ext_profile = True

    base_key_levels = de.get("key_levels", {}) or {}
    if external:
        ext_payload = external["payload"]
        ext_put_strike  = ext_payload.get("put_wall_strike")
        ext_call_strike = ext_payload.get("call_wall_strike")
        ext_put_gex     = ext_payload.get("put_wall_net_gex") or 0
        ext_call_gex    = ext_payload.get("call_wall_net_gex") or 0
        overlay_kl: dict = dict(base_key_levels)
        if ext_put_strike is not None:
            overlay_kl["put_wall"] = {"strike": ext_put_strike, "value": ext_put_gex}
        if ext_call_strike is not None:
            overlay_kl["call_wall"] = {"strike": ext_call_strike, "value": ext_call_gex}
        greeks_raw = {
            "key_levels": overlay_kl,
            "portfolio_totals": de.get("portfolio_totals", {}),
            "exposures_by_date": de.get("exposures_by_date", {}),
            "_external_source": "edgelane_provider_extension",
            "_external_received_at": external["received_at_iso"],
        }
    else:
        greeks_raw = {
            "key_levels": base_key_levels,
            "portfolio_totals": de.get("portfolio_totals", {}),
        }

    # 2. Bias signals (intraday provider profile for dte < 1.5; barrier scorer otherwise)
    # Flow-dollars direction inputs (private GEX provider profile path):
    # buy/sell-signed cumulative call$ vs put$, supplied by the extension.
    _flow_cp = external["payload"].get("call_premium") if external else None
    _flow_pp = external["payload"].get("put_premium") if external else None

    bias = compute_bias_signals(
        symbol=symbol,
        expiration=chosen_expiration,
        spot=spot,
        greeks_raw=greeks_raw,
        exposures_for_chosen=exposures_for_chosen,
        regime_override=None,
        chosen_dte=chosen_dte,
        external_profile=ext_profile,
        flow_call_premium=_flow_cp,
        flow_put_premium=_flow_pp,
    )

    # --- Post-compute overlay: surface external walls in the bias output --
    # find_gex_walls_bilateral() in walls.py only scans per-strike rows, so
    # to honor "prefer external when fresh" we transparently overlay the
    # bilateral walls + gex_wall_strike on the returned bias dict. This is
    # output-layer overlay only — the bias engine's math is untouched.
    if external:
        ext_payload = external["payload"]
        if ext_payload.get("put_wall_strike") is not None:
            bias["put_wall_strike"]   = ext_payload.get("put_wall_strike")
            bias["put_wall_strength"] = ext_payload.get("put_wall_strength") or bias.get("put_wall_strength")
            if ext_payload.get("put_wall_net_gex") is not None:
                bias["put_wall_net_gex"] = ext_payload.get("put_wall_net_gex")
        if ext_payload.get("call_wall_strike") is not None:
            bias["call_wall_strike"]   = ext_payload.get("call_wall_strike")
            bias["call_wall_strength"] = ext_payload.get("call_wall_strength") or bias.get("call_wall_strength")
            if ext_payload.get("call_wall_net_gex") is not None:
                bias["call_wall_net_gex"] = ext_payload.get("call_wall_net_gex")
        # gex_wall (magnet ticker): when we have the full per-strike book
        # (ext_profile), compute_balanced_magnet already set gex_wall_* with the
        # proximity-weighted tug-of-war (balanced-aware) — do NOT clobber it with
        # raw argmax here. Only fall back to the explicit-field/argmax overlay
        # when there's no full profile (walls-only payload).
        if not ext_profile:
            ext_gex_wall = ext_payload.get("gex_wall_strike")
            pw_strike = ext_payload.get("put_wall_strike")
            cw_strike = ext_payload.get("call_wall_strike")
            pw_g = abs(float(ext_payload.get("put_wall_net_gex") or 0))
            cw_g = abs(float(ext_payload.get("call_wall_net_gex") or 0))
            if ext_gex_wall is None:
                if pw_strike is not None and cw_strike is not None:
                    ext_gex_wall = pw_strike if pw_g >= cw_g else cw_strike
                else:
                    ext_gex_wall = pw_strike if pw_strike is not None else cw_strike
            if ext_gex_wall is not None:
                bias["gex_wall_strike"] = ext_gex_wall
                if pw_g >= cw_g and pw_strike is not None:
                    bias["gex_wall_type"]     = "put"
                    bias["gex_wall_strength"] = ext_payload.get("put_wall_strength") or bias.get("gex_wall_strength")
                elif cw_strike is not None:
                    bias["gex_wall_type"]     = "call"
                    bias["gex_wall_strength"] = ext_payload.get("call_wall_strength") or bias.get("gex_wall_strength")
        bias["_data_source"] = "edgelane_provider"
        # Direction came from the flow-dollars rule iff the extension
        # supplied premiums; else it's the magnet-vs-spot fallback.
        _flow_dir_used = ext_profile and _flow_cp is not None and _flow_pp is not None
        bias["_score_method"] = ("edgelane_provider_flow_dir" if _flow_dir_used else
                                 "edgelane_provider_magnet" if ext_profile else "single_wall_magnet")
        if _flow_cp is not None and _flow_pp is not None:
            bias["_flow_call_premium_m"] = round(float(_flow_cp) / 1e6, 2)
            bias["_flow_put_premium_m"]  = round(float(_flow_pp) / 1e6, 2)
        bias["_profile_strikes"] = len(exposures_for_chosen.get("by_strike") or []) if ext_profile else 0
        bias["_external_received_at"] = external["received_at_iso"]
        # vol_expectation: SEPARATE direction signal (NOT the GEX magnet bias).
        ep = external["payload"]
        sw = ep.get("swing_sentiment")
        bias["_vol_expectation"] = {
            "swing": sw,
            "longterm": ep.get("longterm_sentiment"),
            # SEPARATE tag -- sign of swing_sentiment, NOT sign(magnet - spot):
            "tag": ("bullish" if (sw is not None and sw > 0)
                    else "bearish" if (sw is not None and sw < 0)
                    else "neutral"),
            "is_primary": False,
        }
        bias["_displayed_target"]    = ep.get("displayed_target")
        bias["_displayed_sentiment"] = ep.get("displayed_sentiment")
        if ext_profile:
            bias["_profile_by_strike"] = exposures_for_chosen.get("by_strike")
    else:
        bias["_data_source"] = "tradier_volume" if settings.gex_source == "volume" else "tradier_oi"
        bias["_score_method"] = "single_wall_magnet"

    # 3. Expected move (ATM straddle, or fallback 0.5% of spot)
    em = expected_move if expected_move is not None else _estimate_expected_move(chain_contracts, spot)

    # 4. Wall payload for strategy engine (per v4.7.59 signature)
    walls = None
    if bias.get("gex_wall_strike") is not None:
        walls = {
            "strike": bias["gex_wall_strike"],
            "strength": bias["gex_wall_strength"],
            "type": bias["gex_wall_type"],
            "net_gex": bias["net_gex"],
            "dte": chosen_dte,
        }

    recommended = set(bias.get("recommended_strategies") or [])

    # 5. Per strategy: run generate_candidates with BOTH balanced (1.0) and
    #    generous (1.4) width factors, take the union, keep the single
    #    highest-composite candidate per strategy.
    target_delta = 0.20  # JSX default; mirrors TARGET_DELTAS first entry
    width_factors = {
        "balanced": WIDTH_PREFS["balanced"]["factor"],
        "generous": WIDTH_PREFS["generous"]["factor"],
    }

    strategies_out: dict[str, dict] = {}
    for strat in ALL_STRATEGIES:
        all_variants: list[dict] = []
        for width_name, wf in width_factors.items():
            try:
                variants = generate_candidates(
                    strategy=strat,
                    contracts=chain_contracts,
                    dte=chosen_dte,
                    expected_move=em,
                    target_delta=target_delta,
                    width_factor=wf,
                    walls=walls,
                    spot=spot,
                    profile=strike_profile,
                )
            except Exception:
                variants = []
            for v in variants:
                v = dict(v)
                v["width_preset"] = width_name
                v["width_factor"] = wf
                v["strategy"] = strat
                all_variants.append(v)

        best = None
        if all_variants:
            best = max(all_variants, key=lambda c: c.get("composite_score") or 0)

        strategies_out[strat] = {
            "strategy": strat,
            "name": STRATEGIES[strat]["name"],
            "short": STRATEGIES[strat]["short"],
            "type": STRATEGIES[strat]["type"],
            "side": STRATEGIES[strat]["side"],
            "is_recommended": strat in recommended,
            "best": best,
            "all_widths": all_variants,
        }

    # 6. Engine pick: highest composite_score among RECOMMENDED strategies
    pick = None
    rec_candidates = [
        s for s in strategies_out.values()
        if s["is_recommended"] and s["best"] is not None
    ]
    if rec_candidates:
        winner = max(rec_candidates, key=lambda s: s["best"].get("composite_score") or 0)
        b = winner["best"]
        pick = {
            "strategy": winner["strategy"],
            "name": winner["name"],
            "short": winner["short"],
            "spread_type": b.get("type"),   # 'credit' | 'debit' — drives outcome eval
            "label": b.get("label"),
            "width_preset": b.get("width_preset"),
            "composite_score": b.get("composite_score"),
            "composite_verdict": b.get("composite_verdict"),
            "structure_text": b.get("structure_text"),
            "net_premium": b.get("net_premium"),
            "max_profit": b.get("max_profit"),
            "max_loss": b.get("max_loss"),
            "pop_pct": b.get("pop_pct"),
            "ev": b.get("ev"),
            "ev_adjusted": b.get("ev_adjusted"),
            "health": b.get("health"),
            "liquidity": b.get("liquidity"),
            "strikes": b.get("strikes"),
            "legs": b.get("legs"),
            "predicted_direction": _direction_from_bias(bias.get("bias_label") or "neutral"),
        }

    return {
        "symbol": symbol,
        "spot": spot,
        "expiration": chosen_expiration,
        "dte": chosen_dte,
        "expected_move": em,
        "bias": bias,
        "strategies": strategies_out,
        "engine_pick": pick,
    }
