"""Simmer readiness engine — pure math, no I/O, no network, no DB.

Takes a normalized option chain plus a precomputed research block (IV history,
OHLC, walls, catalysts, sentiment, index regime) and returns a readiness result
dict. The same function serves `/simmer/analyze/{symbol}` and the watcher loop,
so the on-demand and scheduled paths can never diverge — mirroring how
`engine.compute_engine_output` serves both the poller and `/snapshot`.

Shape: **hard gates, then rank.** Any gate trips → VETOED, score suppressed,
reason surfaced. No amount of score overrides a gate.

────────────────────────────────────────────────────────────────────────────
Ten corrections that are load-bearing. Each one, implemented the common way,
produces a wrong engine — so each is called out at its implementation site:

 1. POT ≈ 2·N(−d2), NOT 2·delta          → `prob_touch` / `prob_touch_approx`
 2. EV must be INTEGRATED, not binary    → `expected_value`
 3. Risk-neutral EV at mid is EXACTLY 0  → `expected_value` docstring + tests
 4. Max C/W = P(short ITM) = N(−d2)      → `credit_to_width_bound`
 5. 1 SD = 1.2533 × straddle (MAD → SD)  → `expected_move`
 6. Yang-Zhang, never Parkinson/GK       → `yang_zhang_rv`
 7. VRP gates are RATIOS, never points   → `vrp_ratio`
 8. Gate on IV percentile, display rank  → `iv_percentile` / `iv_rank`
 9. Short delta 0.20–0.35 (0.05Δ is −EV) → `GATES.short_delta_*`
10. Steep put skew HURTS a put spread    → `skew_cost`

All strike/price math is in underlying points; dollar figures multiply by
`CONTRACT_MULTIPLIER`. Times are CALENDAR years (`dte / 365`) because IV30 is a
30-*calendar*-day number — pairing it with a trading-day horizon is a
widespread silent bug.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Sequence

from . import simmer_config

CONTRACT_MULTIPLIER = 100.0
SQRT_2PI = math.sqrt(2.0 * math.pi)
# 1 SD = sqrt(pi/2) × mean absolute deviation. `straddle / S` is the MAD.
MAD_TO_SIGMA = math.sqrt(math.pi / 2.0)          # 1.25331...

STRUCTURES = ("bull_put", "bear_call", "iron_condor")
# The ONE deliberate bridge between the readiness vocabulary (`structure`) and
# the math vocabulary (`side`). Never let a structure name reach a math function
# implicitly — see `_require_side`.
STRUCTURE_SIDE = {"bull_put": "put", "bear_call": "call"}


# ─────────────────────────────────────────────────────────────────────────────
# Numeric primitives
# ─────────────────────────────────────────────────────────────────────────────
def _num(v: Any) -> float | None:
    """Safe float coercion — matches the `_num` helper in walls.py."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _require_side(side: Any, allow_both: bool = False) -> str:
    """Validate an option side. **Every function that branches on `side` calls
    this first.**

    Without it, a plain `if side == "put" else <call branch>` silently prices
    the OPPOSITE structure for any unrecognized value — it returns a
    plausible-looking number instead of failing. Passing `"bull_put"` (the
    STRUCTURE vocabulary this module's own envelope uses) yielded a C/W bound of
    0.593, the exact complement of the correct 0.407, and EVs near −2.1 instead
    of ~0. Nothing downstream could tell that apart from a real answer.

    The math layer speaks `put`/`call`; the readiness layer speaks
    `bull_put`/`bear_call`/`iron_condor`. The two are deliberately NOT aliased
    here — an implicit mapping would just relocate the ambiguity. Use the
    explicit `STRUCTURE_SIDE` table to cross the boundary."""
    allowed = {"put", "call", "both"} if allow_both else {"put", "call"}
    if side in allowed:
        return side  # type: ignore[return-value]
    hint = ""
    if isinstance(side, str) and side in STRUCTURE_SIDE:
        hint = (f" — {side!r} is a STRUCTURE name from the readiness envelope; "
                f"map it explicitly with STRUCTURE_SIDE[{side!r}] "
                f"(= {STRUCTURE_SIDE[side]!r}) before calling the math layer")
    elif side == "iron_condor":
        hint = (" — an iron condor has no single side; price its two verticals "
                "separately and combine")
    raise ValueError(f"side must be one of {sorted(allowed)}, got {side!r}{hint}")


def norm_cdf(x: float) -> float:
    """Standard normal CDF via erfc — stdlib only (no numpy/scipy in this app)."""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


# ─────────────────────────────────────────────────────────────────────────────
# Black-Scholes
# ─────────────────────────────────────────────────────────────────────────────
def d1_d2(spot: float, strike: float, sigma: float, t: float,
          r: float = 0.0, q: float = 0.0) -> tuple[float, float]:
    """d1 = [ln(S/K) + (r − q + σ²/2)T] / (σ√T);  d2 = d1 − σ√T."""
    if spot <= 0 or strike <= 0 or sigma <= 0 or t <= 0:
        # Degenerate: treat as a step function at the forward.
        fwd = spot * math.exp((r - q) * max(t, 0.0))
        big = 40.0 if fwd > strike else -40.0
        return big, big
    v = sigma * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t) / v
    return d1, d1 - v


def bs_put(spot: float, strike: float, sigma: float, t: float,
           r: float = 0.0, q: float = 0.0) -> float:
    d1, d2 = d1_d2(spot, strike, sigma, t, r, q)
    return strike * math.exp(-r * t) * norm_cdf(-d2) - spot * math.exp(-q * t) * norm_cdf(-d1)


def bs_call(spot: float, strike: float, sigma: float, t: float,
            r: float = 0.0, q: float = 0.0) -> float:
    d1, d2 = d1_d2(spot, strike, sigma, t, r, q)
    return spot * math.exp(-q * t) * norm_cdf(d1) - strike * math.exp(-r * t) * norm_cdf(d2)


def bs_vega(spot: float, strike: float, sigma: float, t: float,
            r: float = 0.0, q: float = 0.0) -> float:
    """∂price/∂σ for a 1.00 (=100 vol-point) change in vol."""
    if t <= 0 or sigma <= 0:
        return 0.0
    d1, _ = d1_d2(spot, strike, sigma, t, r, q)
    return spot * math.exp(-q * t) * norm_pdf(d1) * math.sqrt(t)


def bs_delta(spot: float, strike: float, sigma: float, t: float, side: str,
             r: float = 0.0, q: float = 0.0) -> float:
    _require_side(side)
    d1, _ = d1_d2(spot, strike, sigma, t, r, q)
    disc = math.exp(-q * t)
    return disc * norm_cdf(d1) if side == "call" else -disc * norm_cdf(-d1)


# ─────────────────────────────────────────────────────────────────────────────
# Probability — the three corrections everyone gets wrong
# ─────────────────────────────────────────────────────────────────────────────
def prob_itm(spot: float, strike: float, sigma: float, t: float, side: str,
             r: float = 0.0, q: float = 0.0) -> float:
    """P(option finishes ITM) = N(−d2) for a put, N(d2) for a call.

    **Delta ≠ P(ITM).** N(−d2) > N(−d1) always, and the gap scales with σ√T.
    Never substitute delta here."""
    _require_side(side)
    _, d2 = d1_d2(spot, strike, sigma, t, r, q)
    return norm_cdf(-d2) if side == "put" else norm_cdf(d2)


def prob_below(spot: float, level: float, sigma: float, t: float,
               r: float = 0.0, q: float = 0.0) -> float:
    """P(S_T ≤ level) under the lognormal with drift (r − q) and vol σ."""
    _, d2 = d1_d2(spot, level, sigma, t, r, q)
    return norm_cdf(-d2)


def prob_touch(spot: float, barrier: float, sigma: float, t: float,
               r: float = 0.0, q: float = 0.0) -> float:
    """Exact first-passage probability for GBM — P(barrier touched before T).

    ν = r − q − σ²/2, b = ln(B/S), s = σ√T:

        down: POT = N((b − νT)/s) + e^(2νb/σ²)·N((b + νT)/s)
        up:   POT = N((−b + νT)/s) + e^(2νb/σ²)·N((−b − νT)/s)

    Measured across IV/DTE/strike, POT/N(−d2) lands in 1.81–1.99 — so the
    `2 × N(−d2)` shorthand is a good approximation. POT/|delta| ranges
    2.05–2.90: **the folk rule `POT ≈ 2 × delta` understates touch risk by
    5–45%, worst exactly where it matters** (high IV, longer DTE)."""
    if spot <= 0 or barrier <= 0 or t <= 0:
        return 0.0
    if sigma <= 0:
        fwd = spot * math.exp((r - q) * t)
        return 1.0 if (barrier <= max(spot, fwd) and barrier >= min(spot, fwd)) else 0.0
    nu = r - q - 0.5 * sigma * sigma
    b = math.log(barrier / spot)
    s = sigma * math.sqrt(t)
    expo = max(-700.0, min(700.0, 2.0 * nu * b / (sigma * sigma)))
    scale = math.exp(expo)
    if barrier < spot:                                   # down barrier
        p = norm_cdf((b - nu * t) / s) + scale * norm_cdf((b + nu * t) / s)
    else:                                                # up barrier
        p = norm_cdf((-b + nu * t) / s) + scale * norm_cdf((-b - nu * t) / s)
    return _clamp01(p)


def prob_touch_approx(spot: float, barrier: float, sigma: float, t: float,
                      r: float = 0.0, q: float = 0.0) -> float:
    """The DEFENSIBLE shorthand: POT ≈ 2 × N(−d2). Use `prob_touch` when you can
    — this exists so callers that only have N(−d2) don't reach for 2 × delta."""
    side = "put" if barrier < spot else "call"
    return _clamp01(2.0 * prob_itm(spot, barrier, sigma, t, side, r, q))


# ─────────────────────────────────────────────────────────────────────────────
# Realized volatility
# ─────────────────────────────────────────────────────────────────────────────
def _bars(rows: Iterable[Any]) -> list[dict]:
    """Normalize OHLC rows: dicts with o/h/l/c (any of open/high/low/close) or
    4-tuples in OHLC order. Rows missing any leg are dropped."""
    out: list[dict] = []
    for row in rows or []:
        if isinstance(row, dict):
            o = _num(row.get("open", row.get("o")))
            h = _num(row.get("high", row.get("h")))
            lo = _num(row.get("low", row.get("l")))
            c = _num(row.get("close", row.get("c")))
        elif isinstance(row, (list, tuple)) and len(row) >= 4:
            o, h, lo, c = (_num(row[0]), _num(row[1]), _num(row[2]), _num(row[3]))
        else:
            continue
        if None in (o, h, lo, c) or min(o, h, lo, c) <= 0:  # type: ignore[arg-type]
            continue
        out.append({"open": o, "high": h, "low": lo, "close": c})
    return out


