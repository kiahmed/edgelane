"""Black-Scholes projection — Python port of the Matrix Lookup grid (§8.3).

Spec: docs/level_engine.md §2 ("the one real port"), §8.3, §11.
Source: `market/ui/index.html` — `_normCdf`, `_bsPrice`, `_priceSpreadAtSpot`,
`_ceil15MinET`, `_buildProjectionGrid`. Pure, stdlib only.

What is ported verbatim (same formulas, same constants)
-------------------------------------------------------
* `_norm_cdf`: Abramowitz-Stegun 7.1.26 erf approximation (a1..a5, p), NOT
  math.erf — so a value here equals the page's cell to ~1e-12.
* `bs_price`: no dividend; T <= 0 → intrinsic; sigma missing/<=0 → discounted
  intrinsic max(0, S − K·e^{−rT}) / max(0, K·e^{−rT} − S).
* `price_spread_at_spot`: Σ long_short × bs_price; r defaults to 0.05; a leg with
  no IV is priced at the sigma-less fallback and flags `missing_iv`.
* Time: T (years) = calendar days to expiry / 365, expiry = 16:00 ET on the
  expiration date (the page's `_msAtETClock(exp, 16, 0)`). Minutes-to-expiry
  m → T = m / 1440 / 365.
* Grid anchoring (the page's `offset`): the model is pinned to the LIVE premium —
  offset = live_signed − model(spot_now, T_now), live_signed = +|net| for a
  debit, −|net| for a credit; a cell = model(S, T) + offset, negated for a
  credit; the page displays |cell|, and so do we (`grid_cell`).

Decisions (spec ambiguities, documented)
----------------------------------------
* PROJECTED TIME. The page has no "default column" — its columns are `now`, then
  every ET quarter-hour until expiry (display-capped at 10), then `exp`. The R/R
  agent projects "spot reaches the target at `at_time_fraction` of the remaining
  time" (strategy agent uses 0.5 = midpoint of now → 16:00 ET) and SNAPS that
  instant to the grid's quarter-hour cadence (expiry is itself on the cadence, so
  quarter-hours ≡ minutes-to-expiry multiples of 15), never earlier than now —
  i.e. the value the operator would read in that grid column. The page's
  10-column display cap is ignored (a display limit, not math).
* IV UNIT. The page uses `leg.iv` as-is. Legacy scorers store
  `leg.iv = contract.iv / 100` (a JSX-era percent convention) while the
  normalised Tradier chain carries `mid_iv` as a FRACTION (0.20) — so the page's
  sigma on a live pick is 100× too small (near-intrinsic projection). The R/R
  agent therefore passes `iv_by_leg` from the chain contracts; `_sigma` treats a
  value > 3 as percent (÷100) so either unit is accepted. Legs keep the legacy
  /100 value for shape parity (see report: requested change for the page).
* Dollars: R/R amounts are $ per one structure (points × 100 multiplier).
  cost = max_loss × 100 (= debit for a debit structure; capital at risk for a
  credit one); pnl = projected close P&L; value = cost + pnl (for a debit, the
  projected close value); multiple = value / cost (§8.3 payoff_multiple).
"""
from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
R_DEFAULT = 0.05                  # page: `if (r == null) r = 0.05`
MULTIPLIER = 100.0                # SPX/SPY option contract multiplier
GRID_STEP_MIN = 15                # page: MIN_STEP = 15 (ET quarter-hours)
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)


# ── Black-Scholes core (page: _normCdf / _bsPrice) ──────────────────────────
def _norm_cdf(x: float) -> float:
    a1, a2, a3 = 0.254829592, -0.284496736, 1.421413741
    a4, a5, p = -1.453152027, 1.061405429, 0.3275911
    sign = -1.0 if x < 0 else 1.0
    ax = abs(x) / math.sqrt(2.0)
    t = 1.0 / (1.0 + p * ax)
    erf = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-ax * ax)
    return 0.5 * (1.0 + sign * erf)


def bs_price(S: float, K: float, T: float, r: float, sigma: float | None, side: str) -> float | None:
    if S is None or K is None or S <= 0 or K <= 0:
        return None
    if T <= 0:
        return max(0.0, S - K) if side == "call" else max(0.0, K - S)
    if sigma is None or sigma <= 0:
        disc = K * math.exp(-r * T)
        return max(0.0, S - disc) if side == "call" else max(0.0, disc - S)
    sq = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sq)
    d2 = d1 - sigma * sq
    if side == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def _sigma(v) -> float | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v / 100.0 if v > 3.0 else v


