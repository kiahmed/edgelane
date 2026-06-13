"""Bias engine -- Python port of _computeBiasSignals + scoring helpers from
spread_optimizer_v4_7_html.jsx (v4.7.57, .59, .61, .63-.67).

Public entry point: compute_bias_signals(symbol, expiration, spot, greeks_raw,
exposures_for_chosen, regime_override, chosen_dte) -- returns the same dict
shape the JSX bias engine produces, ready for parity comparison.
"""
from __future__ import annotations
import math
from typing import Any
from .lookup_tables import STRATEGIES, BIAS_TO_STRATEGY
from .walls import find_gex_wall, find_gex_walls_bilateral, _strike_rows_from, _gex_from_row


# --- EdgelaneProvider external-profile magnet tuning (external_profile path only) ---
_MAGNET_SHARE_DEADZONE = 0.08   # magnet < 8% of total |gex| book => no clear magnet => neutral
_MAGNET_SHARE_FULL     = 0.20   # magnet >= 20% of book => full dominance (mirrors walls.classify 'high')
_MAGNET_MIN_DIST       = 0.0005 # magnet within 0.05% of spot => pinned/neutral
_MAGNET_DAMP_K         = 0.75   # max conviction removed by an opposing green wall (cap, never full reversal)


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _js_round(x: float) -> int:
    """JS-style Math.round: round half AWAY from zero.

    Python's builtin round() uses banker's rounding (round(0.5) == 0); JS
    Math.round(0.5) == 1. This matters at .5 boundaries (e.g. 742.5 -> 743
    in JS but 742 in Python). Used in the directional/EdgelaneProvider scorers to
    match the JSX engine bit-for-bit at edge cases.
    """
    if x >= 0:
        return math.floor(x + 0.5)
    return -math.floor(-x + 0.5)


def _aggregate_exposures(exposures_for_chosen: dict | None, portfolio_totals: dict | None) -> dict:
    """Sum net_gex / net_dex across per-strike rows for the chosen expiration.
    Falls back to portfolio_totals when no per-strike rows exist."""
    rows = _strike_rows_from(exposures_for_chosen)
    net_gex = 0.0
    net_dex = 0.0
    for r in rows:
        net_gex += float(r.get("net_gex") or 0.0)
        net_dex += float(r.get("net_dex") or 0.0)
    if not rows and portfolio_totals:
        net_gex = float(portfolio_totals.get("net_gex") or portfolio_totals.get("gex") or 0.0)
        net_dex = float(portfolio_totals.get("net_dex") or portfolio_totals.get("dex") or 0.0)
    return {"net_gex": net_gex, "net_dex": net_dex}


def compute_directional_score(
    spot: float,
    wall: dict,
    net_gex: float,
    net_dex: float,
    dte: float | None,
) -> float:
    """Legacy single-wall barrier-or-magnet score (used for multi-day plays).

    Mirrors JSX `_computeDirectionalScore` post-v4.7.63.
    """
    if not spot or wall.get("strike") is None:
        return 0.0
    pct_from_wall = (spot - wall["strike"]) / wall["strike"]
    score = pct_from_wall * 200

    strength = wall.get("strength")
    strength_mult = 1.5 if strength == "high" else 1.0 if strength == "medium" else 0.5
    score *= strength_mult

    intraday = dte is not None and dte < 1.5

    if not intraday and net_gex > 0:
        score *= 0.3
        if net_dex != 0:
            score += 10 * (1 if net_dex > 0 else -1)
    elif intraday:
        dist_pct = abs(pct_from_wall)
        if dist_pct < 0.005:
            reach = 6.0
        elif dist_pct < 0.01:
            reach = 4.0
        elif dist_pct < 0.02:
            reach = 2.0
        elif dist_pct < 0.03:
            reach = 1.0
        else:
            reach = 0.3
        score = -score * 8 * reach
        if net_dex != 0:
            score += 15 * (1 if net_dex > 0 else -1)
    elif net_gex < 0:
        score *= 1.2
        if net_dex != 0:
            score += -10 * (1 if net_dex > 0 else -1)

    return max(-100.0, min(100.0, _js_round(score * 10) / 10))