def yang_zhang_rv(rows: Iterable[Any], annualization: int = 252) -> float | None:
    """Annualized Yang-Zhang realized volatility.

        σ²_YZ = σ²_overnight + k·σ²_open_to_close + (1 − k)·σ²_RogersSatchell
        k     = 0.34 / (1.34 + (n + 1)/(n − 1))

    **This single choice determines whether the VRP gate works at all.**
    Simulated with realistic overnight gaps (35% of variance overnight) at a
    true vol of 0.300: close-to-close measured 0.296, Yang-Zhang 0.292, but
    Parkinson 0.234 and Garman-Klass 0.231 — **~23% biased LOW**, because they
    only see the intraday range and miss overnight gaps entirely. On a name at
    true IV/RV = 1.10 they report 1.41–1.43 and sail straight through an
    `IV/RV > 1.20` gate: a universal false positive.

    Needs the previous close for the overnight leg, so `n` usable periods come
    from `n + 1` bars. Returns None below 3 usable periods (k is undefined at
    n = 1)."""
    bars = _bars(rows)
    n = len(bars) - 1
    if n < 3:
        return None

    overnight: list[float] = []   # ln(O_i / C_{i−1})   — the gap Parkinson/GK miss
    open_close: list[float] = []  # ln(C_i / O_i)
    rs = 0.0                      # Rogers-Satchell — drift-independent
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1], bars[i]
        overnight.append(math.log(cur["open"] / prev["close"]))
        open_close.append(math.log(cur["close"] / cur["open"]))
        hc = math.log(cur["high"] / cur["close"])
        ho = math.log(cur["high"] / cur["open"])
        lc = math.log(cur["low"] / cur["close"])
        lo = math.log(cur["low"] / cur["open"])
        rs += hc * ho + lc * lo

    mean_o = sum(overnight) / n
    mean_c = sum(open_close) / n
    var_o = sum((x - mean_o) ** 2 for x in overnight) / (n - 1)
    var_c = sum((x - mean_c) ** 2 for x in open_close) / (n - 1)
    var_rs = rs / n

    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    var = var_o + k * var_c + (1.0 - k) * var_rs
    if var <= 0:
        return None
    return math.sqrt(var) * math.sqrt(annualization)


def close_to_close_rv(rows: Iterable[Any], annualization: int = 252,
                      zero_mean: bool = True) -> float | None:
    """Annualized close-to-close RV. Unbiased but ~5× noisier than Yang-Zhang.

    Kept as the mandatory **cross-check**: if Yang-Zhang shows edge and
    zero-mean close-to-close does not, the "edge" is an overnight-exclusion
    artifact. Require agreement (`rv_agreement`)."""
    bars = _bars(rows)
    closes = [b["close"] for b in bars]
    if len(closes) < 3:
        # Accept a bare list of closes too.
        closes = [c for c in (_num(x) for x in rows or []) if c and c > 0]
    if len(closes) < 3:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    n = len(rets)
    if zero_mean:
        var = sum(x * x for x in rets) / n
    else:
        m = sum(rets) / n
        var = sum((x - m) ** 2 for x in rets) / (n - 1)
    if var <= 0:
        return None
    return math.sqrt(var) * math.sqrt(annualization)


def parkinson_rv(rows: Iterable[Any], annualization: int = 252) -> float | None:
    """⚠ **Reference implementation only — never gate on this.** Parkinson (and
    Garman-Klass) run ~23% biased LOW in the presence of overnight gaps, which
    manufactures VRP that isn't there. Provided so the bias is testable and
    visible rather than rediscovered in production."""
    bars = _bars(rows)
    if len(bars) < 2:
        return None
    c = 1.0 / (4.0 * math.log(2.0))
    var = c * sum(math.log(b["high"] / b["low"]) ** 2 for b in bars) / len(bars)
    if var <= 0:
        return None
    return math.sqrt(var) * math.sqrt(annualization)


def rv_agreement(yz: float | None, cc: float | None, tolerance: float) -> bool:
    """True when the two estimators agree within `tolerance` (relative)."""
    if yz is None or cc is None or yz <= 0 or cc <= 0:
        return False
    return abs(yz - cc) / max(yz, cc) <= tolerance


# ─────────────────────────────────────────────────────────────────────────────
# Implied-volatility statistics
# ─────────────────────────────────────────────────────────────────────────────
def iv_rank(iv: float, low_52w: float, high_52w: float) -> float | None:
    """IV Rank = (IV − IV_min_52w) / (IV_max_52w − IV_min_52w) × 100.

    A **two-point statistic**: one vol spike pins the denominator for a full
    year, and its mean drifts down as the lookback grows because the max only
    ratchets up (the documented post-COVID SPY pathology). **Display this, gate
    on `iv_percentile`.**"""
    iv, low_52w, high_52w = (_num(iv), _num(low_52w), _num(high_52w))
    if None in (iv, low_52w, high_52w) or high_52w <= low_52w:  # type: ignore[operator]
        return None
    return _clamp01((iv - low_52w) / (high_52w - low_52w)) * 100.0  # type: ignore[operator]


def iv_percentile(iv: float, history: Sequence[float]) -> float | None:
    """IV Percentile = % of the last N trading days with IV **below** today.

    An order statistic over all N points; it self-centres near 50 at any
    lookback. This is the gate."""
    iv = _num(iv)
    vals = [v for v in (_num(x) for x in history or []) if v is not None]
    if iv is None or not vals:
        return None
    return 100.0 * sum(1 for v in vals if v < iv) / len(vals)


def interp_total_variance(iv_near: float, t_near: float,
                          iv_far: float, t_far: float, t: float) -> float | None:
    """Interpolate IV to tenor `t` **in total variance**, not in IV.

    Near 0.350 @ 23d with far 0.280 @ 37d gives 0.3150 IV-linear but 0.3087
    variance-linear — a 0.63 vol-point bias applied every single day. Cboe's own
    VIX methodology interpolates variance."""
    iv_near, t_near, iv_far, t_far, t = (_num(iv_near), _num(t_near),
                                         _num(iv_far), _num(t_far), _num(t))
    if None in (iv_near, t_near, iv_far, t_far, t) or t_far <= t_near:  # type: ignore[operator]
        return None
    w_near = iv_near * iv_near * t_near      # type: ignore[operator]
    w_far = iv_far * iv_far * t_far          # type: ignore[operator]
    frac = (t - t_near) / (t_far - t_near)   # type: ignore[operator]
    w = w_near + frac * (w_far - w_near)
    if w <= 0 or t <= 0:                     # type: ignore[operator]
        return None
    return math.sqrt(w / t)                  # type: ignore[operator]


def vrp_ratio(iv: float | None, rv: float | None) -> float | None:
    """IV / RV — a **ratio**, never vol points.

    EV as a fraction of max risk is nearly invariant across IV *level* at
    constant relative VRP (20%–80% IV land within ~0.7pp), which is exactly why
    an absolute "IV − HV > 5 points" rule is economically meaningless: it passes
    every high-IV name and rejects every low-IV one."""
    iv, rv = _num(iv), _num(rv)
    if iv is None or rv is None or rv <= 0:
        return None
    return iv / rv


def cross_sectional_percentile(value: float, peers: Sequence[float]) -> float | None:
    """Rank `value` against today's peer cross-section — **zero history needed**.

    Ranking IV30/RV20 across the watchlist performs on par with 252-day IVR
    (Q5−Q1 spread 0.396 vs 0.392) and blending the two beats either alone by
    ~10%. This is what removes the day-one IV-history blocker."""
    value = _num(value)
    vals = [v for v in (_num(x) for x in peers or []) if v is not None]
    if value is None or not vals:
        return None
    return 100.0 * sum(1 for v in vals if v < value) / len(vals)


# ─────────────────────────────────────────────────────────────────────────────
# Term structure + earnings VRP decomposition
# ─────────────────────────────────────────────────────────────────────────────
# `*_pp` fields are expressed in VOL POINTS (×100 of decimal IV): a 0.02 IV
# difference reads as 2.0 pp. Chosen so "vol points" is literal; the sign-based
# gates below are invariant to the scaling.
_PP = 100.0


def term_structure(iv_near: float | None, iv_far: float | None) -> dict[str, Any]:
    """Single-name IV term structure from two ATM IVs.

    `term_slope = iv_far / iv_near` — **> 1 is contango (upward-sloping), which
    favours the premium seller**; < 1 is backwardation (front richer than back,
    the market pricing near-term stress). `term_slope_pp = iv_near − iv_far` in
    vol points, positive when the front is the richer leg."""
    iv_near, iv_far = _num(iv_near), _num(iv_far)
    if iv_near is None or iv_far is None or iv_near <= 0 or iv_far <= 0:
        return {"term_slope": None, "term_slope_pp": None}
    return {"term_slope": iv_far / iv_near,
            "term_slope_pp": (iv_near - iv_far) * _PP}


