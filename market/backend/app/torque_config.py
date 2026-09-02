"""Torque ticker + strategy configuration.

Everything the frontend needs to know about *which* strikes to auto-fill lives
here, server-side — the page never hardcodes offsets. A JSON file at the repo
root (`torque_tickers.json`, or $TORQUE_TICKERS_CONFIG) is deep-merged over
these defaults so the offsets can be tuned without touching code.

Anchoring model (all distances are in index/underlying POINTS, snapped to the
live chain grid by the engine):

  single     long single option, `offset` points OTM from spot (0 = ATM).
  vertical   near leg `anchor` points from spot (OTM direction of the side);
             far leg `width` points beyond the near leg. For DEBIT verticals
             the near leg is the LONG; for CREDIT verticals it's the SHORT.
  condor     short put `short_offset` below spot / short call above; each
             protected by a `width`-point wing.
  iron_fly   short put + short call at ATM; wings `width` points out.
  butterfly  body at spot+`body_offset`; wings `wing` points on each side
             (buy 1 / sell 2 / buy 1).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ── Strategy registry ──────────────────────────────────────────────────────
# Order here is the display order in the strategy selector. Default = bull_call.
STRATEGY_DEFS: dict[str, dict[str, Any]] = {
    # "dir" (bull/bear) is the strategy's OWN structural bias — used by the frontend's
    # Counter Read confirms/diverging check (compared against real spot trend, not
    # data-derived). Every strategy needs it, not just the verticals: a long call is
    # just as structurally bullish as a bull call spread, it just used to be missing
    # here — leaving Counter Read's tag silently dark on Long Call (the DEFAULT
    # strategy), Long Put, Call Fly, and Put Fly.
    "long_call":   {"name": "Long Call",   "short": "Long Call",   "kind": "single",    "type": "debit",  "side": "call", "dir": "bull", "legs": 1},
    "long_put":    {"name": "Long Put",    "short": "Long Put",    "kind": "single",    "type": "debit",  "side": "put",  "dir": "bear", "legs": 1},
    "bull_call":   {"name": "Bull Call",   "short": "Bull Call",   "kind": "vertical",  "type": "debit",  "side": "call", "dir": "bull", "legs": 2},
    "bear_put":    {"name": "Bear Put",    "short": "Bear Put",    "kind": "vertical",  "type": "debit",  "side": "put",  "dir": "bear", "legs": 2},
    "bull_put":    {"name": "Bull Put",    "short": "Bull Put",    "kind": "vertical",  "type": "credit", "side": "put",  "dir": "bull", "legs": 2},
    "bear_call":   {"name": "Bear Call",   "short": "Bear Call",   "kind": "vertical",  "type": "credit", "side": "call", "dir": "bear", "legs": 2},
    "iron_condor": {"name": "Iron Condor", "short": "Iron Condor", "kind": "condor",    "type": "credit", "side": "both", "legs": 4},
    "iron_fly":    {"name": "Iron Fly",    "short": "Iron Fly",    "kind": "iron_fly",  "type": "credit", "side": "both", "legs": 4},
    "call_fly":    {"name": "Call Butterfly", "short": "Call Fly", "kind": "butterfly", "type": "debit",  "side": "call", "dir": "bull", "legs": 3},
    "put_fly":     {"name": "Put Butterfly",  "short": "Put Fly",  "kind": "butterfly", "type": "debit",  "side": "put",  "dir": "bear", "legs": 3},
}

# Each directional single/vertical/butterfly strategy has a natural opposite —
# same "shape" (single/vertical/butterfly), independently-configured strikes on
# the other side (NOT a mirror at the same strikes, which would be arbitrage-
# locked by put-call parity and carry no separate signal). Used by /torque/build
# to also return the counter structure's pricing for a live relative-richness
# read. Iron Condor/Iron Fly have no entry — both wings are already in their own
# `legs`, so that comparison is done client-side from the single response.
COUNTER_STRATEGY: dict[str, str] = {
    "long_call": "long_put", "long_put": "long_call",
    "bull_call": "bear_put", "bear_put": "bull_call",
    "bull_put": "bear_call", "bear_call": "bull_put",
    "call_fly": "put_fly",   "put_fly": "call_fly",
}

DEFAULT_STRATEGY = "bull_call"

# ── Per-ticker offsets ─────────────────────────────────────────────────────
# `tick` is the option price increment used for rounding limit prices.
_BASE_OFFSETS: dict[str, Any] = {
    "tick": 0.05,
    "single":    {"offset": 0},
    "vertical":  {"anchor": 0,  "width": 50},
    "condor":    {"short_offset": 50, "width": 50},
    "iron_fly":  {"width": 50},
    "butterfly": {"body_offset": 0, "wing": 50},
}

# First entry = the default ticker (NDX), pre-selected on load.
TICKERS: list[str] = ["NDX", "SPX", "RUT", "SPY", "QQQ", "DJX"]

# Yahoo Finance chart symbols for the live spot quote (indices need the ^ form;
# ETFs are the bare ticker). Used as the primary spot source, with Tradier
# put-call parity as fallback. A ticker absent here just uses the fallback.
# ^DJX already quotes at the 1/100 DJIA scale (~525) that the option strike grid
# uses, so it needs no rescaling — do NOT substitute ^DJI (~52,550).
YAHOO_SYMBOLS: dict[str, str] = {
    "NDX": "^NDX", "SPX": "^GSPC", "RUT": "^RUT", "SPY": "SPY", "QQQ": "QQQ",
    "DJX": "^DJX",
}

# ── Root allowlist ─────────────────────────────────────────────────────────
# Tradier serves option chains only by BASE symbol, returning every root listed
# for that expiration mixed together. For most indices the generic dedup in
# torque_engine._keep_primary_root picks the live root. DJX needs an explicit
# allowlist: the AM-settled monthly `DJX` root and the daily/weekly `DJXW` root
# never coexist except on the monthly expiration itself, and we trade DJXW only.
# An expiration whose chain has no allowed root is skipped (see choose_expiration).
TICKER_ROOTS: dict[str, list[str]] = {
    "DJX": ["DJXW"],
}


def allowed_roots(ticker: str) -> list[str]:
    """Roots we're willing to trade for `ticker`. Empty list = no restriction."""
    f = _load_overrides_file().get("roots", {}).get((ticker or "").upper())
    if isinstance(f, list):
        return [str(r).upper() for r in f]
    return list(TICKER_ROOTS.get((ticker or "").upper(), []))