def compute_edgelane_provider_score(
    spot: float,
    put_wall: dict | None,
    call_wall: dict | None,
) -> float:
    """EdgelaneProvider-style asymmetric scorer for 0DTE/1DTE (v4.7.67)."""
    if not spot or not put_wall or put_wall.get("strike") is None:
        if call_wall and call_wall.get("strike"):
            call_dist = (call_wall["strike"] - spot) / spot
            strength = call_wall.get("strength")
            sm = 1.5 if strength == "high" else 1.0 if strength == "medium" else 0.5
            sign = 1 if call_dist > 0 else -1 if call_dist < 0 else 0
            return max(-30.0, min(30.0, _js_round(-sign * 20 * sm * 10) / 10))
        return 0.0

    put_dist_pct = (put_wall["strike"] - spot) / spot
    put_dir = 1 if put_dist_pct > 0 else -1 if put_dist_pct < 0 else 0
    dist_abs = abs(put_dist_pct)

    if dist_abs < 0.005:
        reach = 1.0
    elif dist_abs < 0.01:
        reach = 0.80
    elif dist_abs < 0.02:
        reach = 0.55
    elif dist_abs < 0.03:
        reach = 0.35
    else:
        reach = 0.15

    strength = put_wall.get("strength")
    sm = 1.5 if strength == "high" else 1.0 if strength == "medium" else 0.5
    magnet_score = put_dir * 90 * sm * reach

    if call_wall and call_wall.get("strike"):
        call_dist_pct = (call_wall["strike"] - spot) / spot
        same_side = (1 if call_dist_pct > 0 else -1 if call_dist_pct < 0 else 0) == put_dir
        closer = abs(call_dist_pct) < dist_abs
        if same_side and closer:
            call_mag = abs(call_wall.get("net_gex") or 0.0)
            put_mag = abs(put_wall.get("net_gex") or 0.0)
            block_ratio = call_mag / max(call_mag + put_mag, 1e-9)
            magnet_score *= (1 - block_ratio * 0.75)

    return max(-100.0, min(100.0, _js_round(magnet_score * 10) / 10))


def _reach(dist_abs: float) -> float:
    """Proximity weight for an intraday level (same tiers as the magnet scorers).
    Nearer strikes exert more pull/push on a 0DTE tape."""
    if dist_abs < 0.005:
        return 1.0
    if dist_abs < 0.01:
        return 0.80
    if dist_abs < 0.02:
        return 0.55
    if dist_abs < 0.03:
        return 0.35
    return 0.15


def compute_profile_score(spot: float, by_strike: list[dict]) -> float:
    """Holistic directional read over the ENTIRE signed net-GEX-by-strike
    profile (EdgelaneProvider full-book path — not just the single nearest wall).

    Every strike casts a distance-weighted vote:
      • positive net_gex (put / support cluster) = MAGNET → pulls price TOWARD
        the strike:        vote = sign(strike - spot) * |gex| * reach
      • negative net_gex (call / resistance cluster) = BARRIER → pushes price
        AWAY (a ceiling overhead presses down; a floor below props up):
                           vote = -sign(strike - spot) * |gex| * reach

    Net of all votes / total weight, scaled to [-100, 100]. This means a put
    wall just overhead no longer single-handedly forces "bullish" — it is
    weighed against every other support floor and resistance ceiling in the
    book, above and below spot.
    """
    if not spot or not by_strike:
        return 0.0
    num = 0.0
    den = 0.0
    for r in by_strike:
        s = _num(r.get("strike"))
        if s is None or s <= 0:
            continue
        g = _gex_from_row(r)            # signed: + put-side, - call-side
        if g == 0:
            continue
        dist = (s - spot) / spot
        if dist == 0:
            continue
        direction = 1.0 if dist > 0 else -1.0
        weight = _reach(abs(dist))
        mag = abs(g)
        # magnet pulls toward the strike; barrier repels from it.
        vote = (direction if g > 0 else -direction) * mag * weight
        num += vote
        den += mag * weight
    if den <= 0:
        return 0.0
    score = (num / den) * 100.0
    return max(-100.0, min(100.0, _js_round(score * 10) / 10))