def earnings_vrp(iv_near: float | None, t_near: float | None,
                 iv_far: float | None, t_far: float | None,
                 rv: float | None, event_in_near: bool) -> dict[str, Any]:
    """Decompose the volatility risk premium into an earnings-driven component
    and an ex-earnings (baseline-regime) component — VolRadar's key idea: it
    exposes when apparent premium is ENTIRELY the pending catalyst.

        vrp_total_pp       = iv_near − rv                       (a second lens on
                             VRP, in vol points, alongside the IV/RV ratio)
        σ_event²           = max(0, iv_near² − iv_far²) · T_near   (event only in
                             the near tenor — the far tenor is the clean baseline)
        iv_near_exearn     = sqrt(max(0, iv_near² − σ_event²/T_near))
        vrp_earnings_pp    = iv_near − iv_near_exearn
        vrp_ex_earnings_pp = iv_near_exearn − rv

    When no earnings sits in the near/far window, `vrp_earnings_pp = 0` and
    `vrp_ex_earnings_pp = vrp_total_pp`.

    The actionable read is the sign of `vrp_ex_earnings_pp`: a positive
    `vrp_total_pp` with a NEGATIVE `vrp_ex_earnings_pp` means the premium is
    positive ONLY because of the event — the baseline vol regime does not
    support selling. `evaluate_readiness` turns that into a soft `avoid_if`
    (the catalyst gate already hard-vetoes earnings actually inside the trade
    tenor; this catches earnings sitting just outside it while still inflating
    the front IV)."""
    iv_near, iv_far = _num(iv_near), _num(iv_far)
    t_near, rv = _num(t_near), _num(rv)
    if iv_near is None or rv is None or rv <= 0:
        return {"vrp_total_pp": None, "vrp_earnings_pp": None,
                "vrp_ex_earnings_pp": None, "iv_near_exearn": None}
    vrp_total_pp = (iv_near - rv) * _PP
    if not (event_in_near and iv_far is not None and iv_far > 0
            and t_near is not None and t_near > 0):
        # No event in the window (or no clean far leg): the entire VRP is
        # ex-earnings by construction.
        return {"vrp_total_pp": vrp_total_pp, "vrp_earnings_pp": 0.0,
                "vrp_ex_earnings_pp": vrp_total_pp, "iv_near_exearn": iv_near}
    sigma_event_sq = max(0.0, iv_near * iv_near - iv_far * iv_far) * t_near
    iv_near_exearn = math.sqrt(max(0.0, iv_near * iv_near - sigma_event_sq / t_near))
    return {
        "vrp_total_pp": vrp_total_pp,
        "vrp_earnings_pp": (iv_near - iv_near_exearn) * _PP,
        "vrp_ex_earnings_pp": (iv_near_exearn - rv) * _PP,
        "iv_near_exearn": iv_near_exearn,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Chain helpers
# ─────────────────────────────────────────────────────────────────────────────
def _side(row: dict) -> str | None:
    s = str(row.get("side") or row.get("option_type") or "").lower()
    return "call" if s.startswith("c") else "put" if s.startswith("p") else None


def row_mid(row: dict) -> float | None:
    m = _num(row.get("mid"))
    if m is not None:
        return m
    bid, ask = _num(row.get("bid")), _num(row.get("ask"))
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2.0


def row_spread(row: dict) -> float | None:
    bid, ask = _num(row.get("bid")), _num(row.get("ask"))
    if bid is None or ask is None:
        return None
    return ask - bid


def chain_row(chain: Sequence[dict], side: str, strike: float) -> dict | None:
    """Exact-strike lookup (tolerant of float noise)."""
    _require_side(side)
    for row in chain or []:
        if _side(row) == side and abs((_num(row.get("strike")) or -1e18) - strike) < 1e-6:
            return row
    return None


def strikes_for(chain: Sequence[dict], side: str) -> list[float]:
    _require_side(side)
    out = sorted({_num(r.get("strike")) for r in chain or [] if _side(r) == side} - {None})
    return [s for s in out if s is not None]


def atm_strike(chain: Sequence[dict], spot: float) -> float | None:
    both = set(strikes_for(chain, "put")) & set(strikes_for(chain, "call"))
    if not both:
        both = set(strikes_for(chain, "put")) | set(strikes_for(chain, "call"))
    return min(both, key=lambda k: abs(k - spot)) if both else None


def atm_iv(chain: Sequence[dict], spot: float) -> float | None:
    """ATM IV = mean of the put and call IV at the nearest listed strike."""
    k = atm_strike(chain, spot)
    if k is None:
        return None
    ivs = [_num((chain_row(chain, s, k) or {}).get("iv")) for s in ("put", "call")]
    ivs = [v for v in ivs if v and v > 0]
    return sum(ivs) / len(ivs) if ivs else None


def atm_straddle_mid(chain: Sequence[dict], spot: float) -> float | None:
    k = atm_strike(chain, spot)
    if k is None:
        return None
    p, c = chain_row(chain, "put", k), chain_row(chain, "call", k)
    pm, cm = (row_mid(p or {}), row_mid(c or {}))
    return None if pm is None or cm is None else pm + cm


def iv_at_strike(chain: Sequence[dict], side: str, strike: float) -> float | None:
    """IV of `side` at `strike`, linearly interpolating total variance between
    the bracketing listed strikes (flat extrapolation beyond the wings). Used to
    price the breakeven, which almost never lands on a listed strike."""
    _require_side(side)
    pts = sorted((k, _num((chain_row(chain, side, k) or {}).get("iv")))
                 for k in strikes_for(chain, side))
    pts = [(k, v) for k, v in pts if v and v > 0]
    if not pts:
        return None
    if strike <= pts[0][0]:
        return pts[0][1]
    if strike >= pts[-1][0]:
        return pts[-1][1]
    for (k0, v0), (k1, v1) in zip(pts, pts[1:]):
        if k0 <= strike <= k1:
            if k1 == k0:
                return v0
            f = (strike - k0) / (k1 - k0)
            w = (v0 * v0) + f * ((v1 * v1) - (v0 * v0))   # variance-linear
            return math.sqrt(w) if w > 0 else v0
    return pts[-1][1]


def row_abs_delta(row: dict, spot: float, t: float, side: str,
                  r: float = 0.0, q: float = 0.0) -> float | None:
    """|delta| off the chain when present, else from Black-Scholes on the row's
    own IV. Never used as a probability — see `prob_itm`."""
    _require_side(side)
    d = _num(row.get("delta"))
    if d is not None:
        return abs(d)
    iv, k = _num(row.get("iv")), _num(row.get("strike"))
    if iv is None or k is None or iv <= 0:
        return None
    return abs(bs_delta(spot, k, iv, t, side, r, q))


def strike_by_abs_delta(chain: Sequence[dict], side: str, spot: float, t: float,
                        target: float, r: float = 0.0, q: float = 0.0) -> float | None:
    """Listed strike whose |delta| is closest to `target`."""
    _require_side(side)
    best, best_err = None, float("inf")
    for k in strikes_for(chain, side):
        row = chain_row(chain, side, k)
        if not row:
            continue
        d = row_abs_delta(row, spot, t, side, r, q)
        if d is None:
            continue
        err = abs(d - target)
        if err < best_err:
            best, best_err = k, err
    return best


def rr25_put_minus_call(chain: Sequence[dict], spot: float, t: float,
                        r: float = 0.0, q: float = 0.0) -> float | None:
    """`RR25 = IV(25Δ put) − IV(25Δ call)` — **equity convention, positive =
    put bias**. The field is named for the convention on purpose: FX uses the
    opposite sign, and a silent sign flip inverts every downstream gate."""
    kp = strike_by_abs_delta(chain, "put", spot, t, 0.25, r, q)
    kc = strike_by_abs_delta(chain, "call", spot, t, 0.25, r, q)
    if kp is None or kc is None:
        return None
    ivp = _num((chain_row(chain, "put", kp) or {}).get("iv"))
    ivc = _num((chain_row(chain, "call", kc) or {}).get("iv"))
    if ivp is None or ivc is None:
        return None
    return ivp - ivc


def max_pain(chain: Sequence[dict], spot: float | None = None) -> float | None:
    """Max-pain strike: the settlement price that minimizes total option-holder
    payoff (equivalently, maximizes writer retention) across the listed chain.

    For a candidate settle `K`, in-the-money value is
    `Σ call_OI·(K − k)` over calls with `k < K` plus `Σ put_OI·(k − K)` over
    puts with `k > K`; max pain is the `K` that minimizes it. Pure function over
    the open interest already in the chain — a cheap corroboration alongside the
    GEX walls, never a gate. Ties break toward `spot` when supplied, else the
    lower strike (deterministic)."""
    call_oi: dict[float, float] = {}
    put_oi: dict[float, float] = {}
    for row in chain or []:
        side = _side(row)
        k = _num(row.get("strike"))
        if side is None or k is None:
            continue
        oi = _num(row.get("open_interest")) or 0.0
        (call_oi if side == "call" else put_oi)[k] = \
            (call_oi if side == "call" else put_oi).get(k, 0.0) + oi
    strikes = sorted(set(call_oi) | set(put_oi))
    if not strikes or (sum(call_oi.values()) + sum(put_oi.values())) <= 0:
        return None
    best_k: float | None = None
    best_pain = float("inf")
    for cand in strikes:
        pain = 0.0
        for k, oi in call_oi.items():
            if k < cand:
                pain += oi * (cand - k)
        for k, oi in put_oi.items():
            if k > cand:
                pain += oi * (k - cand)
        if pain < best_pain - 1e-9 or (
                abs(pain - best_pain) <= 1e-9 and best_k is not None
                and spot is not None and abs(cand - spot) < abs(best_k - spot)):
            best_pain, best_k = pain, cand
    return best_k


# ─────────────────────────────────────────────────────────────────────────────
# Expected move
# ─────────────────────────────────────────────────────────────────────────────
def expected_move(spot: float, atm_vol: float | None, dte: float,
                  straddle_mid: float | None = None,
                  days_per_year: float = 365.0) -> dict[str, Any]:
    """Two standard methods; compute both and take the **wider** (conservative).

        EM_sigma    = S · σ_atm · sqrt(DTE / 365)
        EM_straddle = 1.2533 × ATM_straddle_mid

    ⚠ The common shortcut is wrong and the error is ~20%. `straddle / S` is the
    **mean absolute deviation**, not one standard deviation: for a Gaussian
    E|X| = σ·sqrt(2/π) = 0.7979σ, so 1 SD = 1.2533 × (straddle/S). Most
    published "implied move" figures are the raw ATM straddle — i.e. the MAD,
    ~20% *below* 1 SD — and using it as a 1-SD band places short strikes
    systematically too close. (The widely-quoted `0.85 × straddle` has no
    derivation behind it at all.)

    The same error poisons the frequency claim: "price stays inside the implied
    move ~70% of the time" is quoted against the wrong null — the Gaussian
    baseline for a 1-SD band is 68.3%, and for a `straddle/S` band only 57.5%."""
    spot, atm_vol, dte = (_num(spot), _num(atm_vol), _num(dte))
    t = max(0.0, (dte or 0.0)) / days_per_year
    em_sigma = (spot * atm_vol * math.sqrt(t)) if (spot and atm_vol and t > 0) else None
    sm = _num(straddle_mid)
    em_straddle = MAD_TO_SIGMA * sm if sm is not None and sm > 0 else None
    candidates = [x for x in (em_sigma, em_straddle) if x is not None]
    em1 = max(candidates) if candidates else None
    return {
        "em_sigma": em_sigma,
        "em_straddle": em_straddle,
        "em_straddle_raw_mad": sm,     # what most vendors publish — NOT 1 SD
        "em_1sd": em1,                 # the wider of the two
        "em_2sd": (em1 * 2.0) if em1 is not None else None,
        "method": ("straddle" if em1 is not None and em1 == em_straddle else "sigma"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Credit-spread math
# ─────────────────────────────────────────────────────────────────────────────
def breakeven(k_short: float, credit: float, side: str) -> float:
    """`BE = K_short − credit` for a put spread, `K_short + credit` for a call
    spread. **POP is evaluated HERE, never at the short strike.**"""
    _require_side(side)
    return k_short - credit if side == "put" else k_short + credit


def pop_at_breakeven(spot: float, k_short: float, credit: float, sigma: float,
                     t: float, side: str, r: float = 0.0, q: float = 0.0) -> float:
    """POP = P(S_T beyond the breakeven) = N(d2) with K := BE.

    Evaluating at the short strike instead systematically understates POP by the
    whole credit's worth of cushion."""
    _require_side(side)
    be = breakeven(k_short, credit, side)
    if be <= 0:
        return 1.0
    _, d2 = d1_d2(spot, be, sigma, t, r, q)
    return norm_cdf(d2) if side == "put" else norm_cdf(-d2)


def credit_to_width_bound(spot: float, k_short: float, sigma: float, t: float,
                          side: str, r: float = 0.0, q: float = 0.0) -> float:
    """**Maximum achievable C/W = P(short strike finishes ITM) = N(−d2).**

    Because `C(W) = ∫₀^W N(−d2)(K_short − u) du`, C/W is the *mean* of N(−d2)
    over the spread's strike interval and is therefore strictly below N(−d2) at
    the short strike, approaching it only as W → 0.

    Consequence: **"collect ⅓ of the width" and "sell the 16-delta strike" are
    mathematically incompatible** — a 16Δ short caps out at C/W ≈ 0.19 at 30%
    IV, at *any* width. C/W is a DEPENDENT variable: fix the short delta, target
    C/W, solve for width. Do not gate on the two independently."""
    _require_side(side)
    return prob_itm(spot, k_short, sigma, t, side, r, q)


def theoretical_credit(spot: float, k_short: float, k_long: float, t: float,
                       side: str, sigma_at: Callable[[float], float] | float,
                       r: float = 0.0, q: float = 0.0) -> float:
    """Model credit of the vertical: price(short leg) − price(long leg), each
    leg at **its own** vol. `sigma_at` is a flat vol or a callable K → σ, which
    is what lets `skew_cost` reprice the identical structure at flat ATM vol.

    Worth stating precisely, because it is the derivation behind
    `credit_to_width_bound`: at FLAT vol,

        C(W) = ∫₀^W e^(−rT)·N(−d2)(K_short − u) du

    since ∂Put/∂K = e^(−rT)·N(−d2). That is what makes C/W the *mean* of N(−d2)
    over the strike interval, hence strictly below N(−d2) at the short strike.
    **With a smile that identity breaks** — the total derivative along the
    surface picks up a vega·dσ/dK term — so the legs are priced directly here
    rather than integrated, and the bound is quoted at the short strike's own
    vol."""
    _require_side(side)
    vol_fn = sigma_at if callable(sigma_at) else (lambda _k, _s=float(sigma_at): _s)
    if abs(k_short - k_long) <= 0:
        return 0.0
    price = bs_put if side == "put" else bs_call
    s_vol, l_vol = vol_fn(k_short), vol_fn(k_long)
    if not s_vol or not l_vol or s_vol <= 0 or l_vol <= 0:
        return 0.0
    return price(spot, k_short, s_vol, t, r, q) - price(spot, k_long, l_vol, t, r, q)


def solve_width_for_target_cw(spot: float, k_short: float, t: float, side: str,
                              sigma_at: Callable[[float], float] | float,
                              target_cw: float, max_width: float,
                              r: float = 0.0, q: float = 0.0,
                              tol: float = 1e-4) -> float | None:
    """Width whose model C/W equals `target_cw`, or None if infeasible.

    C/W falls monotonically in W (it is the running mean of a monotone N(−d2)),
    so a bisection is exact. Infeasible whenever `target_cw` ≥ the N(−d2) bound
    at the short strike — which is precisely the "⅓ width at 16 delta" case."""
    _require_side(side)
    sig0 = sigma_at(k_short) if callable(sigma_at) else float(sigma_at)
    bound = credit_to_width_bound(spot, k_short, sig0, t, side, r, q)
    if target_cw <= 0 or target_cw >= bound:
        return None

    def cw(w: float) -> float:
        k_long = k_short - w if side == "put" else k_short + w
        return theoretical_credit(spot, k_short, k_long, t, side, sigma_at, r, q) / w

    lo, hi = 1e-6, max(1e-5, max_width)
    if cw(hi) > target_cw:            # even the widest spread pays more than target
        return hi
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if cw(mid) > target_cw:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


def expected_value(spot: float, k_short: float, k_long: float, credit: float,
                   sigma_forecast: float, t: float, side: str,
                   r: float = 0.0, q: float = 0.0, step: float = 0.01,
                   max_steps: int = 200_000) -> float:
    """EV of the credit spread, per share, in present-value terms:

        EV = credit − e^(−rT) · E[loss(S_T)]

    with the loss expectation taken under a lognormal whose **drift is
    risk-neutral (r − q) but whose vol is our FORECAST**, not the chain's.

    Two things this function exists to enforce:

    **(a) The naive binary formula must not be used.**
    `EV = POP × credit − (1 − POP) × maxloss` returns −0.20, −0.21, −0.13 on
    spreads whose true EV is 0.00, because it treats everything below breakeven
    as a *full* max loss and ignores the partial-loss zone between the breakeven
    and the long strike. Here the payoff is integrated in $0.01 increments
    across the full profit/loss range (the constant tails are handled in closed
    form, so the integration is exact to bucket-midpoint error).

    **(b) The invariant.** Under the risk-neutral measure, using the chain's own
    IV, the exact EV of a credit spread priced at mid is **EXACTLY ZERO** —
    because `e^(−rT)·E^Q[loss] = put(K_short) − put(K_long)`, which *is* the
    mid. So passing `sigma_forecast = chain IV` and `credit = model mid` must
    return 0. `tests/simmer/test_engine.py` asserts it; the assertion catches
    whole classes of pricing error.

    All edge therefore comes from the gap between implied vol and *subsequent
    realized* vol. That is why this signature takes a separate forecast vol at
    all: price probabilities off the chain's IV and EV is zero by construction."""
    _require_side(side)
    lo, hi = (min(k_short, k_long), max(k_short, k_long))
    width = hi - lo
    if width <= 0:
        return credit
    step = max(step, width / max_steps)
    n = max(1, int(round(width / step)))
    h = width / n
    disc = math.exp(-r * t)

    def below(x: float) -> float:
        return prob_below(spot, x, sigma_forecast, t, r, q)

    # Constant tail: max loss beyond the long strike.
    e_loss = width * (below(lo) if side == "put" else (1.0 - below(hi)))
    # Partial-loss zone, integrated in $0.01 buckets. The payoff is LINEAR
    # inside a bucket, so bucket-mass × midpoint-payoff is exact to ~1e-10.
    prev = below(lo)
    for i in range(n):
        a = lo + i * h
        b = a + h
        cur = below(b)
        mass = cur - prev
        prev = cur
        mid = 0.5 * (a + b)
        loss = (k_short - mid) if side == "put" else (mid - k_short)
        e_loss += max(0.0, loss) * mass
    return credit - disc * e_loss


def skew_cost(chain: Sequence[dict], spot: float, k_short: float, k_long: float,
              t: float, side: str, r: float = 0.0, q: float = 0.0) -> dict[str, Any]:
    """What skew is costing (or paying) on **this specific structure**.

    Price the identical vertical twice — once on the chain's real per-strike IV,
    once with `IV = IV_ATM` at both strikes — and difference them. A raw skew
    reading is not actionable; this number is.

    Why it matters: a put credit spread sells K₁ and buys K₂ < K₁, and under put
    skew **the further-OTM leg you BUY carries the higher IV**. Steepening skew
    therefore raises the price of the leg you buy by more than the leg you sell,
    compressing credit/width. So the folk rule "steep put skew = more premium"
    is true for a naked put (short skew) and **backwards for a put credit
    spread** (net long skew at the wing). Index call skew slopes down, so a call
    credit spread gets the opposite, favourable treatment.

    Returns `credit_skewed`, `credit_flat_atm`, `cost` (skewed − flat: negative
    = skew is costing you) and `cost_pct` of the flat credit."""
    _require_side(side)
    flat = atm_iv(chain, spot)
    if flat is None:
        return {"credit_skewed": None, "credit_flat_atm": None, "cost": None, "cost_pct": None}

    def sigma_at(k: float) -> float:
        v = iv_at_strike(chain, side, k)
        return v if v and v > 0 else flat

    c_skew = theoretical_credit(spot, k_short, k_long, t, side, sigma_at, r, q)
    c_flat = theoretical_credit(spot, k_short, k_long, t, side, flat, r, q)
    cost = c_skew - c_flat
    return {
        "credit_skewed": c_skew,
        "credit_flat_atm": c_flat,
        "cost": cost,
        "cost_pct": (100.0 * cost / c_flat) if c_flat else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Market regime
# ─────────────────────────────────────────────────────────────────────────────
def market_regime(index: dict | None, cfg_regime: dict) -> dict[str, Any]:
    """Index-level regime from the three *priced* fingerprints. No sentiment
    input is needed to detect a macro shock.

    **The most dangerous property of this product: a crash is when IV is
    richest, and the engine hunts rich IV.** Left alone it would look at a
    post-bad-CPI tape — IV rank 95, premium fat, strikes comfortably far away —
    and sell a bull put into a falling knife. Post-shock IV is compensation for
    genuinely elevated risk, not mispricing.

    Acting is asymmetric: risk-off suppresses `bull_put` (and the put leg of an
    IC); `bear_call` stays eligible.

    Two honest limits, both surfaced rather than hidden: these describe *state*,
    not warning — spot below the gamma flip means you are already through it —
    and `VIX/VIX3M > 1.00` is a high bar that a moderately bad print can miss."""
    bands = cfg_regime.get("bands") or []
    idx = index or {}
    ratio = _num(idx.get("vix_ts") if idx.get("vix_ts") is not None
                 else idx.get("vix_vix3m"))
    band = bands[-1] if bands else {"state": "unknown", "multiplier": 1.0,
                                    "size": 1.0, "suppress": []}
    if ratio is None:
        band = {"state": "unknown", "multiplier": 1.0, "size": 1.0, "suppress": []}
    else:
        for b in bands:
            hi = b.get("max_vix_ts")
            if hi is None or ratio < float(hi):
                band = b
                break
    mult = float(band.get("multiplier", 1.0))
    suppress = list(band.get("suppress") or [])
    notes: list[str] = []

    if idx.get("spot_below_gamma_flip"):
        mult *= float(cfg_regime.get("gamma_flip_penalty", 1.0))
        notes.append("index_below_gamma_flip")     # realized vol ~18% vs ~13% above
    if idx.get("put_skew_steepening"):
        notes.append("index_put_skew_steepening")
        if "bull_put" not in suppress:
            suppress.append("bull_put")
    # Sector dislocation: many names spiking IV together without index stress is
    # ONE shock, not many. Falls out of the union-of-watchlists sweep for free.
    disp = _num(idx.get("dispersion"))
    thr = float(cfg_regime.get("dispersion_threshold", 1.0))
    dislocation = bool(disp is not None and disp > thr)
    if dislocation:
        notes.append("sector_dislocation")
        mult *= 0.85

    strict = str(idx.get("strictness") or "balanced")
    mult *= float((cfg_regime.get("strictness_multiplier") or {}).get(strict, 1.0))
    return {
        "state": band.get("state", "unknown"),
        "vix_ts": ratio,
        "multiplier": max(0.0, min(1.2, mult)),
        "size_factor": float(band.get("size", 1.0)),
        "suppress": suppress,
        "sector_dislocation": dislocation,
        "notes": notes,
    }


def sentiment_persistence(score: float, horizon_days: float, cfg_sent: dict,
                          is_index: bool = False,
                          earnings_inside_tenor: bool = True) -> float:
    """Will this sentiment still hold at horizon `h` (trading days)?

    Positive sentiment decays with a ~1-week half-life and is gone by week 3;
    negative sentiment is still statistically significant at week 13. That
    asymmetry (Heston & Sinha, >900k stories) is the whole reason Simmer treats
    bull puts and bear calls differently after bad news.

    Three mandatory caveats, all implemented here: the long negative half-life
    is realized *at* the next earnings print, so if the spread expires first
    fall back to the short one; index underlyings get a ~1-day half-life; and
    day-0 is mostly contemporaneous impact plus reversal, so it is discounted
    hard rather than treated as predictive."""
    hl_pos = float(cfg_sent.get("halflife_pos_days", 5))
    hl_neg = float(cfg_sent.get("halflife_neg_days", 35))
    if is_index:
        hl_pos = hl_neg = float(cfg_sent.get("index_halflife_days", 1))
    hl = hl_neg if score < 0 else hl_pos
    if score < 0 and cfg_sent.get("earnings_gates_neg_halflife", True) \
            and not earnings_inside_tenor:
        hl = hl_pos
    w = 0.5 ** (max(0.0, horizon_days) / max(hl, 1e-9))
    if horizon_days < 1:
        w *= float(cfg_sent.get("day0_discount", 0.25))
    return w


# ─────────────────────────────────────────────────────────────────────────────
# Gates
# ─────────────────────────────────────────────────────────────────────────────
def _to_days(value: Any) -> float | None:
    """Days-from-now for a catalyst entry. Accepts a number (already relative)
    or `{"days": n}` / `{"dte": n}` — date parsing belongs in the caller, which
    owns the clock. The engine stays pure."""
    if isinstance(value, dict):
        for k in ("days", "dte", "days_from_now"):
            if value.get(k) is not None:
                return _num(value[k])
        return None
    return _num(value)


def global_gates(ctx: dict, cfg: dict) -> list[str]:
    """Gates that depend only on (symbol, expiration) — evaluated once, before
    any structure is considered. Returns veto reasons; empty = clear."""
    g = cfg["gates"]
    out: list[str] = []
    chain, spot, dte = ctx["chain"], ctx["spot"], ctx["dte"]

    # ── Chain sanity (defensive; a crossed/locked market produces nonsense) ──
    puts, calls = strikes_for(chain, "put"), strikes_for(chain, "call")
    if not puts or not calls:
        out.append("chain_sanity:missing_side")
    else:
        lo = min(min(puts), min(calls))
        hi = max(max(puts), max(calls))
        if not (lo <= spot <= hi):
            out.append("chain_sanity:spot_outside_chain")
    if any((row_spread(r) or 0.0) < 0 for r in chain):
        out.append("chain_sanity:crossed_market")
    if not any(_num(r.get("iv")) for r in chain):
        out.append("chain_sanity:greeks_missing")

    # ── Macro-table validity: fail visibly, never sell through a CPI print ──
    if g.get("require_macro_calendar", True):
        valid = ctx["research"].get("macro_valid_through_dte")
        if valid is None:
            out.append("macro_calendar:unavailable")
        elif _num(valid) is not None and float(valid) < dte:
            out.append("macro_calendar:expiry_beyond_valid_through")

    # ── Catalyst lockout ────────────────────────────────────────────────────
    if g.get("catalyst_lockout", True):
        block_days = float(g.get("unconfirmed_earnings_block_days", 7))
        # Earnings hard-vetoes ONLY in the legacy "veto" view; under "consider"
        # (default) and "ignore" it is a folded factor, so the gate skips it.
        earnings_soft = ctx.get("earnings_view") in ("consider", "ignore")
        for cat in ctx["research"].get("catalysts") or []:
            days = _to_days(cat)
            kind = str(cat.get("type") or cat.get("event_type") or "event") \
                if isinstance(cat, dict) else "event"
            confirmed = bool(cat.get("confirmed", True)) if isinstance(cat, dict) else True
            if days is None or days < 0:
                continue
            if kind == "earnings" and earnings_soft:
                # Earnings is folded into the score (or ignored), not a veto here.
                # 8-K and other confirmed binaries still veto.
                continue
            if confirmed and days <= dte:
                out.append(f"catalyst:{kind}_in_tenor")
            elif not confirmed and days <= max(dte, block_days):
                # Unconfirmed earnings block the whole week, not just the day.
                out.append(f"catalyst:unconfirmed_{kind}")

    # ── DTE window (the dollar-friction half is per-structure) ─────────────
    if dte < float(g["dte_min"]) or dte > float(g["dte_max"]):
        out.append("dte_window")

    # ── Volatility floor: gate on PERCENTILE, display rank ─────────────────
    ivp = ctx["metrics"].get("iv_percentile_effective")
    if ivp is None:
        out.append("volatility_history_unavailable")
    elif ivp < float(g["iv_percentile_floor"]):
        # Below IVR ~20 the forward IV/RV is 0.95 — negative-EV before costs.
        out.append("iv_percentile_floor")

    # ── VRP floor — a RATIO, never vol points ──────────────────────────────
    vrp = ctx["metrics"].get("vrp")
    if vrp is None:
        out.append("vrp_unavailable")
    elif vrp < float(g["vrp_ratio_floor"]):
        out.append("vrp_floor")
    elif not ctx["metrics"].get("rv_agrees", True):
        # YZ shows edge but zero-mean close-to-close does not → the "edge" is an
        # overnight-exclusion artifact. Require agreement.
        out.append("rv_estimator_disagreement")
    return out


def structure_gates(cand: dict, ctx: dict, cfg: dict) -> list[str]:
    """Gates that depend on the chosen strikes: liquidity, short delta, and the
    dollar-friction test that DTE alone cannot express."""
    g = cfg["gates"]
    out: list[str] = []

    # ── Liquidity floor — 8% of credit, derived not folklore ───────────────
    # Gross EV is ~7% of max risk at 20% relative VRP and round-trip friction is
    # about the full quoted bid/ask, so on a 5-wide at $3.75 risk the edge is
    # entirely gone somewhere between $0.20 and $0.40 of quoted width.
    credit = cand["credit_mid"]
    pkg = cand["package_spread"]
    if credit is None or credit <= 0:
        out.append("liquidity:no_credit")
    elif pkg is None:
        out.append("liquidity:unquoted")
    elif 100.0 * pkg / credit > float(g["liquidity_spread_pct_of_credit"]):
        out.append("liquidity:spread_pct_of_credit")
    if cand["min_open_interest"] < int(ctx["rule"].get("min_open_interest",
                                                       g["min_open_interest"])):
        out.append("liquidity:open_interest")
    if cand["min_size_at_touch"] is not None \
            and cand["min_size_at_touch"] < int(g["min_size_at_touch"]):
        out.append("liquidity:size_at_touch")

    # ── Short delta band: 5–10Δ shorts are measurably negative-EV ──────────
    sd = cand["short_delta"]
    if sd is None:
        out.append("short_delta:unavailable")
    elif sd < float(g["short_delta_min"]) or sd > float(g["short_delta_max"]):
        out.append("short_delta_band")

    # ── Dollar friction: gross EV ($) ≥ 2 × round-trip cost ────────────────
    # Frictions are fixed in DOLLARS while the dollar edge shrinks with DTE, so
    # a 0DTE spread that is fine on SPX is −48% on a $50 single name. This is
    # why DTE alone is the wrong gate.
    ev_gross = cand["ev_gross_dollars"]
    rt = cand["round_trip_cost"]
    if ev_gross is None or rt is None:
        out.append("friction:unavailable")
    elif ev_gross < float(g["friction_ev_multiple"]) * rt:
        out.append("dollar_friction")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Composite components (each returns 0..1)
# ─────────────────────────────────────────────────────────────────────────────
def _component_structural_safety(cand: dict, ctx: dict, cfg: dict) -> tuple[float, dict]:
    """Short-strike distance beyond the wall + wall strength tier + which side of
    the gamma flip. Weighted heaviest (0.30) because walls are the only part of
    this engine with published, dated, sample-specified hit rates: SpotGamma's
    SPX study (10 May 2019 – 28 May 2024) shows 89% of sessions never break the
    Put Wall intraday and 93% close above it. Those are index statistics —
    single names are noisier, so treat them as an upper bound on confidence."""
    walls = ctx["research"].get("walls") or {}
    em = ctx["metrics"].get("em_1sd")
    legs = cand.get("legs")
    # "both" is legitimate here and ONLY here (plus em-headroom): a condor has
    # two short strikes. Everywhere else a side must be put or call.
    if _require_side(cand["side"], allow_both=True) == "both" and legs:
        pairs = [("put", legs["put"]["short"]), ("call", legs["call"]["short"])]
    else:
        pairs = [(cand["side"], cand["k_short"])]

    scores: list[float] = []
    detail: dict[str, Any] = {}
    for side, k_short in pairs:
        wall = _num(walls.get("put_wall") if side == "put" else walls.get("call_wall"))
        if wall is None or not em:
            detail[f"{side}_reason"] = "no_wall_data"
            scores.append(0.5)               # neutral, flagged in data_quality
            continue
        beyond = (wall - k_short) / em if side == "put" else (k_short - wall) / em
        base = _clamp01(0.5 + beyond)        # at the wall = 0.5, 1 EM beyond = 1.0
        tier = str(walls.get("strength") or walls.get(
            "put_wall_strength" if side == "put" else "call_wall_strength")
            or "low").lower()
        val = base * {"high": 1.0, "medium": 0.88, "low": 0.75}.get(tier, 0.75)
        # Conditional on the gamma-flip side: a ~1.4× realized-vol regime shift
        # across a single level (5-day RV 13% above vs 18% below).
        zg = _num(walls.get("zero_gamma"))
        flip_ok = None
        if zg is not None:
            flip_ok = ctx["spot"] > zg if side == "put" else ctx["spot"] < zg
            val *= 1.0 if flip_ok else 0.85
        scores.append(_clamp01(val))
        detail.update({f"{side}_wall": wall, f"{side}_beyond_em": beyond,
                       "tier": tier, f"{side}_favourable_gamma_side": flip_ok})
    return (min(scores) if scores else 0.5), detail


def _component_em_headroom(cand: dict, ctx: dict, cfg: dict) -> tuple[float, dict]:
    """Short strike vs max(EM_straddle, EM_sigma), in σ units. Full marks at
    `targets.em_headroom_full` EM units out."""
    em = ctx["metrics"].get("em_1sd")
    if not em:
        return 0.5, {"reason": "no_expected_move"}
    legs = cand.get("legs")
    shorts = ([legs["put"]["short"], legs["call"]["short"]]
              if _require_side(cand["side"], allow_both=True) == "both" and legs
              else [cand["k_short"]])
    units = min(abs(k - ctx["spot"]) for k in shorts) / em   # weakest leg governs
    full = float(cfg["targets"]["em_headroom_full"])
    return _clamp01(units / full), {"headroom_em_units": units, "full_at": full}


def _component_vol_richness(ctx: dict, cfg: dict) -> tuple[float, dict]:
    """Dual IVR+IVP condition, IV/RV ratio, term-structure slope.

    Deliberately dominated by the VRP *ratio* rather than the IV level: Option
    Alpha's published backtest found filtering to IVR ≥ 50 collected 28% more
    premium per trade and **lost 71% overall** versus −4% with no IV filter.
    High IV pays more because it is correctly pricing more risk. So a low-IV
    FLOOR is justified (that's the gate) but a high-IV FILTER is not."""
    sw = cfg["sub_weights"]["volatility_richness"]
    m = ctx["metrics"]
    ivp = m.get("iv_percentile_effective")
    vrp = m.get("vrp")
    floor = float(cfg["gates"]["vrp_ratio_floor"])
    full = float(cfg["targets"]["vrp_full"])
    vrp_s = _clamp01(((vrp - floor) / (full - floor)) if vrp is not None and full > floor else 0.0)
    ivp_s = _clamp01((ivp or 0.0) / 100.0)
    ts = m.get("term_slope")            # iv_far/iv_near; >1 = contango = good
    ts_s = _clamp01(((ts or 1.0) - 0.95) / 0.15)
    val = sw["vrp"] * vrp_s + sw["iv_percentile"] * ivp_s + sw["term_structure"] * ts_s
    return _clamp01(val), {"vrp": vrp, "vrp_score": vrp_s, "iv_percentile": ivp,
                           "term_slope": ts}


def _component_credit_quality(cand: dict, ctx: dict, cfg: dict) -> tuple[float, dict]:
    """`Alpha = EV / MaxLoss` (Option Alpha's published ranking metric and the
    only composite any surveyed platform documents — risk-normalized, so $5 EV
    on $50 risk ranks identically to $25 on $250), plus how close C/W gets to
    its N(−d2) ceiling, plus POP at the breakeven."""
    sw = cfg["sub_weights"]["credit_quality"]
    alpha_full = float(cfg["targets"]["alpha_full"])
    a = _clamp01((cand["alpha"] or 0.0) / alpha_full)
    bound = cand["cw_bound"] or 0.0
    eff = _clamp01((cand["cw"] or 0.0) / bound) if bound > 0 else 0.0
    pop = _clamp01(cand["pop_breakeven"] or 0.0)
    val = sw["alpha"] * a + sw["cw_efficiency"] * eff + sw["pop"] * pop
    return _clamp01(val), {"alpha": cand["alpha"], "cw": cand["cw"],
                           "cw_bound": cand["cw_bound"], "cw_efficiency": eff,
                           "pop_breakeven": cand["pop_breakeven"]}


def _component_liquidity(cand: dict, ctx: dict, cfg: dict) -> tuple[float, dict]:
    """Liquidity as a soft multiplier, not only a gate (ORATS' `confidence`
    field does the same). Spread is ALSO expressed in **IV points** — the one
    genuinely professional formulation in the survey, because it normalizes
    across price levels and DTE where a dollar width does not."""
    sw = cfg["sub_weights"]["liquidity_quality"]
    gate_pct = float(cfg["gates"]["liquidity_spread_pct_of_credit"])
    credit, pkg = cand["credit_mid"], cand["package_spread"]
    pct = (100.0 * pkg / credit) if (credit and pkg is not None and credit > 0) else None
    s = _clamp01(1.0 - (pct / gate_pct)) if pct is not None else 0.0
    oi = _clamp01(cand["min_open_interest"] / float(cfg["targets"]["oi_full"]))
    size = _clamp01((cand["min_size_at_touch"] or 0) / float(cfg["targets"]["size_full"]))
    val = sw["spread"] * s + sw["open_interest"] * oi + sw["size_at_touch"] * size
    return _clamp01(val), {"spread_pct_of_credit": pct,
                           "spread_iv_points": cand["spread_iv_points"],
                           "min_open_interest": cand["min_open_interest"],
                           "min_size_at_touch": cand["min_size_at_touch"]}


def _component_sentiment(cand: dict, ctx: dict, cfg: dict) -> tuple[float, dict]:
    """**Side selection and veto only — never a promoter.**

    Neutral (or positive) sentiment scores 1.0, i.e. no penalty; adverse
    sentiment subtracts. A ticker never becomes "ready" *because* sentiment is
    positive. A velocity burst suppresses regardless of direction — unpriced
    uncertainty is the enemy of a premium seller."""
    cs = cfg["sentiment"]
    s = ctx["research"].get("sentiment") or {}
    score = _num(s.get("score"))
    val, detail = 1.0, {"sentiment_score": score, "adverse": False}
    if score is not None:
        adverse = (score < 0 and cand["structure"] in ("bull_put", "iron_condor"))
        if adverse:
            # Negative sentiment persists (half-life ~7 weeks) and the drift runs
            # AGAINST a put spread. Positive sentiment is NOT treated
            # symmetrically: it is gone by week three.
            pers = sentiment_persistence(
                score, ctx["dte"] * (252.0 / 365.0), cs,
                is_index=bool(ctx["rule"].get("is_index")),
                earnings_inside_tenor=bool(ctx["research"].get("earnings_inside_tenor", True)),
            )
            val = _clamp01(1.0 + score * pers)
            detail.update({"adverse": True, "persistence": pers})
    p = _num(s.get("velocity_p"))
    if p is not None and p <= float(cfg["velocity"]["alert_p"]):
        val *= float(cs.get("velocity_suppression", 0.5))
        detail["velocity_burst"] = True
    elif p is not None and p <= float(cfg["velocity"]["warn_p"]):
        val *= 0.85
        detail["velocity_elevated"] = True
    return _clamp01(val), detail


# ─────────────────────────────────────────────────────────────────────────────
# Candidate construction
# ─────────────────────────────────────────────────────────────────────────────
def settings_or(ctx: dict, key: str, default: Any) -> Any:
    """Per-user setting when present, else the engine default. User values are
    clamped by `simmer_config.clamp_setting` on write — server-side enforcement
    is authoritative, so a client asking for a 0.05 short delta never reaches
    here unclamped."""
    v = (ctx.get("settings") or {}).get(key)
    return default if v is None else v


def _package_quote(chain: Sequence[dict], side: str, k_short: float,
                   k_long: float) -> dict[str, Any]:
    """Credit + quoted package width from the real book. `credit_mid` is
    short.mid − long.mid; `package_spread` is the summed per-leg bid/ask width,
    which is what a round trip actually costs."""
    _require_side(side)
    s_row = chain_row(chain, side, k_short)
    l_row = chain_row(chain, side, k_long)
    if not s_row or not l_row:
        return {"ok": False}
    s_mid, l_mid = row_mid(s_row), row_mid(l_row)
    s_sp, l_sp = row_spread(s_row), row_spread(l_row)
    if s_mid is None or l_mid is None:
        return {"ok": False}
    oi = [int(_num(r.get("open_interest")) or 0) for r in (s_row, l_row)]
    sizes = [_num(r.get("bid_size")) for r in (s_row, l_row)] + \
            [_num(r.get("ask_size")) for r in (s_row, l_row)]
    sizes = [x for x in sizes if x is not None]
    return {
        "ok": True,
        "short_row": s_row, "long_row": l_row,
        "credit_mid": s_mid - l_mid,
        "credit_bid": (_num(s_row.get("bid")) or 0.0) - (_num(l_row.get("ask")) or 0.0),
        "package_spread": (s_sp + l_sp) if (s_sp is not None and l_sp is not None) else None,
        "min_open_interest": min(oi) if oi else 0,
        "min_size_at_touch": min(sizes) if sizes else None,
    }


def build_candidate(structure: str, ctx: dict, cfg: dict) -> dict[str, Any] | None:
    """Build one vertical: pick the short strike by DELTA, then SOLVE the width
    for the C/W target. Never the other way round — C/W is a dependent variable
    (see `credit_to_width_bound`).

    Strike placement resolves a genuine tension in the spec. "Place the short
    strike outside BOTH the 1-SD expected move AND the structural wall" and
    "gate short delta to 0.20–0.35" are **not simultaneously satisfiable**: the
    1-SD point sits at roughly 16 delta, which the delta gate vetoes. This is
    the same class of contradiction as "⅓ width at 16 delta". Resolution: the
    delta band is a hard gate and therefore wins; the EM/wall barrier is honored
    **within** the band (step out to it when an in-band strike exists beyond it,
    otherwise stay at the delta target and let the expected-move-headroom
    component score the candidate down). POP is then verified at the breakeven."""
    if structure not in STRUCTURE_SIDE:
        raise ValueError(f"build_candidate handles verticals only, got {structure!r}; "
                         f"an iron condor is built by combining two of them")
    side = STRUCTURE_SIDE[structure]
    chain, spot, t = ctx["chain"], ctx["spot"], ctx["t"]
    r, q = ctx["r"], ctx["q"]
    tg, gates_cfg = cfg["targets"], cfg["gates"]

    # 1. Short strike: nearest to the delta target, inside the delta band.
    listed_all = strikes_for(chain, side)
    d_min = float(settings_or(ctx, "short_delta_min", gates_cfg["short_delta_min"]))
    d_max = float(settings_or(ctx, "short_delta_max", gates_cfg["short_delta_max"]))
    in_band: list[float] = []
    for k in listed_all:
        row = chain_row(chain, side, k)
        d = row_abs_delta(row, spot, t, side, r, q) if row else None
        if d is not None and d_min <= d <= d_max:
            in_band.append(k)
    k_short = None
    if in_band:
        k_short = min(in_band, key=lambda k: abs(
            (row_abs_delta(chain_row(chain, side, k) or {}, spot, t, side, r, q) or 0.0)
            - float(tg["short_delta"])))
    if k_short is None:
        k_short = strike_by_abs_delta(chain, side, spot, t, float(tg["short_delta"]), r, q)
    if k_short is None:
        return None

    # 2. Step further out — to the EM/wall barrier — but only within the band.
    em = ctx["metrics"].get("em_1sd")
    walls = ctx["research"].get("walls") or {}
    floors: list[float] = []
    if em:
        floors.append(spot - em if side == "put" else spot + em)
    wall = _num(walls.get("put_wall") if side == "put" else walls.get("call_wall"))
    if wall is not None:
        floors.append(wall)
    if floors and in_band:
        limit = min(floors) if side == "put" else max(floors)
        outside = [k for k in in_band if (k <= limit if side == "put" else k >= limit)]
        if outside:
            k_short = max(outside) if side == "put" else min(outside)

    s_row = chain_row(chain, side, k_short)
    if not s_row:
        return None
    sigma_short = _num(s_row.get("iv")) or ctx["metrics"].get("atm_iv")
    if not sigma_short or sigma_short <= 0:
        return None

    def sigma_at(k: float) -> float:
        v = iv_at_strike(chain, side, k)
        return v if v and v > 0 else sigma_short

    # 3. Solve the width for the C/W target; snap to a listed strike.
    # The absolute "collect ⅓ of the width" target is INFEASIBLE at the optimal
    # short delta — max C/W = N(−d2), which is ~0.29 at 25Δ — so the effective
    # target is a fixed fraction of that hard ceiling. Chasing the absolute
    # number would silently drive the engine to 0.35Δ+ shorts and 1-wide
    # spreads whose package spread eats the credit.
    bound = credit_to_width_bound(spot, k_short, sigma_short, t, side, r, q)
    cw_target = min(float(tg["credit_to_width"]),
                    float(tg["cw_fraction_of_bound"]) * bound)
    max_width = float(tg["max_width_factor"]) * spot
    w = solve_width_for_target_cw(spot, k_short, t, side, sigma_at,
                                  cw_target, max_width, r, q)
    avail = [k for k in listed_all if (k < k_short if side == "put" else k > k_short)]
    if not avail:
        return None
    if w:
        ideal = (k_short - w) if side == "put" else (k_short + w)
        k_long = min(avail, key=lambda k: abs(k - ideal))
    else:
        # Infeasible target → narrowest available spread (highest achievable C/W).
        k_long = max(avail) if side == "put" else min(avail)

    pkg = _package_quote(chain, side, k_short, k_long)
    if not pkg.get("ok"):
        return None
    width = abs(k_short - k_long)
    credit = pkg["credit_mid"]
    max_loss = max(1e-9, width - credit)

    # 3. Probability + EV. EV uses the FORECAST vol; POP is reported both ways.
    sigma_market = sigma_at(breakeven(k_short, credit, side)) or sigma_short
    sigma_fc = ctx["metrics"].get("rv_forecast") or sigma_market
    step = float(cfg["vol"]["ev_integration_step"])
    max_steps = int(cfg["vol"]["ev_max_steps"])
    ev = expected_value(spot, k_short, k_long, credit, sigma_fc, t, side, r, q, step, max_steps)
    pop_mkt = pop_at_breakeven(spot, k_short, credit, sigma_market, t, side, r, q)
    pop_fc = pop_at_breakeven(spot, k_short, credit, sigma_fc, t, side, r, q)

    short_delta = row_abs_delta(s_row, spot, t, side, r, q)
    fee = float(ctx["rule"].get("commission", 0.35))
    pkg_spread = pkg["package_spread"]
    # Round trip ≈ the full quoted package width (crossed once each way) plus
    # commission on 2 legs × 2 sides.
    round_trip = ((pkg_spread or 0.0) * CONTRACT_MULTIPLIER) + 4.0 * fee
    vega = bs_vega(spot, k_short, sigma_short, t, r, q)
    spread_iv_pts = (100.0 * (pkg_spread or 0.0) / vega) if vega > 0 else None

    cand: dict[str, Any] = {
        "structure": structure,
        "side": side,
        "k_short": k_short,
        "k_long": k_long,
        "width": width,
        "credit_mid": credit,
        "credit_fill": _credit_fill(credit, pkg_spread, float(tg["fill_slippage"]),
                                    float(ctx["rule"].get("tick", 0.01))),
        "package_spread": pkg_spread,
        "max_loss": max_loss,
        "breakeven": breakeven(k_short, credit, side),
        "pop_breakeven": pop_mkt,
        "pop_breakeven_forecast": pop_fc,
        # The gap between the two POPs IS the edge. If they are equal there is
        # no trade — pricing probabilities off the chain's own IV is a tautology.
        "pop_edge": pop_fc - pop_mkt,
        "expected_value": ev,
        "ev_gross_dollars": ev * CONTRACT_MULTIPLIER,
        "alpha": (ev / max_loss) if max_loss > 0 else None,
        "cw": (credit / width) if width > 0 else None,
        "cw_bound": bound,
        "cw_target": cw_target,
        "cw_target_absolute_feasible": float(tg["credit_to_width"]) < bound,
        "short_delta": short_delta,
        "prob_itm_short": prob_itm(spot, k_short, sigma_short, t, side, r, q),
        "prob_touch": prob_touch(spot, k_short, sigma_short, t, r, q),
        "prob_touch_folk_2delta": (2.0 * short_delta) if short_delta is not None else None,
        "min_open_interest": pkg["min_open_interest"],
        "min_size_at_touch": pkg["min_size_at_touch"],
        "spread_iv_points": spread_iv_pts,
        "round_trip_cost": round_trip,
        "skew": skew_cost(chain, spot, k_short, k_long, t, side, r, q),
        "sigma_short": sigma_short,
        "sigma_forecast": sigma_fc,
    }
    return cand


def _credit_fill(credit: float, pkg_spread: float | None, slippage: float,
                 tick: float) -> float:
    """A realistic fill, not the mid: give up `slippage` of the half-spread and
    round DOWN to the tick, so every downstream dollar figure is conservative."""
    if pkg_spread is None or pkg_spread <= 0:
        return credit
    fill = credit - slippage * (pkg_spread / 2.0)
    if tick and tick > 0:
        fill = math.floor(fill / tick) * tick
    return round(max(0.0, fill), 4)


def _combine_iron_condor(put_c: dict, call_c: dict, ctx: dict, cfg: dict) -> dict[str, Any]:
    """Iron condor = both verticals. Only one side can finish ITM, so the loss
    expectations add exactly and `max_loss = max(width) − total credit`. The
    risk-neutral EV invariant survives the combination unchanged."""
    spot, t, r, q = ctx["spot"], ctx["t"], ctx["r"], ctx["q"]
    step = float(cfg["vol"]["ev_integration_step"])
    max_steps = int(cfg["vol"]["ev_max_steps"])
    credit = put_c["credit_mid"] + call_c["credit_mid"]
    width = max(put_c["width"], call_c["width"])
    max_loss = max(1e-9, width - credit)
    ev = 0.0
    for leg in (put_c, call_c):
        ev += expected_value(spot, leg["k_short"], leg["k_long"], leg["credit_mid"],
                             leg["sigma_forecast"], t, leg["side"], r, q, step, max_steps)
    pkg = ((put_c["package_spread"] or 0.0) + (call_c["package_spread"] or 0.0))
    fee = float(ctx["rule"].get("commission", 0.35))
    out = dict(put_c)
    out.update({
        "structure": "iron_condor",
        "side": "both",
        "legs": {"put": {"short": put_c["k_short"], "long": put_c["k_long"]},
                 "call": {"short": call_c["k_short"], "long": call_c["k_long"]}},
        "width": width,
        "credit_mid": credit,
        "credit_fill": _credit_fill(credit, pkg, float(cfg["targets"]["fill_slippage"]),
                                    float(ctx["rule"].get("tick", 0.01))),
        "package_spread": pkg,
        "max_loss": max_loss,
        "expected_value": ev,
        "ev_gross_dollars": ev * CONTRACT_MULTIPLIER,
        "alpha": ev / max_loss,
        "cw": credit / width if width else None,
        "round_trip_cost": pkg * CONTRACT_MULTIPLIER + 8.0 * fee,
        "min_open_interest": min(put_c["min_open_interest"], call_c["min_open_interest"]),
        "min_size_at_touch": min([x for x in (put_c["min_size_at_touch"],
                                              call_c["min_size_at_touch"]) if x is not None],
                                 default=None),
        "pop_breakeven": min(put_c["pop_breakeven"], call_c["pop_breakeven"]),
        "pop_breakeven_forecast": min(put_c["pop_breakeven_forecast"],
                                      call_c["pop_breakeven_forecast"]),
        "short_delta": max([x for x in (put_c["short_delta"], call_c["short_delta"])
                            if x is not None], default=None),
    })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Research metrics
# ─────────────────────────────────────────────────────────────────────────────
def compute_metrics(ctx: dict, cfg: dict) -> dict[str, Any]:
    """Everything expiry-level that the gates and components read: IV rank +
    percentile, Yang-Zhang and close-to-close RV, VRP ratio, expected move,
    RR25 skew, term slope."""
    v = cfg["vol"]
    res = ctx["research"]
    chain, spot, dte, t = ctx["chain"], ctx["spot"], ctx["dte"], ctx["t"]

    iv = _num(res.get("iv30")) or atm_iv(chain, spot)
    hist = [x for x in (_num(h) for h in res.get("iv_history") or []) if x is not None]
    low = _num(res.get("iv_52w_low")) or (min(hist) if hist else None)
    high = _num(res.get("iv_52w_high")) or (max(hist) if hist else None)

    # IVR/IVP are a rank over DAILY history — intraday recompute is meaningless,
    # so the watcher caches them until the next close and passes them straight
    # back in. Computing from `iv_history` is the fallback, not the hot path.
    # Note: the history must be EX-EARNINGS IV, or every quarterly cycle injects
    # a spurious spike that pins the 52-week max for a year.
    ivr = _num(res.get("iv_rank"))
    if ivr is None and iv and low is not None and high is not None:
        ivr = iv_rank(iv, low, high)
    ivp = _num(res.get("iv_percentile"))
    days = int(_num(res.get("iv_history_days")) or len(hist))
    if ivp is None and iv and hist:
        ivp = iv_percentile(iv, hist)
    enough = days >= int(v["min_history_days"])
    provisional = ivp is not None and not enough

    ohlc = res.get("ohlc") or []
    ann = int(v["annualization_days"])
    yz = _num(res.get("rv_yang_zhang")) or yang_zhang_rv(ohlc, ann)
    cc = _num(res.get("rv_close_to_close")) or close_to_close_rv(ohlc, ann, zero_mean=True)
    agrees = rv_agreement(yz, cc, float(cfg["gates"]["rv_agreement_tolerance"])) \
        if (yz is not None and cc is not None) else True
    vrp = vrp_ratio(iv, yz)

    # Cold start: rank IV30/RV20 across the watchlist TODAY — zero history, and
    # on par with 252-day IVR. Blend once history exists.
    xs = cross_sectional_percentile(vrp, res.get("peer_vrp_ratios") or []) \
        if vrp is not None else None
    blend_w = float(v["cross_sectional_blend"])
    if ivp is not None and enough and xs is not None:
        ivp_eff = (1.0 - blend_w) * ivp + blend_w * xs
    elif ivp is not None and enough:
        ivp_eff = ivp
    elif xs is not None:
        ivp_eff = xs
    else:
        ivp_eff = ivp if ivp is not None else None

    em = expected_move(spot, atm_iv(chain, spot) or iv, dte,
                       atm_straddle_mid(chain, spot), float(v["days_per_year"]))

    # Per-ticker term structure: the watcher samples a far (~90d) expiry and
    # passes term_slope + the raw far-expiry IV/tenor straight through. Fall
    # back to computing the slope from those raw legs if only they are present.
    atm = atm_iv(chain, spot)
    iv_near = atm if atm is not None else iv
    iv_far = _num(res.get("iv_far"))
    t_far = _num(res.get("t_far"))
    ts = _num(res.get("term_slope"))
    ts_pp = _num(res.get("term_slope_pp"))
    if ts is None and iv_near and iv_far:
        _tsd = term_structure(iv_near, iv_far)
        ts, ts_pp = _tsd["term_slope"], _tsd["term_slope_pp"]

    # Earnings VRP decomposition (VolRadar): split VRP into the earnings-driven
    # and ex-earnings components off the two-expiry IVs + the known earnings date
    # (the watcher sets `earnings_vrp_event` when a catalyst falls in the window).
    decomp = earnings_vrp(iv_near, t, iv_far, t_far, yz,
                          bool(res.get("earnings_vrp_event")))

    return {
        "atm_iv": atm,
        "iv30": iv,
        "iv_rank": ivr,                       # DISPLAY this
        "iv_percentile": ivp,                 # GATE on this
        "iv_percentile_effective": ivp_eff,   # blended with the cross-section
        "iv_percentile_cross_sectional": xs,
        "iv_history_days": days,
        "iv_rank_provisional": provisional,
        "rv_yang_zhang": yz,
        "rv_close_to_close": cc,
        "rv_agrees": agrees,
        "rv_forecast": _num(res.get("rv_forecast")) or yz,
        "vrp": vrp,
        "term_slope": ts,
        "term_slope_pp": ts_pp,
        "vrp_total_pp": decomp["vrp_total_pp"],
        "vrp_earnings_pp": decomp["vrp_earnings_pp"],
        "vrp_ex_earnings_pp": decomp["vrp_ex_earnings_pp"],
        "max_pain": max_pain(chain, spot),
        "rr25_put_minus_call": _num(res.get("rr25_put_minus_call"))
        if res.get("rr25_put_minus_call") is not None
        else rr25_put_minus_call(chain, spot, t, ctx["r"], ctx["q"]),
        **em,
    }


def _data_quality(ctx: dict, cand: dict | None, cfg: dict) -> tuple[dict, float]:
    """0–1 quality per input group plus an overall confidence. Degrade a
    candidate by measured data quality rather than vetoing it (ORATS' published
    `confidence` field does exactly this)."""
    m, res = ctx["metrics"], ctx["research"]
    v = cfg["vol"]
    days = m.get("iv_history_days") or 0
    if days >= int(v["iv_history_days"]):
        iv_q = 1.0
    elif days >= int(v["min_history_days"]):
        iv_q = 0.6
    elif m.get("iv_percentile_cross_sectional") is not None:
        iv_q = 0.35                       # cross-sectional only — day-one path
    else:
        iv_q = 0.0
    rv_q = 0.0 if m.get("rv_yang_zhang") is None else (1.0 if m.get("rv_agrees") else 0.4)
    walls_q = 1.0 if (res.get("walls") or {}).get("put_wall") is not None else 0.5
    news_q = 1.0 if res.get("sentiment") else 0.7      # absence isn't fatal
    if cand and cand.get("credit_mid") and cand.get("package_spread") is not None:
        gate = float(cfg["gates"]["liquidity_spread_pct_of_credit"])
        quote_q = _clamp01(1.0 - (100.0 * cand["package_spread"] / cand["credit_mid"]) / gate)
    else:
        quote_q = 0.0
    dq = {
        "iv_history": iv_q,
        "iv_history_days": days,
        "iv_rank_provisional": bool(m.get("iv_rank_provisional")),
        "realized_vol": rv_q,
        "rv_estimators_agree": bool(m.get("rv_agrees")),
        "walls": walls_q,
        "news": news_q,
        "quote": quote_q,
        "macro_calendar": res.get("macro_valid_through_dte") is not None,
    }
    conf = _clamp01(0.30 * iv_q + 0.25 * rv_q + 0.20 * quote_q +
                    0.15 * walls_q + 0.10 * news_q)
    return dq, conf


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_readiness(inputs: dict, cfg: dict | None = None) -> dict[str, Any]:
    """Score one `(symbol, expiration)` for premium selling. Pure — no I/O.

    Expected `inputs` (everything under `research` is optional but its absence
    shows up in `data_quality` and, for the gated fields, as a veto):

        symbol      str
        spot        float
        expiration  str            (echoed through; the caller owns the clock)
        dte         float          calendar days to expiration
        chain       [{strike, side, bid, ask, iv, delta, open_interest,
                      bid_size, ask_size}]
        research    {iv30, iv_history[], iv_52w_low/high, ohlc[], rv_forecast,
                     term_slope, peer_vrp_ratios[], walls{put_wall, call_wall,
                     zero_gamma, strength}, catalysts[{type, days, confirmed}],
                     macro_valid_through_dte, sentiment{score, velocity_p},
                     days_to_cover, earnings_inside_tenor, index{vix_ts,
                     spot_below_gamma_flip, put_skew_steepening, dispersion,
                     strictness}}
        settings    {structures_enabled[], short_delta_min/max, dte_min/max,
                     squeeze_veto}   — per-user, clamped server-side

    Returns the response envelope: decision / score / confidence / regime /
    structure / strikes / credit_mid / credit_fill / max_loss / pop_breakeven /
    expected_value / alpha / components{} / veto_reasons[] / avoid_if[] /
    data_quality{}, plus `metrics{}` and `management{}`."""
    symbol = str(inputs.get("symbol") or "").upper()
    cfg = cfg or simmer_config.resolved(symbol)
    rule = cfg.get("ticker") or simmer_config.ticker_rule(symbol)
    settings = inputs.get("settings") or {}
    v = cfg["vol"]

    spot = _num(inputs.get("spot")) or 0.0
    dte = _num(inputs.get("dte"))
    dte = 0.0 if dte is None else dte
    chain = [r for r in (inputs.get("chain") or []) if isinstance(r, dict)]
    research = inputs.get("research") or {}

    ctx: dict[str, Any] = {
        "symbol": symbol, "spot": spot, "dte": dte,
        "t": max(dte, 0.0) / float(v["days_per_year"]),
        "r": float(v["risk_free_rate"]), "q": float(v["dividend_yield"]),
        "chain": chain, "research": research, "rule": rule, "settings": settings,
    }
    # Earnings handling. The caller passes an `earnings` block when the expiry
    # sits in an earnings window. Earnings is a FACTOR folded into the score, not
    # a hard veto (8-K/macro are unaffected and still veto). Three views:
    #   "consider" (default) — fold the analyzer bias: a `go`+aligned read LIFTS,
    #                          opposed penalizes, and a `no-go` HOLDS THE NAME
    #                          BACK (it can never reach "ready" — earnings cuts
    #                          both ways). No hard earnings veto.
    #   "ignore"            — treat earnings as absent: no veto, no fold (the pure
    #                          ex-earnings structural/VRP decision — "what if
    #                          earnings weren't happening"). Powers the toggle-off.
    #   "veto"              — legacy hard veto (kept for explicit conservative use).
    # Back-compat: bool mode True→"consider", False→"veto".
    earn_in = inputs.get("earnings") or {}
    ctx["earnings"] = earn_in
    _view = earn_in.get("view")
    if _view not in ("consider", "ignore", "veto"):
        _view = "consider" if earn_in.get("mode") else "veto"
    ctx["earnings_view"] = _view if earn_in.get("in_window") else None
    envelope: dict[str, Any] = {
        "symbol": symbol,
        "expiration": inputs.get("expiration"),
        "dte": dte,
        "spot": spot,
        "engine_version": cfg.get("engine_version"),
        "decision": "vetoed",
        "score": 0.0,
        "confidence": 0.0,
        "regime": None,
        "structure": None,
        "strikes": None,
        "credit_mid": None,
        "credit_fill": None,
        "max_loss": None,
        "pop_breakeven": None,
        "expected_value": None,
        "alpha": None,
        "components": {},
        "veto_reasons": [],
        "avoid_if": [],
        "data_quality": {},
        "metrics": {},
        "earnings": None,
        "management": dict(cfg["management"]),
    }

    if spot <= 0 or not chain:
        envelope["veto_reasons"] = ["chain_sanity:no_chain"]
        return envelope
    if not rule.get("eligible", True):
        envelope["veto_reasons"] = [f"ineligible:{rule.get('ineligible_reason', 'not_tradable')}"]
        return envelope

    ctx["metrics"] = compute_metrics(ctx, cfg)
    envelope["metrics"] = ctx["metrics"]

    reg = market_regime(research.get("index"), cfg["regime"])
    envelope["regime"] = reg

    vetoes = global_gates(ctx, cfg)
    dq, conf = _data_quality(ctx, None, cfg)
    envelope["data_quality"], envelope["confidence"] = dq, conf
    if vetoes:
        envelope["veto_reasons"] = vetoes
        return envelope

    # ── Which structures are even eligible (side selection) ────────────────
    allowed = [s for s in (settings.get("structures_enabled")
                           or rule.get("structures") or STRUCTURES) if s in STRUCTURES]
    avoid_if: list[str] = []
    for s in reg["suppress"]:
        if s in allowed:
            allowed.remove(s)
            avoid_if.append(f"regime:{reg['state']}_suppresses_{s}")
    # Squeeze: forced covering only pushes price UP, so it threatens the CALL
    # side alone. bull_put stays eligible on purpose.
    dtc = _num(research.get("days_to_cover"))
    squeeze_on = bool(settings.get("squeeze_veto", rule.get("squeeze_veto", True)))
    if dtc is not None and squeeze_on and dtc >= float(cfg["squeeze"]["dtc_veto"]):
        for s in ("bear_call", "iron_condor"):
            if s in allowed:
                allowed.remove(s)
        avoid_if.append(f"squeeze:days_to_cover_{dtc:g}")
    elif dtc is not None and dtc >= float(cfg["squeeze"]["dtc_warn"]):
        avoid_if.append(f"squeeze_watch:days_to_cover_{dtc:g}")
    # Fresh negative sentiment blocks bull puts (persistent drift against the
    # position). Fresh POSITIVE sentiment does NOT symmetrically block bear
    # calls — positive decays with a ~1-week half-life and is gone by week 3.
    sent = _num((research.get("sentiment") or {}).get("score"))
    if sent is not None and sent <= float(cfg["sentiment"]["negative_veto"]):
        for s in ("bull_put", "iron_condor"):
            if s in allowed:
                allowed.remove(s)
        avoid_if.append("sentiment:fresh_negative_blocks_put_side")
    if research.get("ex_dividend_in_tenor"):
        # Assignment on a short call leaves you SHORT the stock, and through the
        # ex-date you owe the dividend on top of the forfeited extrinsic.
        avoid_if.append("early_assignment:ex_dividend_in_tenor")

    # An iron condor CONTAINS both verticals, so suppressing either side
    # suppresses the condor with it — "risk-off blocks bull puts and the put leg
    # of iron condors" is not satisfied by leaving the condor eligible.
    if "iron_condor" in allowed and not {"bull_put", "bear_call"} <= set(allowed):
        allowed.remove("iron_condor")
        avoid_if.append("iron_condor_requires_both_sides")

    if not allowed:
        envelope["veto_reasons"] = ["no_eligible_structure"]
        envelope["avoid_if"] = avoid_if
        return envelope

    # ── Build + gate each structure, keep the best scoring survivor ─────────
    verticals: dict[str, dict] = {}
    for structure in ("bull_put", "bear_call"):
        c = build_candidate(structure, ctx, cfg)
        if c:
            verticals[structure] = c
    candidates: list[dict] = [c for s, c in verticals.items() if s in allowed]
    if "iron_condor" in allowed and len(verticals) == 2:
        candidates.append(_combine_iron_condor(verticals["bull_put"],
                                               verticals["bear_call"], ctx, cfg))

    scored: list[tuple[float, dict, dict, list[str]]] = []
    all_reasons: list[str] = []
    for cand in candidates:
        reasons = structure_gates(cand, ctx, cfg)
        if reasons:
            all_reasons.extend(f"{cand['structure']}:{x}" for x in reasons)
            continue
        comps: dict[str, Any] = {}
        vals: dict[str, float] = {}
        vals["structural_safety"], comps["structural_safety"] = \
            _component_structural_safety(cand, ctx, cfg)
        vals["expected_move_headroom"], comps["expected_move_headroom"] = \
            _component_em_headroom(cand, ctx, cfg)
        vals["volatility_richness"], comps["volatility_richness"] = \
            _component_vol_richness(ctx, cfg)
        vals["credit_quality"], comps["credit_quality"] = \
            _component_credit_quality(cand, ctx, cfg)
        vals["liquidity_quality"], comps["liquidity_quality"] = \
            _component_liquidity(cand, ctx, cfg)
        vals["sentiment_lean"], comps["sentiment_lean"] = \
            _component_sentiment(cand, ctx, cfg)
        w = cfg["weights"]
        raw = sum(w.get(k, 0.0) * val for k, val in vals.items())
        score = 100.0 * raw * float(reg["multiplier"])
        # `score` last so a detail key of the same name can never shadow it.
        # `weight` is the RESOLVED composite weight (after simmer/simmer_tickers.json
        # overrides + normalization) so the UI attributes points from live
        # config instead of hardcoding the weights — they must never diverge.
        for k in comps:
            comps[k] = {**(comps[k] or {}), "score": round(vals[k], 6),
                        "weight": round(float(w.get(k, 0.0)), 6)}
        scored.append((score, cand, comps, reasons))

    if not scored:
        envelope["veto_reasons"] = sorted(set(all_reasons)) or ["no_candidate"]
        envelope["avoid_if"] = avoid_if
        return envelope

    score, cand, comps, _ = max(scored, key=lambda x: x[0])
    dq, conf = _data_quality(ctx, cand, cfg)

    # Soft warnings that inform but never veto.
    if cand.get("skew", {}).get("cost_pct") is not None and cand["skew"]["cost_pct"] < -5.0:
        avoid_if.append(f"skew:costs_{abs(cand['skew']['cost_pct']):.1f}pct_of_credit")
    if cand.get("cw") is not None and cand.get("cw_target") \
            and cand["cw"] < 0.8 * cand["cw_target"]:
        avoid_if.append("credit_to_width_below_target")
    if cand.get("cw_target_absolute_feasible") is False:
        # Honest disclosure rather than silent retargeting: "collect ⅓ of the
        # width" is unreachable at this short delta, at ANY width.
        avoid_if.append("credit_to_width_target_infeasible_at_this_delta")
    if ctx["metrics"].get("iv_rank_provisional"):
        avoid_if.append("iv_rank_provisional_short_history")
    if reg["state"] in ("transition", "backwardation", "stressed"):
        avoid_if.append(f"regime:{reg['state']}_reduce_size")
    if cand.get("pop_edge") is not None and cand["pop_edge"] <= 0:
        # Market-implied POP and forecast POP agree → no edge, by construction.
        avoid_if.append("no_forecast_edge_over_market_iv")
    # Earnings VRP decomposition: positive total VRP but NEGATIVE ex-earnings VRP
    # means the premium is entirely the pending catalyst — the baseline vol
    # regime does not support selling. Soft, because the catalyst gate already
    # hard-vetoes earnings actually inside the tenor; this catches earnings just
    # OUTSIDE it that still inflate the front IV.
    vrp_total_pp = ctx["metrics"].get("vrp_total_pp")
    vrp_ex_pp = ctx["metrics"].get("vrp_ex_earnings_pp")
    if vrp_total_pp is not None and vrp_ex_pp is not None \
            and vrp_total_pp > 0 and vrp_ex_pp < 0:
        avoid_if.append("premium_entirely_earnings_driven")

    # ── Earnings mode: fold the cached directional bias into the score ────────
    # Fold the earnings bias per the view (see the ctx block above).
    #   consider + go   : aligned LIFTS (conf-scaled, ±max_lift); opposed penalizes.
    #   consider + no-go: HOLDS THE NAME BACK — negative pressure AND a hard cap
    #                     below the ready band, so a no-go earnings read can never
    #                     show "ready" even if the structure otherwise clears.
    #   ignore          : no fold — the pure ex-earnings score stands.
    bands = cfg["decision"]
    earn_block = None
    _earn = ctx.get("earnings") or {}
    _view = ctx.get("earnings_view")
    if _earn.get("in_window"):
        _dir = str(_earn.get("direction") or "neutral")
        _conf = _num(_earn.get("confidence")) or 0.0
        _go = bool(_earn.get("go"))
        _max_lift = float(cfg.get("earnings", {}).get("max_lift", 15.0))
        _lift = 0.0
        _held_back = False
        if _view == "consider":
            _sign = {"bullish": 1.0, "bearish": -1.0}.get(_dir, 0.0)
            _lean = {"bull_put": 1.0, "bear_call": -1.0}.get(cand["structure"], 0.0)
            _align = _lean * _sign
            if _dir == "neutral" and cand["structure"] == "iron_condor":
                _align = 1.0
            if _go:
                _lift = round(_align * _conf * _max_lift, 2)
            else:
                _lift = round(-abs(_conf) * _max_lift, 2)
                _held_back = True
            score = max(0.0, min(100.0, score + _lift))
            if _held_back:
                score = min(score, float(bands["ready"]) - 0.01)   # never "ready" on a no-go
        earn_block = {
            "in_window": True,
            "view": _view,
            "applied": _view == "consider",
            "held_back": _held_back,
            "direction": _dir,
            "confidence": round(_conf, 3),
            "go": _go,
            "lift": _lift,
            # A ready earnings name under a go read is a "sell the run-up, CLOSE
            # BEFORE the print" play — never a hold-through. Surfaced for the UI.
            "close_before_print": _view == "consider" and _go,
            "earnings_date": _earn.get("earnings_date"),
            "rationale": _earn.get("rationale"),
        }

    decision = "ready" if score >= float(bands["ready"]) \
        else "watch" if score >= float(bands["watch"]) else "avoid"

    strikes: dict[str, Any] = cand.get("legs") or {
        "short": cand["k_short"], "long": cand["k_long"], "width": cand["width"],
    }
    envelope.update({
        "decision": decision,
        "score": round(score, 2),
        "confidence": round(conf, 4),
        "structure": cand["structure"],
        "strikes": strikes,
        "credit_mid": cand["credit_mid"],
        "credit_fill": cand["credit_fill"],
        "max_loss": cand["max_loss"],
        "pop_breakeven": cand["pop_breakeven"],
        "expected_value": cand["expected_value"],
        "alpha": cand["alpha"],
        "components": comps,
        "veto_reasons": [],
        "avoid_if": avoid_if,
        "data_quality": dq,
        "earnings": earn_block,
        "candidate": cand,
        "rejected": sorted(set(all_reasons)),
    })
    return envelope