def _leg_key(leg: dict):
    return leg.get("symbol") or (float(leg["strike"]), leg.get("side"))


def price_spread_at_spot(legs: list[dict], S: float, T: float, r: float | None = R_DEFAULT,
                         iv_by_leg: dict | None = None) -> tuple[float, bool] | None:
    """Page `_priceSpreadAtSpot` → (total, missing_iv) or None.

    `iv_by_leg` (optional) maps leg symbol (or (strike, side)) → IV; when a leg
    is found there it overrides `leg.iv`."""
    if not legs or S is None:
        return None
    if r is None:
        r = R_DEFAULT
    total, missing = 0.0, False
    for leg in legs:
        raw = leg.get("iv")
        if iv_by_leg is not None and _leg_key(leg) in iv_by_leg:
            raw = iv_by_leg[_leg_key(leg)]
        sigma = _sigma(raw) if iv_by_leg is not None else (
            float(raw) if raw is not None and float(raw) > 0 else None)
        if sigma is None:
            missing = True
        ls = leg.get("long_short", leg.get("longShort"))
        px = bs_price(S, float(leg["strike"]), T, r, sigma, leg.get("side"))
        if px is None:
            return None
        total += (float(ls) if ls is not None else 0.0) * px
    return total, missing


def years_from_minutes(minutes: float) -> float:
    return max(0.0, minutes / 1440.0 / 365.0)


def project_value(legs: list[dict], spot_at: float, minutes_to_expiry_at: float,
                  iv_by_leg: dict | None = None, r: float = R_DEFAULT) -> float | None:
    """Raw model value of the legs (Σ long_short × BS, per share, signed: a debit
    structure is > 0, a credit structure < 0) at `spot_at` with
    `minutes_to_expiry_at` minutes left. This is the page's `_priceSpreadAtSpot
    (...).total` with T = minutes/1440/365 — no live-premium anchoring (see
    `grid_cell`)."""
    out = price_spread_at_spot(legs, spot_at, years_from_minutes(minutes_to_expiry_at), r, iv_by_leg)
    return None if out is None else out[0]


def grid_cell(legs: list[dict], net_premium: float, is_credit: bool, spot_now: float,
              minutes_now: float, spot_at: float, minutes_at: float,
              iv_by_leg: dict | None = None, r: float = R_DEFAULT) -> float | None:
    """One Matrix Lookup cell: the projected premium (per share, >= 0 as the page
    displays it) — model anchored to the live premium via the page's offset."""
    anchor = price_spread_at_spot(legs, spot_now, years_from_minutes(minutes_now), r, iv_by_leg)
    if anchor is None:
        return None
    live_signed = -abs(net_premium) if is_credit else abs(net_premium)
    offset = live_signed - anchor[0]
    px = price_spread_at_spot(legs, spot_at, years_from_minutes(minutes_at), r, iv_by_leg)
    if px is None:
        return None
    adjusted = px[0] + offset
    return abs(-adjusted if is_credit else adjusted)


# ── time helpers (page: _msAtETClock / _inSessionET / _ceil15MinET) ─────────
def expiry_close_utc(expiration: str | date) -> datetime:
    """16:00 ET on the expiration date, as aware UTC (page `_msAtETClock(exp,16,0)`)."""
    d = expiration if isinstance(expiration, date) else date.fromisoformat(str(expiration)[:10])
    return datetime.combine(d, SESSION_CLOSE, tzinfo=ET).astimezone(timezone.utc)


def minutes_to_expiry(now: datetime, expiration: str | date | None,
                      dte_fallback: float | None = None) -> float:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if expiration:
        return max(0.0, (expiry_close_utc(expiration) - now).total_seconds() / 60.0)
    return max(0.0, float(dte_fallback or 0.0) * 1440.0)


def _in_session_et(ts: datetime) -> bool:
    e = ts.astimezone(ET)
    if e.weekday() >= 5:
        return False
    return SESSION_OPEN <= e.time().replace(second=0, microsecond=0) < SESSION_CLOSE


def _ceil15_et(ts: datetime) -> datetime:
    e = ts.astimezone(ET)
    rem = e.minute % GRID_STEP_MIN
    return ts if rem == 0 else ts + timedelta(minutes=GRID_STEP_MIN - rem)