def compute_magnet_direction_score(spot, by_strike, put_wall, call_wall):
    """EdgelaneProvider 0DTE magnet-direction scorer (external_profile path ONLY).

    SIGN CONVENTION (extension): net_gex>0 = PUT-side = MAGNET
    (price pulled TOWARD it); net_gex<0 = CALL-side = RESISTANCE (repel/cap).

    MAGNET = biggest put bar = put_wall (argmax net_gex). Direction = which side of
    spot the magnet sits on.
        magnet ABOVE spot -> BULLISH (+)
        magnet BELOW spot -> BEARISH (-)
    Conviction = proximity (intraday reachability) * dominance (magnet's share of the
    whole book) * (1 - green_damp). A CALL resistance bar sitting BETWEEN spot
    and the magnet dampens conviction.

    Returns (score in [-100,100], magnet_dict|None). magnet_dict is ALWAYS the
    biggest put bar (so the gex_wall ticker shows the red magnet), even when the
    score is neutralised by the dead-zone.
    """
    # No red magnet at all -> nothing to pull toward; leave caller's wall untouched.
    if not spot or not put_wall or put_wall.get("strike") is None:
        return 0.0, None

    magnet_strike = float(put_wall["strike"])
    magnet_mag    = abs(float(put_wall.get("net_gex") or 0.0))   # >0 by construction

    magnet = {
        "strike":   magnet_strike,
        "strength": put_wall.get("strength"),
        "type":     "put",                 # put-side magnet (provider target)
        "net_gex":  put_wall.get("net_gex"),
    }

    # total book weight for dominance share
    total_abs = 0.0
    for r in (by_strike or []):
        total_abs += abs(_gex_from_row(r))
    if total_abs <= 0 or magnet_mag <= 0:
        return 0.0, magnet

    dist = (magnet_strike - spot) / spot
    # pinned-at-spot dead-zone -> neutral but still show the ticker
    if abs(dist) < _MAGNET_MIN_DIST:
        return 0.0, magnet
    sign = 1.0 if dist > 0 else -1.0      # + above spot = bullish ; - below = bearish

    # dominance: how much the magnet stands out from the rest of the book
    share = magnet_mag / total_abs
    if share < _MAGNET_SHARE_DEADZONE:    # no clear magnet -> honest neutral
        return 0.0, magnet
    dominance = min(1.0, share / _MAGNET_SHARE_FULL)

    # proximity: nearer = more reachable on a 0DTE tape (reuse existing tiers)
    proximity = _reach(abs(dist))

    # GREEN-BAR DAMPING: sum |net_gex| of CALL-side (net_gex<0) bars strictly
    # BETWEEN spot and the magnet -- only an OPPOSING wall in the PATH obstructs.
    lo, hi = (spot, magnet_strike) if spot <= magnet_strike else (magnet_strike, spot)
    resist_between = 0.0
    for r in (by_strike or []):
        s = _num(r.get("strike"))
        if s is None or not (lo < s < hi):
            continue
        g = _gex_from_row(r)
        if g < 0:                          # green/call resistance in the path
            resist_between += abs(g)
    damp = min(1.0, resist_between / magnet_mag) * _MAGNET_DAMP_K   # in [0, 0.75]

    conviction = max(0.0, dominance * proximity * (1.0 - damp))
    score = sign * 100.0 * conviction
    score = max(-100.0, min(100.0, _js_round(score * 10) / 10))
    return score, magnet


def compute_magnet_score(spot: float, gex_wall_strike: float | None) -> float:
    """Intraday magnet score (private GEX provider profile path).

    The magnet target is the single strike with the largest |GEX| in the 0DTE
    flow — i.e. our `gex_wall`. The intraday verdict is simply which side of spot
    that magnet sits on: above → price drifts up (bullish), below → bearish.

    Conviction scales with distance: a magnet pinned at spot is weak (price is
    already there), clear separation is strong, and an unreachably-far magnet
    fades (can't get there intraday). `gex_wall_strike` is picked upstream as
    argmax|net_gex| via find_gex_wall — NOT the biggest put/call wall.
    """
    if not spot or not gex_wall_strike:
        return 0.0
    dist = (gex_wall_strike - spot) / spot
    if dist == 0:
        return 0.0
    sign = 1.0 if dist > 0 else -1.0
    a = abs(dist)
    if a < 0.025:
        # ramp 0 → full conviction by ~0.9% (today's distance landed ~strong)
        conviction = min(1.0, a / 0.009)
    else:
        # too far to reach intraday → fade, floor at 0.2
        conviction = max(0.2, 1.0 - (a - 0.025) / 0.05)
    score = sign * 100.0 * conviction
    return max(-100.0, min(100.0, _js_round(score * 10) / 10))


