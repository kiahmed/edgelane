"""Strategy candidate generation + scoring — Python port of the scoring/builder
section of spread_optimizer_v4_7_html.jsx (lines ~1600-3082).

Ports the JSX math that turns a normalized contracts chain into ranked
candidate spreads. Parity with the JSX engine is critical — all numeric
constants (composite weights, EV cap, POP tiebreaker divisor, health-badge
thresholds, limit-edge tiers, wall-strength multipliers) are preserved
EXACTLY so captured JSX outputs can be diff-tested against this module.

Public surface (functions the rest of the backend will call):

    - width_base_for_dte(dte, expected_move)
    - find_strike_by_delta(contracts, side, target_abs_delta)
    - find_closest_strike(contracts, side, target, condition=None)
    - compute_limit_premiums(candidate)
    - score_vertical(short, long, type_, dte)
    - score_condor(sp, lp, sc, lc, dte)
    - score_butterfly(low, mid, high, side, dte)
    - build_vertical(strategy, contracts, dte, expected_move, target_delta, width_factor)
    - build_iron_condor(contracts, dte, expected_move, target_delta, width_factor)
    - build_iron_butterfly(contracts, dte, expected_move, width_factor)
    - build_butterfly(strategy, contracts, dte, expected_move, width_factor)
    - generate_candidates(strategy, contracts, dte, expected_move, target_delta, width_factor, walls)
    - composite_score(c)
    - composite_verdict(score, ev)
    - pick_best_candidate(candidates)

The `walls` argument to `generate_candidates` is a dict like:
    {'strike': float, 'strength': 'low'|'medium'|'high', 'net_gex': float, 'dte': float}
matching the v4.7.59+ JSX signature.  All keys optional; missing values
fall through to the defaults that the JSX engine uses.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from .lookup_tables import STRATEGIES
from .walls import compute_wall_penalty
from .strike_profiles import pick_debit_strikes, DEBIT_STRATEGIES, StrikeProfile

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _num(v: Any) -> float | None:
    """Safe float coercion — matches the `_num` helper in dealer_exposures.py."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _get_mid(c: dict) -> float:
    """Mid-price of a contract.

    Port of `getMid` at JSX line 1600.  Honors an explicit `mid` field when
    present (chain ingest may pre-compute it); otherwise averages bid/ask.
    """
    mid = c.get("mid")
    if mid is not None:
        return float(mid)
    return (float(c.get("bid") or 0.0) + float(c.get("ask") or 0.0)) / 2.0


def _sign(x: float) -> int:
    """Manual Math.sign — Python lacks a clean equivalent and numpy.sign
    returns the wrong type on scalars."""
    return 1 if x > 0 else -1 if x < 0 else 0


# ─────────────────────────────────────────────────────────────────────────────
# Width selection + strike search
# ─────────────────────────────────────────────────────────────────────────────


def width_base_for_dte(dte: float, expected_move: float) -> float:
    """Port of `widthBaseForDTE` at JSX line 1602.

    v4.7.32: dte is fractional. Same-day (<1) keeps the 0.4× tight base.
    """
    if dte < 1:
        return 0.4 * expected_move
    if dte <= 7:
        return 1.0 * expected_move
    return 1.5 * expected_move


def find_strike_by_delta(
    contracts: Iterable[dict], side: str, target_abs_delta: float
) -> dict | None:
    """Port of `findStrikeByDelta` at JSX line 1628.

    Rejects bid<=0 because a short leg with no bid is genuinely untradeable
    even if recent volume exists.  Stricter than the chain-fetch filter on
    purpose — that one tolerates bid==0 when volume>0, but you cannot fill a
    short at any price >$0 if there is no bid.
    """
    best: dict | None = None
    best_diff = float("inf")
    for c in contracts:
        if c.get("side") != side:
            continue
        delta = c.get("delta")
        if delta is None:
            continue
        bid = float(c.get("bid") or 0.0)
        if bid <= 0:
            continue
        diff = abs(abs(float(delta)) - target_abs_delta)
        if diff < best_diff:
            best_diff = diff
            best = c
    return best


def find_closest_strike(
    contracts: Iterable[dict],
    side: str,
    target: float,
    condition: Callable[[dict], bool] | None = None,
) -> dict | None:
    """Port of `findClosestStrike` at JSX line 1639.

    Same bid<=0 rejection rationale as `find_strike_by_delta`.
    """
    best: dict | None = None
    best_diff = float("inf")
    for c in contracts:
        if c.get("side") != side:
            continue
        bid = float(c.get("bid") or 0.0)
        if bid <= 0:
            continue
        if condition is not None and not condition(c):
            continue
        strike = float(c.get("strike") or 0.0)
        diff = abs(strike - target)
        if diff < best_diff:
            best_diff = diff
            best = c
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Health / liquidity classifiers (JSX lines 1650-1693)
# ─────────────────────────────────────────────────────────────────────────────


def _classify_health(
    strategy_type: str,
    net_delta: float,
    dte: float,
    max_loss: float,
    max_profit: float,
) -> str:
    """Port of `classifyHealth` at JSX line 1650.

    Debit (bull_call / bear_put): payoff-per-debit ratio + DTE.
    Credit / condor / fly: net-delta + capital-trap guard.
    """
    if strategy_type == "debit":
        ratio = max_profit / max(0.01, max_loss)
        if dte <= 2:
            return "broken"
        if ratio < 0.30:
            return "capital_trap"
        if ratio < 0.60:
            return "thin"
        return "healthy"

    if net_delta < 0.05 and dte <= 5:
        return "broken"
    if max_loss > 5 * max_profit:
        return "capital_trap"
    if net_delta < 0.10:
        return "thin"
    if net_delta > 0.30:
        return "directional"
    return "healthy"