def yahoo_symbol(ticker: str) -> str | None:
    return YAHOO_SYMBOLS.get((ticker or "").upper())

_TICKER_OVERRIDES: dict[str, dict[str, Any]] = {
    "NDX": {
        "tick": 0.05,
        "vertical":  {"anchor": 20, "width": 100},   # long 20pts off spot, short 100 beyond
        "condor":    {"short_offset": 100, "width": 100},
        "iron_fly":  {"width": 100},
        "butterfly": {"body_offset": 0, "wing": 100},
    },
    "SPX": {   # ~7490, 5pt grid
        "tick": 0.05,
        "vertical":  {"anchor": 5, "width": 25},
        "condor":    {"short_offset": 25, "width": 25},
        "iron_fly":  {"width": 25},
        "butterfly": {"body_offset": 0, "wing": 25},
    },
    "RUT": {   # ~2960, 5pt grid
        "tick": 0.05,
        "vertical":  {"anchor": 5, "width": 20},
        "condor":    {"short_offset": 20, "width": 20},
        "iron_fly":  {"width": 20},
        "butterfly": {"body_offset": 0, "wing": 20},
    },
    "SPY": {   # ~746, 1pt grid
        "tick": 0.01,
        "vertical":  {"anchor": 1, "width": 5},
        "condor":    {"short_offset": 5, "width": 5},
        "iron_fly":  {"width": 5},
        "butterfly": {"body_offset": 0, "wing": 5},
    },
    "QQQ": {   # ~738, 1pt grid
        "tick": 0.01,
        "vertical":  {"anchor": 1, "width": 5},
        "condor":    {"short_offset": 5, "width": 5},
        "iron_fly":  {"width": 5},
        "butterfly": {"body_offset": 0, "wing": 5},
    },
    # DJX (DJXW root) ~525, 1pt grid, PENNY-quoted (bids like 2.68/1.41 are not
    # 0.05 multiples, so tick=0.01). Widths are deliberately wider than the
    # 1pt grid would suggest: DJX is 1/100 of the Dow, so its option premiums
    # are ~1/12 of SPX's while market makers still quote a ~$0.30 wide market.
    # A 2-3pt vertical therefore has a package spread of 47-60% of its own mid
    # and cannot be closed profitably; at width 10 that falls to ~16-20%.
    "DJX": {
        "tick": 0.01,
        "vertical":  {"anchor": 1, "width": 10},
        "condor":    {"short_offset": 10, "width": 10},
        "iron_fly":  {"width": 10},
        "butterfly": {"body_offset": 0, "wing": 10},
    },
}