def compute_balanced_magnet(spot: float, put_wall: dict | None, call_wall: dict | None):
    """Proximity-weighted tug-of-war for the external (private GEX provider) intraday path.

    Replaces the single argmax magnet (compute_magnet_score), which whipsawed
    when two near-equal walls straddled spot. Live 2026-06-12: put 7325 (4.21M,
    below) vs call 7500 (4.47M, above) were ~tied, so argmax flipped the magnet
    each poll -> bias swung bullish +100 / bearish -100 minute to minute.

    Each wall's pull = |GEX| / fractional-distance-to-spot (bigger AND nearer
    wins). The signed imbalance gives direction; below a dead-zone the book is
    balanced and we report neutral (honest "range-bound", not a saturated call).
    The dominant-by-pull wall becomes the displayed gex_wall ticker; in the
    dead-zone it is marked "balanced" so the UI shows a range, not a side.

    Returns (score, magnet_dict|None). Matching the provider's own *price target*
    is NOT attempted here — that number is the provider's model, not derivable
    from flow GEX magnitudes.
    """
    def pull(w):
        if not w or w.get("strike") is None or not spot:
            return 0.0
        dist = abs(w["strike"] - spot) / spot
        if dist < 1e-4:
            dist = 1e-4
        return abs(w.get("net_gex") or 0.0) / dist

    pull_call, pull_put = pull(call_wall), pull(put_wall)
    total = pull_call + pull_put
    if total <= 0:
        return 0.0, None

    imbalance = (pull_call - pull_put) / total  # +1 = call/upside, -1 = put/downside
    a = abs(imbalance)
    DEAD = 0.15
    conviction = 0.0 if a <= DEAD else min(1.0, (a - DEAD) / (1.0 - DEAD))
    sign = 1.0 if imbalance > 0 else -1.0
    score = max(-100.0, min(100.0, _js_round(sign * 100.0 * conviction * 10) / 10))

    dom = call_wall if pull_call >= pull_put else put_wall
    dom = dom or {}
    if a <= DEAD:
        magnet = {"strike": dom.get("strike"), "strength": "balanced", "type": "balanced"}
    else:
        magnet = {"strike": dom.get("strike"), "strength": dom.get("strength"),
                  "type": dom.get("type") or ("call" if pull_call > pull_put else "put")}
    return score, magnet


def score_to_bias_label(score: float) -> str:
    if score >= 60:  return "bullish"
    if score >= 20:  return "mild_bullish"
    if score <= -60: return "bearish"
    if score <= -20: return "mild_bearish"
    return "neutral"


def compute_confidence(score: float, wall: dict, net_dex: float, spot: float, dte: float | None) -> str:
    """Mirrors JSX `_computeConfidence` post-v4.7.59 (intraday reachability gate)."""
    abs_score = abs(score)
    if wall.get("strength") == "low" or wall.get("strike") is None:
        return "low"
    if abs_score < 15:
        return "low"

    wall_dir = 1 if spot > wall["strike"] else -1 if spot < wall["strike"] else 0
    dex_dir = -1 if net_dex > 0 else 1 if net_dex < 0 else 0
    aligned = wall_dir != 0 and dex_dir != 0 and wall_dir == dex_dir

    intraday = dte is not None and dte < 1.5
    wall_dist_pct = abs(spot - wall["strike"]) / spot if (wall.get("strike") and spot) else 0
    unreachable_intraday = intraday and wall_dist_pct > 0.015

    if wall.get("strength") == "high" and abs_score > 30 and aligned:
        base = "high"
    elif abs_score < 25:
        base = "low"
    else:
        base = "medium"

    if unreachable_intraday:
        if base == "high":   return "medium"
        if base == "medium": return "low"
        return "low"
    return base


