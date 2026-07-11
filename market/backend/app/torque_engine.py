"""Torque order-construction engine — pure, network-free.

Given a spot + a normalized option chain + a (ticker, strategy) rule, it:
  * auto-fills the legs/strikes (snapped to the live chain grid),
  * prices the spread from bid/ask (net bid / mid / ask, debit or credit),
  * builds the +30% profit-target close legs,
  * computes a real-time directional *lean* (premium skew + OI/volume
    concentration) — an honest heuristic, NOT a prediction.

All strike selection snaps to strikes that actually exist in the chain, so the
caller can step a leg up/down by whole grid increments.
"""
from __future__ import annotations

import math

from datetime import date, datetime, timezone
from typing import Any

from .order_builder import Leg
from .torque_config import ticker_rule, STRATEGY_DEFS, allowed_roots


# ── chain helpers ──────────────────────────────────────────────────────────
def normalize_chain(raw: list[dict], ticker: str | None = None) -> list[dict]:
    """Map raw Tradier option rows → {symbol, strike, side, bid, ask, mid,
    last, open_interest, volume, iv, delta}. Drops rows without a strike.

    Index underlyings list multiple roots at the same strike — e.g. the
    AM-settled monthly `NDX` (stale, quotes hours old) and the PM-settled 0DTE
    `NDXP` (live). When two rows share (side, strike) we keep the one with the
    freshest quote timestamp (bid_date/ask_date/trade_date, epoch ms), so the
    live root wins generically (NDX→NDXP, SPX→SPXW) without hardcoding roots.
    Tie-break: higher volume, then open interest, then first seen.

    When `ticker` has an explicit root allowlist (torque_config.TICKER_ROOTS —
    today only DJX→DJXW), rows from any other root are dropped up front rather
    than being left to the generic dedup. Returns [] if the expiration lists no
    allowed root at all, which is the caller's signal to try the next one."""
    allow = {r.upper() for r in allowed_roots(ticker)} if ticker else set()
    best: dict[tuple[str, float], dict] = {}
    for o in raw or []:
        if allow and str(o.get("root_symbol") or "").upper() not in allow:
            continue
        try:
            strike = float(o.get("strike"))
        except (TypeError, ValueError):
            continue
        otype = (o.get("option_type") or o.get("side") or "").lower()
        side = "call" if otype.startswith("c") else "put" if otype.startswith("p") else None
        if side is None:
            continue
        bid = _f(o.get("bid")); ask = _f(o.get("ask"))
        greeks = o.get("greeks") or {}
        qts = max(_f(o.get("bid_date")) or 0.0,
                  _f(o.get("ask_date")) or 0.0,
                  _f(o.get("trade_date")) or 0.0)
        vol = int(_f(o.get("volume")) or 0)
        oi = int(_f(o.get("open_interest")) or 0)
        two_sided = bid is not None and ask is not None
        # tightness: live MM quotes are tight; stale hours-old quotes are wide.
        # smaller is better → negate so bigger key = better.
        tight = -((ask - bid)) if two_sided else float("-inf")
        # DEDUP PRIORITY (must be STABLE — Tradier's bid_date flip-flops, so it
        # can NOT be primary or the live↔stale root flickers every few seconds):
        #   1. two-sided quote present
        #   2. VOLUME — the live 0DTE root trades; the stale AM-settled root is 0
        #   3. tighter spread
        #   4. freshest timestamp (last-resort tie-break only)
        # open_interest is deliberately EXCLUDED: the stale monthly root often
        # has HIGHER OI, which would select the wrong (stale) quotes.
        rank = (1 if two_sided else 0, vol, tight, qts)
        row = {
            "symbol": o.get("symbol"),
            "strike": strike,
            "side": side,
            "bid": bid,
            "ask": ask,
            "mid": round((bid + ask) / 2, 4) if two_sided else None,
            "last": _f(o.get("last")),
            "open_interest": oi,
            "volume": vol,
            "iv": _f(greeks.get("mid_iv") or greeks.get("smv_vol")),
            "delta": _f(greeks.get("delta")),
            "_rank": rank, "_root": o.get("root_symbol"),
        }
        key = (side, strike)
        cur = best.get(key)
        if cur is None or rank > cur["_rank"]:
            best[key] = row
    out = _keep_primary_root(list(best.values()))
    for r in out:
        r.pop("_rank", None)
    return out