def _classify_liquidity(*legs: dict) -> str:
    """Port of `classifyLiquidity` at JSX line 1671 — min OI across the legs."""
    if not legs:
        return "low"
    min_oi = min(int(l.get("open_interest") or 0) for l in legs)
    if min_oi > 200:
        return "high"
    if min_oi > 50:
        return "mid"
    return "low"


def _health_explanation(
    strategy_type: str,
    h: str,
    net_delta: float,
    dte: float,
    max_loss: float,
    max_profit: float,
) -> str:
    """Port of `healthExplanation` at JSX line 1678."""
    if strategy_type == "debit":
        ratio = max_profit / max(0.01, max_loss)
        ratio_s = f"{ratio:.2f}"
        if h == "broken":
            return f"DTE {dte} too short for a debit — directional move can't play out before gamma dominates."
        if h == "capital_trap":
            return f"Payoff/debit {ratio_s} — paid too much premium relative to width; breakeven is hard to reach."
        if h == "thin":
            return f"Payoff/debit {ratio_s} — mediocre return; needs a strong directional move to justify the cost."
        return f"Payoff/debit {ratio_s} — debit reasonable for the width and DTE. Manage size by the debit paid."

    d = f"{net_delta:.3f}"
    if h == "broken":
        return f"Net Δ {d} with {dte} DTE — gamma will dominate before theta delivers."
    if h == "capital_trap":
        return f"Max loss {(max_loss / max_profit):.1f}× max profit — one loss undoes many wins."
    if h == "thin":
        return f"Net Δ {d} — only theta works. Needs days, not hours."
    if h == "directional":
        return f"Net Δ {d} — directional bet, not premium harvest."
    return f"Net Δ {d} sits in the working zone. Theta + delta both contribute."


# ─────────────────────────────────────────────────────────────────────────────
# Limit-order premium tiers (JSX line 2331)
# ─────────────────────────────────────────────────────────────────────────────

# Width-scaled limit-order tiers — preserved verbatim from JSX `LIMIT_EDGE_TIERS`
# (line 2325). pct is the fraction of total spread width to push for as edge.
LIMIT_EDGE_TIERS: list[dict] = [
    {"name": "modest", "pct": 0.0075, "hint": "often fills"},
    {"name": "balanced", "pct": 0.0150, "hint": "patient"},
    {"name": "strong", "pct": 0.0300, "hint": "on dislocations"},
]