def recommend_strategies(bias_label: str) -> list[str]:
    """Mirrors JSX `_recommendStrategies`. Iteration order of STRATEGIES must
    match the JSX declaration order; see lookup_tables.STRATEGIES note."""
    fits = [k for k, s in STRATEGIES.items() if bias_label in s["fits"]]
    primary = BIAS_TO_STRATEGY.get(bias_label)
    if primary and primary in fits:
        return [primary] + [k for k in fits if k != primary][:2]
    return fits[:3]


def _extract_greek_wall(
    greeks_raw: dict | None,
    exposures_for_chosen: dict | None,
    key_name: str,
    fallback_row_field: str,
) -> dict | None:
    kl = (greeks_raw or {}).get("key_levels") or (exposures_for_chosen or {}).get("key_levels") or {}
    direct = kl.get(key_name)
    if direct and direct.get("strike") is not None:
        return {"strike": float(direct["strike"]), "value": float(direct.get(fallback_row_field) or direct.get("value") or 0)}
    rows = _strike_rows_from(exposures_for_chosen)
    best: dict | None = None
    for r in rows:
        strike = _num(r.get("strike"))
        val = float(r.get(fallback_row_field) or 0)
        if strike is None or math.isnan(strike):
            continue
        if best is None or abs(val) > abs(best["value"]):
            best = {"strike": strike, "value": val}
    return best if (best and best["value"] != 0) else None


_FLOW_DIR_DEADBAND_M = 5.0   # $5M dead-band — matches EdgelaneProvider `diff > 5` exactly


def compute_flow_direction_score(call_premium: float | None, put_premium: float | None):
    """Flow-dollars market-direction signal (private GEX provider profile path):

        cflow = calls[last] (cumulative $M on calls)
        pflow = puts[last]  (cumulative $M on puts)
        diff  = abs(abs(cflow) - abs(pflow))
        Bullish if cflow > pflow and diff > 5
        Bearish if cflow < pflow and diff > 5
        Neutral otherwise            ( within $5M )

    `call_premium`/`put_premium` arrive in DOLLARS (buy/sell-signed per trade by
    the extension: +premium_spent for side>0, -premium_spent for side<0, with
    the provider's own filters: skip premium_spent>5M and skip abs(side)==1). This
    is the FLOW-dollars signal — independent of the GEX magnet/target.

    Returns (score in [-100,100], used: bool). Missing inputs => (None, False).
    """
    if call_premium is None or put_premium is None:
        return None, False
    cflow = float(call_premium) / 1e6
    pflow = float(put_premium) / 1e6
    diff = abs(abs(cflow) - abs(pflow))
    if diff <= _FLOW_DIR_DEADBAND_M:
        return 0.0, True                       # Neutral
    share = min(1.0, diff / (abs(cflow) + abs(pflow) + 1e-9))
    mag = max(20.0, min(100.0, _js_round(100.0 * share * 10) / 10))
    sign = 1.0 if cflow > pflow else -1.0
    return sign * mag, True