DEFAULT_CLOSE_TARGET_PCT = 30.0   # +30% profit target for the auto-close order

# ── Auto-close profit targets ──────────────────────────────────────────────
# The close order is a limit at entry*(1±pct). For it to ever fill, `pct` must
# exceed the round-trip cost of crossing the package bid/ask — otherwise the
# target price sits inside the spread and the position can only be closed at a
# loss. On SPX/NDX the package spread is ~3-4% of mid, so the 30% default has
# huge headroom. On DJX it is 16-43% of mid (measured live), so 30% is BELOW
# the round-trip cost for tighter widths. Hence a per-ticker floor that the API
# clamps UP to — a user cannot select a target that is structurally unfillable.
#   "default" = pre-filled in the UI, "min" = hard floor enforced server-side.
#   "spread_scaled": also raise the floor to the LIVE package spread at place
#   time. Opt-in per ticker: on tight names the static floor already clears the
#   spread by 10x, and scaling there would silently move long-standing SPX/NDX
#   close targets. DJX needs it because its spread% swings 16% → 800% intraday
#   as premium decays.
#   "required": auto-close cannot be turned OFF for this ticker. The user may
#   raise the profit target but never disarm the exit. Consequence: when the
#   package is wider than MAX_AUTO_CLOSE_SPREAD_PCT the close cannot be armed,
#   so the ENTRY is refused too — a mandatory exit means no exit, no trade.
CLOSE_TARGETS: dict[str, dict[str, float]] = {
    "DJX": {"default": 60.0, "min": 45.0, "spread_scaled": True, "required": True},
}

# A package whose own bid/ask is this wide relative to its mid cannot support ANY
# profit target — the close limit would sit inside the spread forever. Observed on
# DJX 0DTE once premium decays to a few cents (net_mid 0.02, net_bid -0.06). We
# refuse to arm auto-close rather than place an entry whose exit can never fill.
MAX_AUTO_CLOSE_SPREAD_PCT = 100.0

# ── Stop-loss (app-managed) ────────────────────────────────────────────────
# The profit-target close is a PASSIVE resting limit: it never crosses, so it
# never pays the exit half-spread. A stop is the opposite — it fires when the
# trade is going wrong and must CROSS to get out, paying the half-spread it
# would otherwise have avoided. On DJX that exit half can be ~90% of the
# position's mid, so:
#   * the trigger is measured at MID (fair value), never at the bid — measuring
#     at the bid would fire instantly, since a wide package is underwater by
#     half the spread the moment it fills;
#   * if the book is wider than STOP_MAX_EXIT_SPREAD_PCT at trigger time we do
#     NOT dump into it. Crossing a 180%-of-mid market hands back more than the
#     stop was meant to save. The watcher flags `stop_blocked_wide_market` and
#     keeps polling for a sane book instead.
STOP_LOSS: dict[str, dict[str, float]] = {
    "DJX": {"default": 50.0},
}
STOP_MAX_EXIT_SPREAD_PCT = 60.0

# ── Market orders ──────────────────────────────────────────────────────────
# A market order crosses to whatever the book shows. On a penny-wide name that
# costs a cent; on DJX the ask can be 10.00 against a 5.22 mid, so a market
# entry silently pays ~2x fair value. There is no legitimate reason to send a
# market order into that book — force a limit and let the user see the price.
NO_MARKET_ORDER_TICKERS: set[str] = {"DJX"}


def market_orders_allowed(ticker: str) -> bool:
    f = _load_overrides_file().get("no_market_order_tickers")
    banned = {str(t).upper() for t in f} if isinstance(f, list) else NO_MARKET_ORDER_TICKERS
    return (ticker or "").upper() not in banned