def compute_limit_premiums(candidate: dict) -> dict | None:
    """Port of `_computeLimitPremiums` at JSX line 2331.

    Returns a dict with:
        side       : 'credit' or 'debit'
        current    : net_premium at current mid
        breakeven  : zero-EV limit premium
        tiers      : list of {name, hint, pct_of_width, target_ev, target, delta, feasible}
        target     : backward-compat scalar (modest tier's target)
        target_ev  : backward-compat scalar (modest tier's target_ev)
        delta      : backward-compat scalar (modest tier's delta)
        feasible   : backward-compat scalar (modest tier's feasibility flag)

    Returns None when the spread is unranked (width<=0 or POP at the edges).
    """
    type_ = candidate.get("type")
    pop_pct = candidate.get("pop_pct") or 0
    max_profit = candidate.get("max_profit") or 0
    max_loss = candidate.get("max_loss") or 0
    net_premium = candidate.get("net_premium")
    wall_penalty = candidate.get("wall_penalty") or {}

    pop = (pop_pct or 0) / 100.0
    factor_raw = wall_penalty.get("factor")
    factor = max(0.01, factor_raw if factor_raw is not None else 1.0)
    width = (max_profit or 0) + (max_loss or 0)
    if width <= 0 or pop <= 0 or pop >= 1:
        return None

    is_credit = type_ == "credit"
    breakeven = (1 - pop) * width if is_credit else pop * width
    current = net_premium

    tiers: list[dict] = []
    for t in LIMIT_EDGE_TIERS:
        target_ev = t["pct"] * width
        target = (
            breakeven + target_ev / factor if is_credit else breakeven - target_ev / factor
        )
        delta = (target - current) if is_credit else (current - target)
        feasible = (target < width) if is_credit else (target > 0.05)
        tiers.append({
            "name": t["name"],
            "hint": t["hint"],
            "pct_of_width": t["pct"],
            "target_ev": target_ev,
            "target": target,
            "delta": delta,
            "feasible": feasible,
        })

    return {
        "side": "credit" if is_credit else "debit",
        "current": current,
        "breakeven": breakeven,
        "tiers": tiers,
        # Backward-compat scalars — the *modest* tier is the default reference
        # for composite scoring + trade-ticket fallback. Modest is the
        # realistic patience-limit, not the aggressive strong-tier number.
        "target": tiers[0]["target"],
        "target_ev": tiers[0]["target_ev"],
        "delta": tiers[0]["delta"],
        "feasible": tiers[0]["feasible"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Score functions (JSX lines 2546-2716)
# ─────────────────────────────────────────────────────────────────────────────


def score_vertical(
    short: dict | None, long: dict | None, type_: str, dte: float
) -> dict | None:
    """Port of `scoreVertical` at JSX line 2546.

    `type_` is 'credit' or 'debit'.  Returns None if the spread doesn't
    price (net premium <= 0.01) or has zero width.
    """
    if not short or not long:
        return None
    width = abs(float(short["strike"]) - float(long["strike"]))
    if width <= 0:
        return None
    s_mid = _get_mid(short)
    l_mid = _get_mid(long)

    if type_ == "credit":
        net_premium = s_mid - l_mid
        if net_premium <= 0.01:
            return None
        max_profit = net_premium
        max_loss = width - net_premium
    else:
        net_premium = l_mid - s_mid
        if net_premium <= 0.01:
            return None
        max_profit = width - net_premium
        max_loss = net_premium

    s_delta = abs(float(short.get("delta") or 0.0))
    l_delta = abs(float(long.get("delta") or 0.0))
    net_delta = s_delta - l_delta
    net_theta = float(short.get("theta") or 0.0) - float(long.get("theta") or 0.0)

    if type_ == "credit":
        pop_pct = (1 - s_delta) * 100
    else:
        # HEURISTIC, not a real probability: blends long/short delta by how
        # much of the width the trader paid for. Directionally right but does
        # NOT model the lognormal price distribution. Documented in JSX as
        # off by 10-20% for short-DTE high-IV setups.
        frac = min(1.0, net_premium / width)
        pop_pct = max(0.0, min(100.0, (l_delta * (1 - frac) + s_delta * frac) * 100))

    ev = (pop_pct / 100) * max_profit - (1 - pop_pct / 100) * max_loss

    breakevens: list[float] = []
    if type_ == "credit" and short.get("side") == "put":
        breakevens = [float(short["strike"]) - net_premium]
    elif type_ == "credit" and short.get("side") == "call":
        breakevens = [float(short["strike"]) + net_premium]
    elif type_ == "debit" and long.get("side") == "call":
        breakevens = [float(long["strike"]) + net_premium]
    elif type_ == "debit" and long.get("side") == "put":
        breakevens = [float(long["strike"]) - net_premium]

    health = _classify_health(type_, net_delta, dte, max_loss, max_profit)
    liquidity = _classify_liquidity(short, long)
    side_char = str(short.get("side") or "")[:1].upper()

    # Strike map for downstream wall-penalty math. Tag by side so the wall
    # function can find the relevant strike regardless of credit vs debit framing.
    strikes = {
        "short_put": float(short["strike"]) if short.get("side") == "put" else None,
        "long_put": float(long["strike"]) if long.get("side") == "put" else None,
        "short_call": float(short["strike"]) if short.get("side") == "call" else None,
        "long_call": float(long["strike"]) if long.get("side") == "call" else None,
        "center": None,
    }

    if type_ == "credit":
        structure_text = f"Short {short['strike']}{side_char} / Long {long['strike']}{side_char}"
    else:
        structure_text = f"Long {long['strike']}{side_char} / Short {short['strike']}{side_char}"

    return {
        "structure_text": structure_text,
        "width": width,
        "type": type_,
        "net_premium": net_premium,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "capital_required": max_loss,
        "rr_ratio": max_profit / max_loss if max_loss else 0.0,
        "pop_pct": pop_pct,
        "ev": ev,
        "breakevens": breakevens,
        "liquidity": liquidity,
        "short_delta": s_delta,
        "long_delta": l_delta,
        "net_spread_delta": net_delta,
        "net_theta_dollar": net_theta,
        "strikes": strikes,
        "dte": dte,
        "legs": [
            {
                "strike": float(long["strike"]),
                "side": long.get("side"),
                "long_short": 1,
                "iv": (float(long.get("iv") or 0) / 100) if (long.get("iv") or 0) > 0 else None,
                "symbol": long.get("symbol"),
            },
            {
                "strike": float(short["strike"]),
                "side": short.get("side"),
                "long_short": -1,
                "iv": (float(short.get("iv") or 0) / 100) if (short.get("iv") or 0) > 0 else None,
                "symbol": short.get("symbol"),
            },
        ],
        "health": health,
        # JSX always passes 'credit' here, even for debit verticals — preserved.
        "health_explanation": _health_explanation("credit", health, net_delta, dte, max_loss, max_profit),
    }


def score_condor(
    sp: dict | None,
    lp: dict | None,
    sc: dict | None,
    lc: dict | None,
    dte: float,
) -> dict | None:
    """Port of `scoreCondor` at JSX line 2620."""
    if not sp or not lp or not sc or not lc:
        return None
    put_width = float(sp["strike"]) - float(lp["strike"])
    call_width = float(lc["strike"]) - float(sc["strike"])
    if put_width <= 0 or call_width <= 0:
        return None
    put_credit = _get_mid(sp) - _get_mid(lp)
    call_credit = _get_mid(sc) - _get_mid(lc)
    net_premium = put_credit + call_credit
    if net_premium <= 0.01:
        return None
    max_loss = max(put_width, call_width) - net_premium

    sp_d = abs(float(sp.get("delta") or 0.0))
    sc_d = abs(float(sc.get("delta") or 0.0))
    lp_d = abs(float(lp.get("delta") or 0.0))
    lc_d = abs(float(lc.get("delta") or 0.0))
    pop_pct = max(0.0, (1 - sp_d - sc_d) * 100)
    ev = (pop_pct / 100) * net_premium - (1 - pop_pct / 100) * max_loss
    put_net_d = sp_d - lp_d
    call_net_d = sc_d - lc_d
    net_delta = max(put_net_d, call_net_d)
    net_theta = (
        float(sp.get("theta") or 0.0) - float(lp.get("theta") or 0.0)
        + float(sc.get("theta") or 0.0) - float(lc.get("theta") or 0.0)
    )
    health = _classify_health("credit", net_delta, dte, max_loss, net_premium)
    liquidity = _classify_liquidity(sp, lp, sc, lc)

    strikes = {
        "short_put": float(sp["strike"]),
        "long_put": float(lp["strike"]),
        "short_call": float(sc["strike"]),
        "long_call": float(lc["strike"]),
        "center": (float(sp["strike"]) + float(sc["strike"])) / 2,
    }

    return {
        "structure_text": f"{lp['strike']}/{sp['strike']}P + {sc['strike']}/{lc['strike']}C",
        "width": max(put_width, call_width),
        "type": "credit",
        "net_premium": net_premium,
        "max_profit": net_premium,
        "max_loss": max_loss,
        "capital_required": max_loss,
        "rr_ratio": net_premium / max_loss if max_loss else 0.0,
        "pop_pct": pop_pct,
        "ev": ev,
        "breakevens": [float(sp["strike"]) - net_premium, float(sc["strike"]) + net_premium],
        "liquidity": liquidity,
        "short_delta": max(sp_d, sc_d),
        "long_delta": max(lp_d, lc_d),
        "net_spread_delta": net_delta,
        "net_theta_dollar": net_theta,
        "strikes": strikes,
        "dte": dte,
        "legs": [
            {"strike": float(lp["strike"]), "side": "put", "long_short": 1,
             "iv": (float(lp.get("iv") or 0) / 100) if (lp.get("iv") or 0) > 0 else None,
             "symbol": lp.get("symbol")},
            {"strike": float(sp["strike"]), "side": "put", "long_short": -1,
             "iv": (float(sp.get("iv") or 0) / 100) if (sp.get("iv") or 0) > 0 else None,
             "symbol": sp.get("symbol")},
            {"strike": float(sc["strike"]), "side": "call", "long_short": -1,
             "iv": (float(sc.get("iv") or 0) / 100) if (sc.get("iv") or 0) > 0 else None,
             "symbol": sc.get("symbol")},
            {"strike": float(lc["strike"]), "side": "call", "long_short": 1,
             "iv": (float(lc.get("iv") or 0) / 100) if (lc.get("iv") or 0) > 0 else None,
             "symbol": lc.get("symbol")},
        ],
        "health": health,
        "health_explanation": _health_explanation("credit", health, net_delta, dte, max_loss, net_premium),
    }


def score_butterfly(
    low: dict | None,
    mid: dict | None,
    high: dict | None,
    side: str,
    dte: float,
) -> dict | None:
    """Port of `scoreButterfly` at JSX line 2670.

    1 long low, 2 short mid, 1 long high. Requires near-symmetric wings
    (within 50% of the smaller wing's width).
    """
    if not low or not mid or not high:
        return None
    w1 = float(mid["strike"]) - float(low["strike"])
    w2 = float(high["strike"]) - float(mid["strike"])
    if abs(w1 - w2) > 0.5 * min(w1, w2):
        return None
    debit = _get_mid(low) + _get_mid(high) - 2 * _get_mid(mid)
    if debit <= 0.01:
        return None
    max_profit = min(w1, w2) - debit
    max_loss = debit
    pop_pct = 25  # butterflies inherently low POP; rough estimate
    ev = (pop_pct / 100) * max_profit - (1 - pop_pct / 100) * max_loss
    net_delta = abs(
        float(low.get("delta") or 0.0)
        - 2 * float(mid.get("delta") or 0.0)
        + float(high.get("delta") or 0.0)
    )
    net_theta = (
        float(low.get("theta") or 0.0)
        - 2 * float(mid.get("theta") or 0.0)
        + float(high.get("theta") or 0.0)
    )
    health = _classify_health("credit", net_delta, dte, max_loss, max_profit)
    liquidity = _classify_liquidity(low, mid, high)
    side_char = side[:1].upper()

    # Center is the only strike the wall penalty cares about for butterflies.
    strikes = {
        "short_put": float(mid["strike"]) if side == "put" else None,
        "long_put": None,
        "short_call": float(mid["strike"]) if side == "call" else None,
        "long_call": None,
        "center": float(mid["strike"]),
    }

    return {
        "structure_text": f"{low['strike']}/{mid['strike']}/{high['strike']} {side_char} fly",
        "width": min(w1, w2),
        "type": "debit",
        "net_premium": debit,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "capital_required": max_loss,
        "rr_ratio": max_profit / max_loss if max_loss else 0.0,
        "pop_pct": pop_pct,
        "ev": ev,
        "breakevens": [float(low["strike"]) + debit, float(high["strike"]) - debit],
        "liquidity": liquidity,
        "short_delta": abs(float(mid.get("delta") or 0.0)),
        "long_delta": (abs(float(low.get("delta") or 0.0)) + abs(float(high.get("delta") or 0.0))) / 2,
        "net_spread_delta": net_delta,
        "net_theta_dollar": net_theta,
        "strikes": strikes,
        "dte": dte,
        "legs": [
            {"strike": float(low["strike"]), "side": side, "long_short": 1,
             "iv": (float(low.get("iv") or 0) / 100) if (low.get("iv") or 0) > 0 else None,
             "symbol": low.get("symbol")},
            {"strike": float(mid["strike"]), "side": side, "long_short": -2,
             "iv": (float(mid.get("iv") or 0) / 100) if (mid.get("iv") or 0) > 0 else None,
             "symbol": mid.get("symbol")},
            {"strike": float(high["strike"]), "side": side, "long_short": 1,
             "iv": (float(high.get("iv") or 0) / 100) if (high.get("iv") or 0) > 0 else None,
             "symbol": high.get("symbol")},
        ],
        "health": health,
        # JSX line 2714 calls healthExplanation with (health, netDelta, dte, ...)
        # — first arg is the health badge, NOT a strategy_type. Looks like a
        # JSX bug; preserving the bug here for parity.  The implementation
        # falls through to the credit branch since 'healthy'/'thin'/etc. is
        # not 'debit'.
        "health_explanation": _health_explanation(health, health, net_delta, dte, max_loss, max_profit),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Builders (JSX lines 2718-2798)
# ─────────────────────────────────────────────────────────────────────────────


def build_vertical(
    strategy: str,
    contracts: list[dict],
    dte: float,
    expected_move: float,
    target_delta: float,
    width_factor: float,
    *,
    spot: float | None = None,
    walls: dict | None = None,
    profile: StrikeProfile | None = None,
    aggressiveness: int = 0,
) -> dict | None:
    """Port of `buildVertical` at JSX line 2718.

    Debit verticals (bull_call/bear_put): when a `profile` + `spot` are supplied
    and the profile is enabled, the OTM-OTM smart picker (strike_profiles.py)
    selects the strikes instead of the legacy `1 - target_delta` deep-ITM mirror.
    Falls back to the legacy path if the picker can't build. Credit spreads are
    unaffected. Defaults keep the legacy behavior so JSX-parity callers are
    unchanged."""
    base_w = width_base_for_dte(dte, expected_move)
    desired_w = base_w * width_factor

    if strategy in DEBIT_STRATEGIES and profile is not None and getattr(profile, "enabled", False) and spot:
        picked = pick_debit_strikes(
            strategy, contracts, float(spot), expected_move, walls, profile, aggressiveness
        )
        if picked:
            v = score_vertical(picked["short"], picked["long"], "debit", dte)
            if v is not None:
                v["strike_logic"] = picked["logic"]
                v["strike_source"] = "smart_picker"
            return v
        # picker couldn't build (illiquid/thin chain) -> legacy path below

    if strategy == "bull_put":
        s = find_strike_by_delta(contracts, "put", target_delta)
        if not s:
            return None
        l = find_closest_strike(
            contracts, "put", float(s["strike"]) - desired_w,
            lambda c: float(c["strike"]) < float(s["strike"]),
        )
        return score_vertical(s, l, "credit", dte)

    if strategy == "bear_call":
        s = find_strike_by_delta(contracts, "call", target_delta)
        if not s:
            return None
        l = find_closest_strike(
            contracts, "call", float(s["strike"]) + desired_w,
            lambda c: float(c["strike"]) > float(s["strike"]),
        )
        return score_vertical(s, l, "credit", dte)

    # Debit spreads: pick the long leg by delta = max(0.5, 1 - target_delta).
    # The 0.5 floor is defensive — with the slider capped at 0.40 it never
    # activates today, but it preserves "long the higher-delta leg" intent
    # if the slider ever widens beyond 0.50.
    if strategy == "bull_call":
        long = find_strike_by_delta(contracts, "call", max(0.5, 1 - target_delta))
        if not long:
            return None
        short = find_closest_strike(
            contracts, "call", float(long["strike"]) + desired_w,
            lambda c: float(c["strike"]) > float(long["strike"]),
        )
        return score_vertical(short, long, "debit", dte)

    if strategy == "bear_put":
        long = find_strike_by_delta(contracts, "put", max(0.5, 1 - target_delta))
        if not long:
            return None
        short = find_closest_strike(
            contracts, "put", float(long["strike"]) - desired_w,
            lambda c: float(c["strike"]) < float(long["strike"]),
        )
        return score_vertical(short, long, "debit", dte)

    return None


def build_iron_condor(
    contracts: list[dict],
    dte: float,
    expected_move: float,
    target_delta: float,
    width_factor: float,
) -> dict | None:
    """Port of `buildIronCondor` at JSX line 2759."""
    base_w = width_base_for_dte(dte, expected_move)
    desired_w = base_w * width_factor
    sp = find_strike_by_delta(contracts, "put", target_delta)
    sc = find_strike_by_delta(contracts, "call", target_delta)
    if not sp or not sc:
        return None
    lp = find_closest_strike(
        contracts, "put", float(sp["strike"]) - desired_w,
        lambda c: float(c["strike"]) < float(sp["strike"]),
    )
    lc = find_closest_strike(
        contracts, "call", float(sc["strike"]) + desired_w,
        lambda c: float(c["strike"]) > float(sc["strike"]),
    )
    return score_condor(sp, lp, sc, lc, dte)


def build_iron_butterfly(
    contracts: list[dict],
    dte: float,
    expected_move: float,
    width_factor: float,
) -> dict | None:
    """Port of `buildIronButterfly` at JSX line 2770 — short ATM call+put, long wings."""
    base_w = width_base_for_dte(dte, expected_move)
    desired_w = base_w * width_factor
    sp = find_strike_by_delta(contracts, "put", 0.50)
    sc = find_strike_by_delta(contracts, "call", 0.50)
    if not sp or not sc:
        return None
    lp = find_closest_strike(
        contracts, "put", float(sp["strike"]) - desired_w,
        lambda c: float(c["strike"]) < float(sp["strike"]),
    )
    lc = find_closest_strike(
        contracts, "call", float(sc["strike"]) + desired_w,
        lambda c: float(c["strike"]) > float(sc["strike"]),
    )
    return score_condor(sp, lp, sc, lc, dte)


def build_butterfly(
    strategy: str,
    contracts: list[dict],
    dte: float,
    expected_move: float,
    width_factor: float,
) -> dict | None:
    """Port of `buildButterfly` at JSX line 2782."""
    base_w = width_base_for_dte(dte, expected_move)
    desired_w = base_w * width_factor
    side = "call" if strategy == "call_butterfly" else "put"
    center = find_strike_by_delta(contracts, side, 0.50)
    if not center:
        return None
    if side == "call":
        low = find_closest_strike(
            contracts, "call", float(center["strike"]) - desired_w,
            lambda c: float(c["strike"]) < float(center["strike"]),
        )
        high = find_closest_strike(
            contracts, "call", float(center["strike"]) + desired_w,
            lambda c: float(c["strike"]) > float(center["strike"]),
        )
    else:
        low = find_closest_strike(
            contracts, "put", float(center["strike"]) - desired_w,
            lambda c: float(c["strike"]) < float(center["strike"]),
        )
        high = find_closest_strike(
            contracts, "put", float(center["strike"]) + desired_w,
            lambda c: float(c["strike"]) > float(center["strike"]),
        )
    return score_butterfly(low, center, high, side, dte)


# ─────────────────────────────────────────────────────────────────────────────
# Candidate fan-out (JSX line 2800)
# ─────────────────────────────────────────────────────────────────────────────


def generate_candidates(
    strategy: str,
    contracts: list[dict],
    dte: float,
    expected_move: float,
    target_delta: float,
    width_factor: float,
    walls: dict | None,
    spot: float | None = None,
    profile: StrikeProfile | None = None,
) -> list[dict]:
    """Port of `generateCandidates` at JSX line 2800.

    Produces 3 candidates per strategy (Conservative / Balanced / Aggressive),
    each scored, wall-adjusted, limit-priced, and composite-ranked. Returns
    a list of enriched candidate dicts ready for `pick_best_candidate`.

    `walls` may be None or a dict with optional keys:
        strike     : float
        strength   : 'low' | 'medium' | 'high'
        net_gex    : float (default 0)
        dte        : float (overrides candidate dte for wall-penalty intraday gate)
    Calls compute_wall_penalty() with the v4.7.59 signature including net_gex
    and dte.
    """
    # Debit verticals route through the smart picker when a profile+spot are
    # present; `aggr` (set per-variant below) shifts the OTM-OTM structure so the
    # Conservative/Balanced/Aggressive fan stays distinct. Credit/fly builders
    # ignore the extra kwargs.
    _aggr = 0  # rebound per-variant in the loop below (Conservative -1 .. Aggressive +1)
    builders: dict[str, Callable[[float, float], dict | None]] = {
        "bull_put":       lambda d, w: build_vertical("bull_put",  contracts, dte, expected_move, d, w),
        "bear_call":      lambda d, w: build_vertical("bear_call", contracts, dte, expected_move, d, w),
        "bull_call":      lambda d, w: build_vertical("bull_call", contracts, dte, expected_move, d, w, spot=spot, walls=walls, profile=profile, aggressiveness=_aggr),
        "bear_put":       lambda d, w: build_vertical("bear_put",  contracts, dte, expected_move, d, w, spot=spot, walls=walls, profile=profile, aggressiveness=_aggr),
        "iron_condor":    lambda d, w: build_iron_condor(contracts, dte, expected_move, d, w),
        "iron_butterfly": lambda d, w: build_iron_butterfly(contracts, dte, expected_move, w),
        "call_butterfly": lambda d, w: build_butterfly("call_butterfly", contracts, dte, expected_move, w),
        "put_butterfly":  lambda d, w: build_butterfly("put_butterfly",  contracts, dte, expected_move, w),
    }
    builder = builders.get(strategy)
    if builder is None:
        return []

    is_fly = strategy in ("iron_butterfly", "call_butterfly", "put_butterfly")
    if is_fly:
        configs = [
            {"label": "Conservative", "delta": target_delta, "width": width_factor * 1.5},
            {"label": "Balanced",     "delta": target_delta, "width": width_factor * 1.0},
            {"label": "Aggressive",   "delta": target_delta, "width": width_factor * 0.7},
        ]
    else:
        configs = [
            {"label": "Conservative", "delta": max(0.05, target_delta - 0.10), "width": width_factor},
            {"label": "Balanced",     "delta": target_delta,                    "width": width_factor},
            {"label": "Aggressive",   "delta": min(0.45, target_delta + 0.10), "width": width_factor},
        ]

    # Extract wall args once (preserving JSX `walls?.strike` / `walls?.netGex || 0` semantics)
    if walls is not None:
        w_strike = walls.get("strike")
        w_strength = walls.get("strength")
        w_net_gex = walls.get("net_gex") or 0
        w_dte = walls.get("dte")
    else:
        w_strike = None
        w_strength = None
        w_net_gex = 0
        w_dte = None

    _aggr_by_label = {"Conservative": -1, "Balanced": 0, "Aggressive": 1}
    candidates: list[dict] = []
    for cfg in configs:
        _aggr = _aggr_by_label.get(cfg["label"], 0)  # consumed by debit picker lambdas
        c = builder(cfg["delta"], cfg["width"])
        if not c:
            continue
        # Strategy-aware GEX wall penalty. Adjusts EV used for ranking; keeps raw EV for display.
        # v4.7.57: regime-aware via walls.net_gex (long-gamma multi-day = wall is floor).
        # v4.7.59: also DTE-gated — for intraday plays (DTE < 1.5), wall acts as
        # magnet regardless of regime sign because gamma concentration at
        # near-expiration overwhelms multi-day positioning.
        wall_pen = compute_wall_penalty(
            strategy,
            c["strikes"],
            {"breakeven": (c["breakevens"][0] if c["breakevens"] else None)},
            w_strike,
            w_strength,
            net_gex=w_net_gex,
            dte=w_dte,
        )
        ev_adjusted = c["ev"] * wall_pen["factor"]
        rationale = (
            f"{cfg['label']} variant — short Δ target {cfg['delta']:.2f}, "
            f"width factor {cfg['width']:.1f}×."
        )
        if wall_pen.get("reason"):
            rationale += f" {wall_pen['reason']}"

        enriched = dict(c)
        enriched["label"] = cfg["label"]
        enriched["rationale"] = rationale
        enriched["wall_penalty"] = wall_pen
        enriched["ev_adjusted"] = ev_adjusted
        enriched["limit_premiums"] = compute_limit_premiums(enriched)
        enriched["composite_score"] = composite_score(enriched)
        enriched["composite_verdict"] = composite_verdict(
            enriched["composite_score"],
            enriched.get("ev_adjusted") if enriched.get("ev_adjusted") is not None else enriched.get("ev"),
        )
        candidates.append(enriched)
    return candidates


# ─────────────────────────────────────────────────────────────────────────────
# Composite scoring (JSX lines 2959-3082)
# ─────────────────────────────────────────────────────────────────────────────

# Preserved verbatim from JSX `COMPOSITE_WEIGHTS` at line 2959. Tuning these
# constants shifts ranking priorities; the formula stays the same.
COMPOSITE_WEIGHTS: dict[str, float] = {
    "CENTER":           50,    # neutral starting point
    "EV_MAX":           40,    # cap on EV contribution
    "EV_MULT":          2,     # ev_adjusted × this = ev contribution (clipped)
    "BADGE_HEALTHY":    15,
    "BADGE_NEUTRAL":    -5,    # thin / directional
    "BADGE_DISQUAL":    -30,   # broken / capital_trap
    "LIQ_HIGH":         10,
    "LIQ_MID":          0,
    "LIQ_LOW":          -10,
    "LIMIT_NEG_EV_MAX": 30,    # weight of limit feasibility when EV is negative
    "LIMIT_POS_EV_MAX": 10,    # weight when EV is already positive
    "POP_TIEBREAK_DIV": 10,    # (pop - 50) / this = pop contribution
}

TRADEABLE_THRESHOLD: float = 60  # ≥ this = engine-recommended (green)
SKIP_THRESHOLD: float = 40       # < this = clearly do-not-trade


def composite_score(c: dict) -> float:
    """Port of `_compositeScore` at JSX line 2977.

    Aggregates every per-card signal into ONE number 0..100 (centered at 50).
    Cap on EV contribution is 40; healthy badge contributes +15, high liquidity
    +10. Limit-target feasibility contributes up to +10 (EV ≥0) or +30 (EV <0).
    POP gives a small tiebreaker via (pop_pct - 50) / 10.
    """
    W = COMPOSITE_WEIGHTS
    ev = c.get("ev_adjusted")
    if ev is None:
        ev = c.get("ev")
    if ev is None:
        ev = 0

    # EV component (clipped 0..EV_MAX; negative EV contributes 0)
    ev_score = max(0, min(W["EV_MAX"], ev * W["EV_MULT"]))

    # Badge component
    badge_map = {
        "healthy":      W["BADGE_HEALTHY"],
        "thin":         W["BADGE_NEUTRAL"],
        "directional":  W["BADGE_NEUTRAL"],
        "broken":       W["BADGE_DISQUAL"],
        "capital_trap": W["BADGE_DISQUAL"],
    }
    badge_score = badge_map.get(c.get("health"), 0)

    # Liquidity component
    liq_map = {"high": W["LIQ_HIGH"], "mid": W["LIQ_MID"], "low": W["LIQ_LOW"]}
    liq_score = liq_map.get(c.get("liquidity"), 0)

    # Limit-target feasibility
    limit_score = 0.0
    lp = c.get("limit_premiums")
    if lp and lp.get("feasible"):
        feasibility = max(0.0, min(1.0, 1 - abs(lp.get("delta") or 0) / max(0.01, lp.get("current") or 1)))
        limit_score = feasibility * (W["LIMIT_NEG_EV_MAX"] if ev < 0 else W["LIMIT_POS_EV_MAX"])

    # POP small tiebreaker
    pop_pct = c.get("pop_pct")
    if pop_pct is None:
        pop_pct = 50
    pop_score = (pop_pct - 50) / W["POP_TIEBREAK_DIV"]

    total = W["CENTER"] + ev_score + badge_score + liq_score + limit_score + pop_score
    # Match JSX's `Math.round(total * 10) / 10` then clamp 0..100.
    # JS rounds half AWAY from zero (e.g. Math.round(0.5) === 1); Python's
    # builtin round() uses banker's rounding (round(0.5) === 0). Use floor
    # +0.5 to mimic JS behavior for parity with the JSX engine.
    import math as _m
    def _r(x):
        return _m.floor(x + 0.5) if x >= 0 else -_m.floor(-x + 0.5)
    return max(0.0, min(100.0, _r(total * 10) / 10))


def composite_verdict(score: float, ev: float) -> dict:
    """Port of `_compositeVerdict` at JSX line 3011.

    Tradeable at score ≥60. Distinguishes whether you'd fill at market or
    need a GTC limit based on the sign of EV:
        ev ≥ 0  → 'tradeable now'      (current premium already an edge)
        ev < 0  → 'tradeable on limit' (score qualified via limit feasibility)
    """
    if score >= TRADEABLE_THRESHOLD:
        if ev >= 0:
            return {"label": "tradeable now", "mode": "market", "color": "emerald"}
        return {"label": "tradeable on limit", "mode": "limit", "color": "emerald"}
    if score >= SKIP_THRESHOLD:
        return {"label": "marginal", "mode": "wait", "color": "amber"}
    return {"label": "do not trade", "mode": "skip", "color": "rose"}


def pick_best_candidate(candidates: list[dict]) -> str | None:
    """Port of `pickBestCandidate` at JSX line 3071.

    Pure composite-score ranking. The composite already encodes EV (wall-
    adjusted), health badge, liquidity, limit-target feasibility, and the POP
    tiebreaker — so no separate filter step is needed. Returns the *label*
    of the winning candidate (e.g. 'Balanced'), matching JSX behavior.
    """
    if not candidates:
        return None
    sorted_c = sorted(candidates, key=lambda c: (c.get("composite_score") or 0), reverse=True)
    return sorted_c[0].get("label")


# ─────────────────────────────────────────────────────────────────────────────
# Smoke test
# ─────────────────────────────────────────────────────────────────────────────


def _synthetic_spx_0dte_chain(spot: float = 7580.0) -> list[dict]:
    """Build a small synthetic SPX 0DTE chain for the smoke test.

    Strikes from spot-50 to spot+50 in 10-point increments (11 strikes,
    both put + call → 22 contracts). Mock greeks roughly approximate the
    delta sweep across the chain.
    """
    contracts: list[dict] = []
    strikes = [spot + k * 10 for k in range(-5, 6)]
    for k in strikes:
        # Crude delta proxy: distance from spot scaled by 50
        moneyness = (k - spot) / 50.0  # roughly -1..+1
        # Call delta ranges high-near-ITM → low-far-OTM (around spot is ~0.5)
        call_delta = max(0.02, min(0.98, 0.5 - moneyness * 0.4))
        put_delta = call_delta - 1.0  # put-call parity in delta
        # Mock bid/ask — keep all >0.05 so find_strike_by_delta accepts them
        c_mid = max(0.10, 5.0 - 4.0 * moneyness if moneyness < 0 else max(0.10, 5.0 / (1 + moneyness * 3)))
        p_mid = max(0.10, 5.0 + 4.0 * moneyness if moneyness > 0 else max(0.10, 5.0 / (1 - moneyness * 3)))
        contracts.append({
            "symbol": f"SPXW_C{int(k)}",
            "strike": float(k),
            "side": "call",
            "expiration": "2026-06-04",
            "delta": call_delta,
            "gamma": 0.005,
            "theta": -0.5,
            "vega": 0.8,
            "iv": 18.0,
            "bid": max(0.05, c_mid - 0.10),
            "ask": c_mid + 0.10,
            "mid": c_mid,
            "open_interest": 500,
            "volume": 100,
        })
        contracts.append({
            "symbol": f"SPXW_P{int(k)}",
            "strike": float(k),
            "side": "put",
            "expiration": "2026-06-04",
            "delta": put_delta,
            "gamma": 0.005,
            "theta": -0.5,
            "vega": 0.8,
            "iv": 18.0,
            "bid": max(0.05, p_mid - 0.10),
            "ask": p_mid + 0.10,
            "mid": p_mid,
            "open_interest": 500,
            "volume": 100,
        })
    return contracts


if __name__ == "__main__":
    # Sanity check: chain → candidates → first candidate dict.
    chain = _synthetic_spx_0dte_chain(spot=7580.0)
    cands = generate_candidates(
        "bull_put",
        chain,
        dte=0.5,
        expected_move=30,
        target_delta=0.20,
        width_factor=1.0,
        walls=None,
    )
    print(f"generated {len(cands)} candidates for bull_put @ spot 7580 DTE 0.5")
    if cands:
        first = cands[0]
        # Pretty-print the first candidate's full dict for visibility
        import json
        print(json.dumps(first, indent=2, default=str))
        print("\n— summary —")
        print(f"  composite_score : {first.get('composite_score')}")
        print(f"  composite_verdict: {first.get('composite_verdict')}")
        print(f"  ev              : {first.get('ev'):.4f}")
        print(f"  ev_adjusted     : {first.get('ev_adjusted'):.4f}")
        print(f"  pop_pct         : {first.get('pop_pct'):.2f}")
        print(f"  health          : {first.get('health')}")
        print(f"  liquidity       : {first.get('liquidity')}")
        print(f"  wall_penalty    : {first.get('wall_penalty')}")
        print(f"  pick_best       : {pick_best_candidate(cands)}")