def compute_bias_signals(
    symbol: str,
    expiration: str,
    spot: float,
    greeks_raw: dict | None,
    exposures_for_chosen: dict | None,
    regime_override: dict | None = None,
    chosen_dte: float | None = None,
    external_profile: bool = False,
    flow_call_premium: float | None = None,
    flow_put_premium: float | None = None,
) -> dict:
    """End-to-end bias derivation matching JSX _computeBiasSignals semantics.

    `external_profile=True` signals that `exposures_for_chosen` carries a full
    signed net-GEX-by-strike book from the EdgelaneProvider extension (not a Tradier
    OI chain). On the intraday path we then score the WHOLE profile
    (compute_profile_score) instead of the single-put-wall magnet rule, so the
    bias reflects every support/resistance level. The legacy single-wall scorer
    is left intact for the non-EdgelaneProvider path to preserve JSX parity.
    """
    wall = find_gex_wall(greeks_raw, exposures_for_chosen)
    bilateral = find_gex_walls_bilateral(greeks_raw, exposures_for_chosen)
    put_wall = bilateral.get("put_wall")
    call_wall = bilateral.get("call_wall")

    fallback = _aggregate_exposures(exposures_for_chosen, (greeks_raw or {}).get("portfolio_totals"))
    intraday = chosen_dte is not None and chosen_dte < 1.5
    net_gex = (regime_override.get("net_gex") if (regime_override and not intraday and regime_override.get("net_gex") is not None) else fallback["net_gex"])
    net_dex = (regime_override.get("net_dex") if (regime_override and not intraday and regime_override.get("net_dex") is not None) else fallback["net_dex"])

    if intraday:
        if external_profile:
            # LOCKED 2026-06-12: magnet = biggest PUT/red bar (argmax net_gex);
            # direction = side of spot the magnet sits on, damped by green walls
            # in the path. Supersedes compute_balanced_magnet on this path.
            by_strike = _strike_rows_from(exposures_for_chosen)
            score, ext_magnet = compute_magnet_direction_score(
                spot, by_strike, put_wall, call_wall
            )
            if ext_magnet and ext_magnet.get("strike") is not None:
                wall = ext_magnet          # gex_wall ticker = red magnet, type='put'
            # DIRECTION: the provider's flow-direction signal is the flow-dollars
            # tug-of-war (call$ vs put$), NOT magnet-vs-spot. When the extension
            # supplies the premiums, that signal OVERRIDES the magnet direction;
            # the magnet remains the TARGET (gex_wall ticker) only.
            fscore, fused = compute_flow_direction_score(flow_call_premium, flow_put_premium)
            if fused:
                score = fscore
        else:
            score = compute_edgelane_provider_score(spot, put_wall, call_wall)
    else:
        score = compute_directional_score(spot, wall, net_gex, net_dex, chosen_dte)

    bias_label = score_to_bias_label(score)
    confidence = compute_confidence(score, wall, net_dex, spot, chosen_dte)
    recommended = recommend_strategies(bias_label)

    vex_wall = _extract_greek_wall(greeks_raw, exposures_for_chosen, "vex_wall", "net_vex")
    tex_wall = _extract_greek_wall(greeks_raw, exposures_for_chosen, "tex_wall", "net_tex")

    wall_side = "above" if (wall.get("strike") is not None and spot > wall["strike"]) else \
                "below" if (wall.get("strike") is not None and spot < wall["strike"]) else "at"
    gamma_regime = "positive (pinning)" if net_gex > 0 else "negative (amplifying)" if net_gex < 0 else "neutral"
    dex_skew_side = "positive (dealer-long-delta)" if net_dex > 0 else \
                    "negative (dealer-short-delta)" if net_dex < 0 else "flat"

    return {
        "spot": spot,
        "directional_score": score,
        "bias_label": bias_label,
        "confidence": confidence,
        "net_gex": net_gex,
        "dte": chosen_dte,
        "gex_wall_strike": wall.get("strike"),
        "gex_wall_strength": wall.get("strength"),
        "gex_wall_type": wall.get("type"),
        "put_wall_strike":   put_wall["strike"]   if put_wall  else None,
        "put_wall_strength": put_wall["strength"] if put_wall  else None,
        "put_wall_net_gex":  put_wall["net_gex"]  if put_wall  else None,
        "call_wall_strike":   call_wall["strike"]   if call_wall else None,
        "call_wall_strength": call_wall["strength"] if call_wall else None,
        "call_wall_net_gex":  call_wall["net_gex"]  if call_wall else None,
        "vex_wall_strike": vex_wall["strike"] if vex_wall else None,
        "vex_wall_value":  vex_wall["value"]  if vex_wall else None,
        "tex_wall_strike": tex_wall["strike"] if tex_wall else None,
        "tex_wall_value":  tex_wall["value"]  if tex_wall else None,
        "recommended_strategies": recommended,
        "_facts": {
            "wall_side": wall_side,
            "gamma_regime": gamma_regime,
            "dex_skew_side": dex_skew_side,
            "net_gex": net_gex,
            "net_dex": net_dex,
        },
    }