# ── Execution fees ─────────────────────────────────────────────────────────
# Commission is charged PER CONTRACT PER LEG, both opening and closing. Values
# below were read off Tradier's own order preview (`commission` field) against a
# live production account — identical in sandbox:
#     DJX  buy 1 contract  -> 0.53      DJX  buy 10 -> 5.30   (linear)
#     DJX  2-leg vertical  -> 1.06      (= 2 legs x 0.53)
#     SPX  buy 1 contract  -> 0.95
# Index options carry a proprietary exchange fee, hence DJX/SPX > the plain
# equity-option rate. A ticker not listed here uses DEFAULT_COMMISSION.
#
# `extra_fee_per_contract` covers regulatory pass-throughs (ORF, TAF) which
# Tradier's preview reports as fees=0 because they are assessed at settlement,
# not at order time. It defaults to 0.0 — we do NOT invent a number. Set it in
# torque_tickers.json if you want the target to absorb them too.
DEFAULT_COMMISSION = 0.35
COMMISSION_PER_CONTRACT: dict[str, float] = {
    "DJX": 0.53,
    "SPX": 0.95,
    "NDX": 0.95,
    "RUT": 0.95,
}
DEFAULT_EXTRA_FEE = 0.0


def commission_per_contract(ticker: str) -> float:
    f = _load_overrides_file().get("commissions", {})
    tk = (ticker or "").upper()
    if isinstance(f, dict) and tk in f:
        return float(f[tk])
    return float(COMMISSION_PER_CONTRACT.get(tk, DEFAULT_COMMISSION))


def extra_fee_per_contract(ticker: str) -> float:
    f = _load_overrides_file().get("extra_fees", {})
    tk = (ticker or "").upper()
    if isinstance(f, dict) and tk in f:
        return float(f[tk])
    return DEFAULT_EXTRA_FEE


def fee_per_contract(ticker: str) -> float:
    """All-in per-contract, per-leg, one-way execution cost."""
    return commission_per_contract(ticker) + extra_fee_per_contract(ticker)


def stop_loss_default(ticker: str) -> float | None:
    f = _load_overrides_file().get("stop_loss", {}).get((ticker or "").upper(), {})
    sl = {**STOP_LOSS.get((ticker or "").upper(), {}), **(f if isinstance(f, dict) else {})}
    d = sl.get("default")
    return float(d) if d is not None else None


def close_target_default(ticker: str) -> float:
    f = _load_overrides_file().get("close_targets", {}).get((ticker or "").upper(), {})
    ct = {**CLOSE_TARGETS.get((ticker or "").upper(), {}), **(f if isinstance(f, dict) else {})}
    return float(ct.get("default", DEFAULT_CLOSE_TARGET_PCT))


def close_target_min(ticker: str) -> float:
    """Hard floor for the auto-close profit target. Default 1.0 (= the API's
    existing lower bound) for tickers whose spreads are tight enough not to
    need one."""
    f = _load_overrides_file().get("close_targets", {}).get((ticker or "").upper(), {})
    ct = {**CLOSE_TARGETS.get((ticker or "").upper(), {}), **(f if isinstance(f, dict) else {})}
    return float(ct.get("min", 1.0))


def close_target_spread_scaled(ticker: str) -> bool:
    """True when the floor must also clear the live package bid/ask (DJX)."""
    f = _load_overrides_file().get("close_targets", {}).get((ticker or "").upper(), {})
    ct = {**CLOSE_TARGETS.get((ticker or "").upper(), {}), **(f if isinstance(f, dict) else {})}
    return bool(ct.get("spread_scaled", False))


def close_target_required(ticker: str) -> bool:
    """True when auto-close may not be disarmed for this ticker (DJX)."""
    f = _load_overrides_file().get("close_targets", {}).get((ticker or "").upper(), {})
    ct = {**CLOSE_TARGETS.get((ticker or "").upper(), {}), **(f if isinstance(f, dict) else {})}
    return bool(ct.get("required", False))


def close_targets_map() -> dict[str, dict[str, float]]:
    """Per-ticker {default,min,spread_scaled,required} for the frontend."""
    return {t: {"default": close_target_default(t), "min": close_target_min(t),
                "spread_scaled": close_target_spread_scaled(t),
                "required": close_target_required(t)}
            for t in tickers()}


# ── Counter Read noise floor ─────────────────────────────────────────────────
# Counter Read's richness ratio is `mid / width` — fine with real premium, but
# right at 0DTE close a structure's legs can collapse toward worthless together,
# and a penny-level difference between two near-zero mids (e.g. $0.03 vs $0.00)
# produces a 100%/0% ratio with no real conviction behind it. Below this floor,
# BOTH sides being under it means the reading is noise, not signal.
#
# One flat number is wrong in both directions across tickers: NDX/SPX/RUT trade
# in bigger dollar increments (a $0.05 mid there really can be near-worthless),
# while SPY/QQQ run smaller nominal premiums (a $0.50 mid there is still a real,
# meaningful price) — so the floor is sized per ticker, same pattern as
# COMMISSION_PER_CONTRACT / CLOSE_TARGETS above.
DEFAULT_RICHNESS_FLOOR = 0.03
RICHNESS_FLOOR: dict[str, float] = {
    "NDX": 0.05, "SPX": 0.05, "RUT": 0.05,
    "SPY": 0.02, "QQQ": 0.02, "DJX": 0.02,
}