def time_axis(now: datetime, expiration: str | date, max_cols: int = 10) -> list[dict]:
    """The page's time columns (same-day / in-session case): `now`, then
    quarter-hours from ceil15(now + 1 min), every 15 min, up to max_cols − 1
    cells, then `exp`. Each: {ts, minutes_to_expiry, label}. Out-of-session
    starts (pre-open) begin at 09:30 ET like the page; multi-day expiries are
    out of scope (v1 is 0DTE)."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    exp = expiry_close_utc(expiration)
    cursor = now
    if not _in_session_et(now):
        e = now.astimezone(ET)
        if e.weekday() < 5 and e.time() < SESSION_OPEN:
            cursor = datetime.combine(e.date(), SESSION_OPEN, tzinfo=ET).astimezone(timezone.utc)
    first = min(cursor, exp)
    cells = [{"ts": first, "minutes_to_expiry": max(0.0, (exp - first).total_seconds() / 60),
              "label": "now" if cursor == now else "open"}]
    cursor = _ceil15_et(cursor + timedelta(minutes=1))
    while len(cells) < max_cols - 1 and cursor < exp and _in_session_et(cursor):
        cells.append({"ts": cursor, "minutes_to_expiry": (exp - cursor).total_seconds() / 60,
                      "label": cursor.astimezone(ET).strftime("%H:%M")})
        cursor += timedelta(minutes=GRID_STEP_MIN)
    if cells[-1]["ts"] < exp:
        cells.append({"ts": exp, "minutes_to_expiry": 0.0, "label": "exp"})
    return cells


def projected_minutes(minutes_now: float, at_time_fraction: float) -> float:
    """Minutes-to-expiry at which the target is assumed reached: now +
    fraction × remaining, snapped to the nearest grid column — `now` or an ET
    quarter-hour (multiples of 15 min-to-expiry, since expiry 16:00 ET is on
    the cadence; 0 = `exp`) — never before now (<= minutes_now)."""
    f = min(1.0, max(0.0, float(at_time_fraction)))
    m_now = max(0.0, float(minutes_now))
    raw = m_now * (1.0 - f)
    q = math.floor(raw / GRID_STEP_MIN + 0.5) * GRID_STEP_MIN
    cols = [c for c in (q - GRID_STEP_MIN, q, q + GRID_STEP_MIN) if 0 <= c <= m_now] + [m_now]
    return float(min(cols, key=lambda c: (abs(c - raw), c != q)))


# ── Risk / Reward (§8.3) ────────────────────────────────────────────────────
def risk_reward(pick_or_legs, target_price: float, at_time_fraction: float = 0.5, *,
                spot: float, minutes_to_expiry_now: float, net_premium: float | None = None,
                is_credit: bool | None = None, max_loss: float | None = None,
                iv_by_leg: dict | None = None, r: float = R_DEFAULT) -> dict | None:
    """{cost, value, multiple, target, pnl, minutes_to_expiry, at} for one
    structure if spot is at `target_price` at the projected time.

    `pick_or_legs` is a scored candidate / pick (legs, net_premium, max_loss,
    type|spread_type) or a bare legs list (then pass net_premium, is_credit and
    optionally max_loss). Amounts in $ per structure; see module docstring."""
    if isinstance(pick_or_legs, dict):
        legs = pick_or_legs.get("legs") or []
        net = pick_or_legs.get("net_premium") if net_premium is None else net_premium
        kind = pick_or_legs.get("spread_type") or pick_or_legs.get("type") or "debit"
        credit = (kind == "credit") if is_credit is None else is_credit
        ml = pick_or_legs.get("max_loss") if max_loss is None else max_loss
    else:
        legs, net, credit, ml = list(pick_or_legs or []), net_premium, bool(is_credit), max_loss
    if not legs or net is None or target_price is None:
        return None
    net = abs(float(net))
    if ml is None:
        ml = net if not credit else None
    if ml is None or ml <= 0:
        return None
    m_at = projected_minutes(minutes_to_expiry_now, at_time_fraction)
    cell = grid_cell(legs, net, credit, spot, minutes_to_expiry_now, float(target_price), m_at,
                     iv_by_leg, r)
    if cell is None:
        return None
    pnl_pts = (net - cell) if credit else (cell - net)
    cost = float(ml) * MULTIPLIER
    pnl = pnl_pts * MULTIPLIER
    value = cost + pnl
    return {"cost": round(cost, 2), "value": round(value, 2),
            "multiple": round(value / cost, 2) if cost > 0 else None,
            "target": float(target_price), "pnl": round(pnl, 2),
            "minutes_to_expiry": m_at, "at_time_fraction": at_time_fraction}