# PM-settled weekly roots that ARE the live 0DTE listing for an index. Tradier
# only serves option chains by the BASE symbol (NDX) — querying NDXP/SPXW/RUTW
# returns empty — so the chain always comes back with both the live weekly root
# and the stale AM-settled monthly root mixed together. We translate NDX→NDXP
# (etc.) by keeping only the live root. Preferring a KNOWN weekly root makes the
# pick deterministic even pre-market when volume is still 0.
KNOWN_LIVE_ROOTS = {"NDXP", "NDXW", "SPXW", "SPXPM", "RUTW", "XSP", "VIXW", "DJXW"}


def _keep_primary_root(rows: list[dict]) -> list[dict]:
    """Keep ONLY the live root so the tradeable grid is exactly the live listing
    and the auto-anchor can't snap onto a stale strike the live root doesn't
    list (e.g. NDX 30425 when NDXP steps 30420/30430). Root preference:
    KNOWN weekly/PM root → most total volume → most strikes listed. No-op when
    there's a single root (ETFs, tests)."""
    roots = {r.get("_root") for r in rows if r.get("_root")}
    if len(roots) < 2:
        return rows
    vol_by_root: dict[str, int] = {}
    cnt_by_root: dict[str, int] = {}
    for r in rows:
        rt = r.get("_root")
        if not rt:
            continue
        vol_by_root[rt] = vol_by_root.get(rt, 0) + (r.get("volume") or 0)
        cnt_by_root[rt] = cnt_by_root.get(rt, 0) + 1
    primary = max(roots, key=lambda rt: (rt in KNOWN_LIVE_ROOTS,
                                         vol_by_root.get(rt, 0),
                                         cnt_by_root.get(rt, 0)))
    return [r for r in rows if r.get("_root") in (primary, None)]


def choose_expiration(expirations: list[str]) -> str | None:
    """0DTE if today is listed, else the nearest future expiration."""
    cands = expiration_candidates(expirations)
    return cands[0] if cands else None