def richness_floor(ticker: str) -> float:
    f = _load_overrides_file().get("richness_floor", {})
    tk = (ticker or "").upper()
    if isinstance(f, dict) and tk in f:
        return float(f[tk])
    return float(RICHNESS_FLOOR.get(tk, DEFAULT_RICHNESS_FLOOR))


def richness_floors_map() -> dict[str, float]:
    """Per-ticker Counter Read noise floor for the frontend."""
    return {t: richness_floor(t) for t in tickers()}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_overrides_file() -> dict[str, Any]:
    """Optional JSON override file. Shape:
       {"tickers":[...], "default_strategy":"...", "overrides":{"NDX":{...}}}"""
    path = os.environ.get("TORQUE_TICKERS_CONFIG")
    candidates = [Path(path)] if path else []
    # Canonical location: market/backend/torque/torque_tickers.json — a dedicated
    # folder for Torque config, alongside (not inside) the app/ package so it's
    # obviously config, not code. this file lives at market/backend/app/torque_config.py,
    # so parents[1] is market/backend.
    here = Path(__file__).resolve()
    candidates.append(here.parents[1] / "torque" / "torque_tickers.json")
    # Back-compat fallback: also walk up looking for a bare torque_tickers.json at
    # any ancestor (e.g. repo root), in case one was ever placed there directly.
    for parent in here.parents:
        candidates.append(parent / "torque_tickers.json")
    for p in candidates:
        try:
            if p and p.is_file():
                return json.loads(p.read_text())
        except Exception:
            continue
    return {}


def ticker_rule(ticker: str, strategy: str) -> dict[str, Any]:
    """Resolved offset rule for (ticker, strategy): base ⊕ ticker-override ⊕
    file-override, narrowed to the strategy's kind. Returns e.g.
    {"kind":"vertical","tick":0.05,"anchor":20,"width":100,"type":"debit","side":"call","dir":"bull"}.
    """
    sdef = STRATEGY_DEFS.get(strategy)
    if not sdef:
        raise KeyError(f"unknown strategy {strategy!r}")
    kind = sdef["kind"]
    tk = ticker.upper()

    merged = _deep_merge({}, _BASE_OFFSETS)
    merged = _deep_merge(merged, _TICKER_OVERRIDES.get(tk, {}))
    file_over = _load_overrides_file().get("overrides", {}).get(tk, {})
    merged = _deep_merge(merged, file_over)

    rule = dict(merged.get(kind) or {})
    rule["kind"] = kind
    rule["tick"] = float(merged.get("tick", 0.05))
    rule["type"] = sdef["type"]
    rule["side"] = sdef["side"]
    if "dir" in sdef:
        rule["dir"] = sdef["dir"]
    return rule


def tickers() -> list[str]:
    f = _load_overrides_file().get("tickers")
    return list(f) if isinstance(f, list) and f else list(TICKERS)


def default_strategy() -> str:
    return _load_overrides_file().get("default_strategy") or DEFAULT_STRATEGY


def operator_uids() -> list[str]:
    """Auth uid(s) (Supabase `profiles.id`) of the server operator(s) to auto-grant
    the full toolset on startup. Sourced from config — never hardcoded: the
    `TORQUE_OPERATOR_UIDS` env var (comma-separated) and/or an `operator_uids`
    array in torque_tickers.json. Empty by default (no seeding). Used by the
    backend to seed `profiles.tools_enabled` via service_role (see
    supabase_admin.grant_user_tools); the admin token bypasses the gate anyway."""
    out: list[str] = []
    env = os.environ.get("TORQUE_OPERATOR_UIDS", "")
    out.extend(u.strip() for u in env.split(",") if u.strip())
    f = _load_overrides_file().get("operator_uids")
    if isinstance(f, list):
        out.extend(str(u).strip() for u in f if str(u).strip())
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def strategy_list() -> list[dict[str, Any]]:
    """Public strategy registry for the frontend (key + display fields)."""
    return [{"key": k, **{kk: vv for kk, vv in v.items()}} for k, v in STRATEGY_DEFS.items()]