def expiration_candidates(expirations: list[str], limit: int = 4) -> list[str]:
    """Expirations in the order Torque should try them: 0DTE first, then
    nearest-future ascending. More than one is returned because a ticker with a
    root allowlist (DJX→DJXW) may have expirations that list *only* the excluded
    root — DJX's monthly 3rd-Friday dates carry the AM-settled `DJX` root and no
    `DJXW` — and those must be skipped rather than traded."""
    if not expirations:
        return []
    today = datetime.now(timezone.utc).date().isoformat()
    future = sorted(e for e in expirations if e >= today)
    if not future:
        return [sorted(expirations)[-1]]
    return future[:limit]


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def implied_spot(contracts: list[dict], fallback: float | None = None) -> float | None:
    """Live underlying from put-call parity at the ATM strike:
        spot ≈ call_mid − put_mid + strike
    Option bid/ask are real-time, so this tracks the true market even when the
    broker's derived index quote lags. ATM = strike with the smallest
    |call_mid − put_mid| (also rejects deep-ITM junk rows with one-sided
    markets). Returns `fallback` if no strike has a two-sided call+put."""
    cm: dict[float, float] = {}
    pm: dict[float, float] = {}
    for c in contracts or []:
        m = c.get("mid")
        if m is None or m <= 0:
            continue
        (cm if c["side"] == "call" else pm)[c["strike"]] = m
    common = set(cm) & set(pm)
    if not common:
        return fallback
    # ATM = smallest |call_mid − put_mid|; tie-break to the strike nearest the
    # broker quote (or, absent one, the grid centre) so a flat/degenerate skew
    # doesn't snap parity to an extreme strike.
    centre = fallback if fallback else sorted(common)[len(common) // 2]
    atm = min(common, key=lambda k: (round(abs(cm[k] - pm[k]), 4), abs(k - centre)))
    parity = cm[atm] - pm[atm] + atm
    # sanity: a sane ATM parity sits within a few % of the strike grid centre
    if fallback and abs(parity - fallback) > max(0.1 * fallback, 50):
        # raw quote disagrees wildly (stale/halted) — still prefer parity if it
        # lands inside the listed strike range, else fall back.
        ks = sorted(common)
        if not (ks[0] <= parity <= ks[-1]):
            return fallback
    return round(parity, 2)


def _grid(contracts: list[dict], side: str) -> list[float]:
    return sorted({c["strike"] for c in contracts if c["side"] == side})


def _nearest(grid: list[float], target: float) -> float | None:
    if not grid:
        return None
    return min(grid, key=lambda s: abs(s - target))


def _step(grid: list[float], strike: float, n: int) -> float:
    """Move `n` grid positions from `strike` (clamped to the grid ends)."""
    if not grid or strike not in grid:
        if grid:
            strike = _nearest(grid, strike)
        else:
            return strike
    i = grid.index(strike)
    j = max(0, min(len(grid) - 1, i + n))
    return grid[j]


def _by_strike(contracts: list[dict], side: str, strike: float) -> dict | None:
    for c in contracts:
        if c["side"] == side and c["strike"] == strike:
            return c
    return None


# ── structure builder ──────────────────────────────────────────────────────
def build_structure(spot: float, contracts: list[dict], ticker: str, strategy: str,
                    adjustments: dict[str, int] | None = None) -> dict:
    """Auto-fill legs for (ticker, strategy). `adjustments` maps a leg role
    (e.g. 'long','short','short_put','long_call','body') to a signed integer
    number of grid steps the user nudged that leg.

    Returns {strategy, type, legs:[{role, side, strike, action, quantity,
    symbol}], rule, grid_step}. Each leg.symbol is the canonical OCC pulled
    straight from the chain (None if that strike has no listed contract).
    """
    adj = adjustments or {}
    rule = ticker_rule(ticker, strategy)
    kind = rule["kind"]
    sdef = STRATEGY_DEFS[strategy]
    legs: list[dict] = []

    def mk(role: str, side: str, target: float, long_short: int, base: float | None = None):
        grid = _grid(contracts, side)
        anchor = base if base is not None else target
        strike = _nearest(grid, anchor)
        if strike is None:
            return None
        if adj.get(role):
            strike = _step(grid, strike, int(adj[role]))
        c = _by_strike(contracts, side, strike)
        action, qty = _action_qty(long_short)
        legs.append({
            "role": role, "side": side, "strike": strike,
            "action": action, "quantity": qty, "long_short": long_short,
            "symbol": (c or {}).get("symbol"),
        })
        return strike

    if kind == "single":
        side = rule["side"]
        off = float(rule.get("offset", 0))
        target = spot + off if side == "call" else spot - off
        mk("long", side, target, +1)

    elif kind == "vertical":
        side = rule["side"]
        anchor = float(rule.get("anchor", 0))
        width = float(rule.get("width", 50))
        is_debit = rule["type"] == "debit"
        up = (side == "call")          # calls go OTM upward, puts downward
        sgn = 1 if up else -1
        near = spot + sgn * anchor
        near_ls = +1 if is_debit else -1       # debit: near=long; credit: near=short
        near_strike = mk("near", side, near, near_ls)
        if near_strike is not None:
            far = near_strike + sgn * width
            mk("far", side, far, -near_ls)

    elif kind == "condor":
        so = float(rule.get("short_offset", 50))
        width = float(rule.get("width", 50))
        sp = mk("short_put", "put", spot - so, -1)
        if sp is not None:
            mk("long_put", "put", sp - width, +1)
        sc = mk("short_call", "call", spot + so, -1)
        if sc is not None:
            mk("long_call", "call", sc + width, +1)

    elif kind == "iron_fly":
        width = float(rule.get("width", 50))
        sp = mk("short_put", "put", spot, -1)
        if sp is not None:
            mk("long_put", "put", sp - width, +1)
        sc = mk("short_call", "call", spot, -1)
        if sc is not None:
            mk("long_call", "call", sc + width, +1)

    elif kind == "butterfly":
        side = rule["side"]
        body_off = float(rule.get("body_offset", 0))
        wing = float(rule.get("wing", 50))
        body = mk("body", side, spot + body_off, -2)
        if body is not None:
            mk("low", side, body - wing, +1)
            mk("high", side, body + wing, +1)

    return {
        "strategy": strategy,
        "name": sdef["name"],
        "type": rule["type"],
        "side": rule["side"],
        "legs": legs,
        "rule": rule,
    }


def _action_qty(long_short: int) -> tuple[str, int]:
    if long_short >= 1:
        return ("buy_to_open", long_short)
    return ("sell_to_open", abs(long_short))


# ── pricing ────────────────────────────────────────────────────────────────
def price_structure(legs: list[dict], px: dict[str, dict]) -> dict:
    """Net spread price from a {symbol: {bid, ask}} map. Convention: positive =
    net DEBIT (you pay), negative = net CREDIT (you receive). Returns net bid /
    mid / ask plus per-leg prices. `bid`/`ask` are the marketable extremes of
    the spread, `mid` the fair value."""
    net_mid = net_bid = net_ask = 0.0
    leg_px: list[dict] = []
    complete = True
    for lg in legs:
        sym = lg.get("symbol")
        q = px.get(sym) if sym else None
        bid = _f((q or {}).get("bid"))
        ask = _f((q or {}).get("ask"))
        qty = int(lg.get("quantity", 1))
        buy = lg["action"].startswith("buy")
        if bid is None or ask is None:
            complete = False
            leg_px.append({**_leg_brief(lg), "bid": bid, "ask": ask, "mid": None})
            continue
        mid = (bid + ask) / 2
        sign = 1 if buy else -1
        net_mid += sign * qty * mid
        # buying pays ask / receives bid on shorts at the worst; vice-versa
        net_ask += qty * (ask if buy else -bid)   # most you'd pay (debit) / least credit
        net_bid += qty * (bid if buy else -ask)    # least you'd pay / most credit
        leg_px.append({**_leg_brief(lg), "bid": bid, "ask": ask, "mid": round(mid, 4)})
    ptype = "debit" if net_mid >= 0 else "credit"
    if not complete:
        # A partial net (one leg priced, the other missing) is worse than no
        # number — it reads as a real spread price but is a single leg. Never
        # emit it; the UI keeps its last complete price instead.
        return {"type": ptype, "net_mid": None, "net_bid": None, "net_ask": None,
                "abs_mid": None, "abs_bid": None, "abs_ask": None,
                "complete": False, "legs": leg_px}
    return {
        "type": ptype,
        "net_mid": round(net_mid, 4),
        "net_bid": round(net_bid, 4),
        "net_ask": round(net_ask, 4),
        "abs_mid": round(abs(net_mid), 4),
        "abs_bid": round(abs(net_bid), 4),
        "abs_ask": round(abs(net_ask), 4),
        "complete": complete,
        "legs": leg_px,
    }


def _leg_brief(lg: dict) -> dict:
    return {"role": lg.get("role"), "side": lg.get("side"), "strike": lg.get("strike"),
            "action": lg.get("action"), "quantity": lg.get("quantity"), "symbol": lg.get("symbol")}


def round_to_tick(price: float, tick: float) -> float:
    if not tick:
        return round(price, 2)
    return round(round(price / tick) * tick, 4)


def suggested_limit(price: dict, tick: float) -> float:
    return round_to_tick(price.get("abs_mid") or 0.0, tick)


# ── auto-close (+30% target) ───────────────────────────────────────────────
def package_spread_pct(px: dict) -> float | None:
    """Width of the SPREAD's own bid/ask as a % of its mid — the round-trip cost
    of getting in and out. None when the package has no complete two-sided price.

    This, not the per-leg spread, is what an auto-close target must clear: a
    profit target below this number prices the close order inside the package's
    own bid/ask, so it can never fill. SPX/NDX run ~3-4%; DJX runs 16-45% on
    healthy widths and blows past 100% once 0DTE premium decays to a few cents."""
    if not px.get("complete"):
        return None
    mid = px.get("abs_mid")
    nb, na = px.get("net_bid"), px.get("net_ask")
    if not mid or nb is None or na is None:
        return None
    return abs(na - nb) / mid * 100.0


def stop_breached(mark: float, entry_fill: float, order_type: str, stop_pct: float) -> bool:
    """Has the position lost `stop_pct`% of the entry fill?

    `mark` is the package's ABSOLUTE mid (fair value), never the bid — a wide
    package is underwater by half its spread the instant it fills, so a bid-based
    stop would fire immediately on DJX.

      debit  → you paid `entry_fill`; you're down when the mark falls.
      credit → you received `entry_fill`; you're down when the buy-back cost rises.
    """
    if not entry_fill or mark is None or stop_pct is None:
        return False
    f = float(stop_pct) / 100.0
    e = abs(float(entry_fill))
    if order_type == "credit":
        return abs(mark) >= e * (1.0 + f)
    return abs(mark) <= e * (1.0 - f)


def stop_exit_price(px: dict, order_type: str, tick: float) -> float | None:
    """Marketable limit to GET OUT — we cross, but with a bounded price so a
    garbage quote can't fill us at absurd levels. debit → sell at the package's
    net bid; credit → buy back at its net ask. None when the package has no
    complete two-sided price."""
    if not px.get("complete"):
        return None
    raw = px.get("net_ask") if order_type == "credit" else px.get("net_bid")
    if raw is None:
        return None
    return round_to_tick(abs(float(raw)), tick)


def _ceil_tick(price: float, tick: float) -> float:
    if not tick:
        return round(price, 2)
    return round(math.ceil(round(price / tick, 9)) * tick, 4)


def _floor_tick(price: float, tick: float) -> float:
    if not tick:
        return round(price, 2)
    return round(math.floor(round(price / tick, 9)) * tick, 4)


def round_trip_fee_price(ticker_fee: float, legs: int, multiplier: int = 100) -> float:
    """Both-ways execution cost expressed in PREMIUM units (per 1 spread).

    Commission is per contract PER LEG, charged on the open and again on the
    close. Because both the fee and the premium scale with quantity, the
    per-unit fee is independent of how many contracts you trade:
        total = 2 * legs * qty * fee      premium = price * multiplier * qty
        fee_in_price = total / (multiplier * qty) = 2 * legs * fee / multiplier
    DJX single leg: 2*1*0.53/100 = 0.0106 (~1 tick). Vertical: 0.0212.
    """
    return 2.0 * int(legs) * float(ticker_fee) / float(multiplier)


def close_target_price(entry_price: float, order_type: str, pct: float, tick: float,
                       *, legs: int = 1, fee_per_contract: float = 0.0,
                       multiplier: int = 100) -> float:
    """Profit-target close price that nets `pct`% AFTER execution fees.

    Debit  → you paid `entry_price` plus the opening fee, and the closing fee is
             deducted from your sale proceeds, so you must sell for MORE than the
             naive entry*(1+pct).
    Credit → you collected `entry_price` minus the opening fee, and buying back
             costs the closing fee too, so you must buy back for LESS.

    With fee_per_contract=0 this reduces exactly to the old entry*(1±pct).
    Rounding is directional — debit targets round UP, credit targets round DOWN —
    so tick rounding can never drag the target back below true breakeven.
    """
    f = pct / 100.0
    one_way = int(legs) * float(fee_per_contract) / float(multiplier)   # per premium unit
    if order_type == "debit":
        cost = entry_price + one_way            # what the position really cost you
        return _ceil_tick(cost * (1 + f) + one_way, tick)
    credit = entry_price - one_way              # what you really collected
    price = _floor_tick(credit * (1 - f) - one_way, tick)
    # A credit buy-to-close limit MUST be a positive price. At pct≥~100 (or with
    # fees, slightly under) the formula goes ≤0 — Tradier rejects a non-positive
    # limit, which would silently unarm the auto-close. Floor it at one tick: the
    # cheapest real buy-back, i.e. "keep essentially all the credit". Callers that
    # want the target reported honestly should also cap pct (see max_credit_pct).
    return max(price, tick if tick else 0.01)


def max_credit_pct(entry_credit: float, tick: float, *, legs: int = 1,
                   fee_per_contract: float = 0.0, multiplier: int = 100) -> float:
    """Largest close-target pct for a CREDIT whose buy-back limit is still ≥ one
    tick (a fillable, positive price). Above this the position can only be closed
    by letting it expire, not by a resting limit. Returns a value in [1, 99]."""
    one_way = int(legs) * float(fee_per_contract) / float(multiplier)
    credit = float(entry_credit) - one_way
    floor_px = tick if tick else 0.01
    if credit <= floor_px:                      # credit already ≤ the min buy-back
        return 1.0
    f = 1.0 - (floor_px + one_way) / credit     # price(f) == floor_px
    return max(1.0, min(99.0, round(f * 100.0, 2)))


def breakeven_close_price(entry_price: float, order_type: str, tick: float,
                          *, legs: int = 1, fee_per_contract: float = 0.0,
                          multiplier: int = 100) -> float:
    """The close price at which the round trip nets exactly zero (pct=0)."""
    return close_target_price(entry_price, order_type, 0.0, tick,
                              legs=legs, fee_per_contract=fee_per_contract,
                              multiplier=multiplier)


def build_close_legs(legs: list[dict]) -> list[dict]:
    """Flip each opening leg to its closing counterpart (buy_to_open→
    sell_to_close, sell_to_open→buy_to_close), preserving qty/symbol."""
    flip = {"buy_to_open": "sell_to_close", "sell_to_open": "buy_to_close"}
    out = []
    for lg in legs:
        out.append({**lg, "action": flip.get(lg["action"], lg["action"])})
    return out


def legs_to_order_legs(legs: list[dict], contracts_qty: int = 1) -> list[Leg]:
    """Convert engine leg dicts → order_builder.Leg (qty × contracts)."""
    out: list[Leg] = []
    for lg in legs:
        out.append(Leg(
            side=lg["side"], strike=float(lg["strike"]), action=lg["action"],
            quantity=int(lg.get("quantity", 1)) * int(contracts_qty),
            occ_symbol=lg.get("symbol"),
        ))
    return out


# ── directional lean (heuristic) ───────────────────────────────────────────
def analyze(spot: float, contracts: list[dict], band_pct: float = 3.0) -> dict:
    """Real-time directional lean from premium skew + OI/volume concentration.

    Honest heuristic only — options positioning reflects hedging demand and
    dealer inventory as much as any directional view. Returns a signed lean
    (-100 bearish … +100 bullish), a suggested single-leg side, the component
    signals, and the key OI/volume strikes — so the UI can show the why.
    """
    lo, hi = spot * (1 - band_pct / 100), spot * (1 + band_pct / 100)
    band = [c for c in contracts if lo <= c["strike"] <= hi]
    calls = [c for c in band if c["side"] == "call"]
    puts = [c for c in band if c["side"] == "put"]

    call_vol = sum(c["volume"] for c in calls)
    put_vol = sum(c["volume"] for c in puts)
    call_prem = sum((c["mid"] or 0) * c["open_interest"] for c in calls)
    put_prem = sum((c["mid"] or 0) * c["open_interest"] for c in puts)
    # mid × VOLUME × 100 = premium $ actually traded today (volume is intraday and
    # cumulative, unlike OI which settles overnight) — feeds the live flow tracker.
    call_dollar = sum((c["mid"] or 0) * c["volume"] for c in calls) * 100
    put_dollar = sum((c["mid"] or 0) * c["volume"] for c in puts) * 100
    call_oi = sum(c["open_interest"] for c in calls)
    put_oi = sum(c["open_interest"] for c in puts)

    vol_sig = _ratio_sig(call_vol, put_vol)         # +bullish: call volume dominates
    skew_sig = _ratio_sig(call_prem, put_prem)      # +bullish: call premium richer
    # magnet = the most-concentrated combined-OI strike; it only pulls when OI
    # is genuinely concentrated (a flat OI profile must NOT fabricate a lean —
    # otherwise argmax tie-breaks to an arbitrary strike). `strength` is the
    # top strike's share above the average, so uniform OI → strength 0 → no pull.
    oi_by_strike: dict[float, int] = {}
    for c in band:
        oi_by_strike[c["strike"]] = oi_by_strike.get(c["strike"], 0) + c["open_interest"]
    if oi_by_strike:
        max_oi = max(oi_by_strike.values())
        tied = [k for k, v in oi_by_strike.items() if v == max_oi]
        magnet = min(tied, key=lambda k: abs(k - spot))   # nearest spot among ties
        avg_oi = sum(oi_by_strike.values()) / len(oi_by_strike)
        strength = (max_oi - avg_oi) / max_oi if max_oi > 0 else 0.0
    else:
        magnet, strength = spot, 0.0
    dist = (magnet - spot) / spot if spot else 0
    prox = max(-1.0, min(1.0, dist / (band_pct / 100))) if spot else 0.0
    magnet_sig = prox * strength

    lean = 100 * (0.4 * vol_sig + 0.3 * skew_sig + 0.3 * magnet_sig)
    lean = max(-100.0, min(100.0, lean))

    if lean >= 25:
        label, side = "bullish", "call"
    elif lean <= -25:
        label, side = "bearish", "put"
    else:
        # inside the ±25 band there's no directional edge — don't fabricate a
        # CALL bias just because the score isn't negative.
        label, side = "neutral", None

    top_call_oi = max(calls, key=lambda c: c["open_interest"], default=None)
    top_put_oi = max(puts, key=lambda c: c["open_interest"], default=None)
    top_vol = max(band, key=lambda c: c["volume"], default=None)

    return {
        "spot": spot,
        "lean": round(lean, 1),
        "label": label,
        "suggested_side": side,
        "components": {
            "volume": {"signal": round(vol_sig, 3), "call": call_vol, "put": put_vol,
                       "pcr": round(put_vol / call_vol, 3) if call_vol else None,
                       "call_dollar": round(call_dollar), "put_dollar": round(put_dollar)},
            "premium_skew": {"signal": round(skew_sig, 3), "call_oi_premium": round(call_prem, 1),
                             "put_oi_premium": round(put_prem, 1)},
            "oi": {"call": call_oi, "put": put_oi,
                   "pcr": round(put_oi / call_oi, 3) if call_oi else None},
            "magnet": {"signal": round(magnet_sig, 3), "strike": magnet,
                       "dist_pct": round(dist * 100, 2), "strength": round(strength, 3)},
        },
        "levels": {
            "top_call_oi": _level(top_call_oi),
            "top_put_oi": _level(top_put_oi),
            "top_volume": _level(top_vol),
        },
        "disclaimer": "Heuristic lean from current positioning (premium skew + OI/volume). "
                      "Not a price prediction — options flow reflects hedging and dealer "
                      "inventory as much as direction.",
    }


def _ratio_sig(a: float, b: float) -> float:
    tot = (a or 0) + (b or 0)
    if tot <= 0:
        return 0.0
    return ((a or 0) - (b or 0)) / tot


def _level(c: dict | None) -> dict | None:
    if not c:
        return None
    return {"strike": c["strike"], "side": c["side"],
            "open_interest": c["open_interest"], "volume": c["volume"], "mid": c["mid"]}
