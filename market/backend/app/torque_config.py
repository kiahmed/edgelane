"""Torque ticker + strategy configuration.

Strategy shapes, global defaults and safety constants live here in code. Every
PER-TICKER setting — which tickers trade, price step, strike offsets, spot
symbol, option-root allowlist, fees, exit defaults, market-order policy, Counter
Read floor — lives in the Supabase table `public.torque_tickers_setup`
(migration 0021), so every backend container reads the same data. It is held
in memory (`_SETUP`), loaded before the app serves (`refresh_ticker_setup`,
called from main.py) and refreshed every TORQUE_TICKERS_REFRESH_SEC, so an
edit in the table reaches every container without a redeploy. The accessors
below are synchronous reads of that snapshot.

Fails closed: until the table has been read once, no ticker is configured —
the page lists nothing and news signals are dropped as "not a configured
Torque ticker". A failed refresh keeps the last good copy.

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

import asyncio
import logging
import os
import time
from typing import Any

log = logging.getLogger("edgelane.market.torque_config")

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

# ── Base strike offsets (strategy-shape defaults) ──────────────────────────
# Structural defaults for every ticker; a ticker's `offsets` in
# torque_tickers_setup are merged over these. `tick` is the fallback price step
# for a ticker with no row at all.
_BASE_OFFSETS: dict[str, Any] = {
    "tick": 0.05,
    "single":    {"offset": 0},
    "vertical":  {"anchor": 0,  "width": 50},
    "condor":    {"short_offset": 50, "width": 50},
    "iron_fly":  {"width": 50},
    "butterfly": {"body_offset": 0, "wing": 50},
}


class TickSize(float):
    """A price increment that can step up above a price break — the US
    penny-pilot rule: $0.01 below $3, $0.05 at or above. Behaves as its base
    (minimum) step wherever it's used as a plain number; the engine's
    rounding helpers ask `at(price)` for the step that's valid at the price
    being rounded, so every limit, target and stop lands on a legal increment
    on either side of $3."""
    above: float | None
    brk: float

    def __new__(cls, base: float, above: float | None = None, brk: float = 3.0):
        obj = super().__new__(cls, base)
        obj.above = float(above) if above else None
        obj.brk = float(brk)
        return obj

    def at(self, price: float) -> float:
        if self.above and abs(float(price)) >= self.brk:
            return self.above
        return float(self)


# ── Per-ticker setup: Supabase public.torque_tickers_setup ─────────────────
# ticker -> row, in list order (dict keeps insertion order). Holds every row,
# enabled or not: a disabled ticker drops out of tickers() but its settings
# still apply to anything already open on it.
_SETUP: dict[str, dict[str, Any]] = {}
_SETUP_STATE: dict[str, Any] = {"loaded": False, "loaded_at": 0.0}

_ROW_DEFAULTS: dict[str, Any] = {
    "enabled": True, "offsets": {}, "close_target_spread_scaled": False,
    "close_target_required": False, "market_orders_allowed": True,
}


def apply_ticker_setup(rows: list[dict[str, Any]]) -> None:
    """Replace the in-memory snapshot with `rows` (the table's contents)."""
    norm = []
    for r in rows or []:
        t = str(r.get("ticker") or "").strip().upper()
        if not t:
            continue
        row = {**_ROW_DEFAULTS, **{k: v for k, v in r.items() if v is not None}, "ticker": t}
        if not isinstance(row.get("offsets"), dict):
            row["offsets"] = {}
        norm.append(row)
    norm.sort(key=lambda r: (int(r.get("sort_order") or 0), r["ticker"]))
    _SETUP.clear()
    _SETUP.update({r["ticker"]: r for r in norm})
    _SETUP_STATE.update(loaded=True, loaded_at=time.time())


async def refresh_ticker_setup() -> bool:
    """Reload the snapshot from Supabase. False (snapshot untouched) when the
    read failed — a transient outage must never wipe a good copy."""
    from . import supabase_admin
    try:
        rows = await supabase_admin.get_torque_ticker_setup()
    except Exception as e:
        log.warning("torque ticker setup read failed: %s", e)
        rows = None
    if rows is None:
        if not _SETUP_STATE["loaded"]:
            log.warning("torque ticker setup not loaded yet — no Torque tickers until it is")
        return False
    apply_ticker_setup(rows)
    return True


async def ticker_setup_loop(interval_sec: float) -> None:
    """Background refresh so every container converges on the table's
    contents within `interval_sec`. Retries every 10s while the setup has
    never loaded (Torque has no tickers until it does)."""
    interval = max(5.0, float(interval_sec))
    while True:
        await asyncio.sleep(interval if _SETUP_STATE["loaded"] else min(10.0, interval))
        before = (tickers(), {t: dict(r) for t, r in _SETUP.items()})
        if await refresh_ticker_setup() and (tickers(), {t: dict(r) for t, r in _SETUP.items()}) != before:
            from .logsetup import txn
            txn("torque ticker setup changed", tickers=",".join(tickers()))


def ticker_setup_loaded() -> bool:
    return bool(_SETUP_STATE["loaded"])


def _row(ticker: str) -> dict[str, Any]:
    return _SETUP.get((ticker or "").upper(), {})


def tick_size(ticker: str) -> TickSize:
    """The ticker's price step (price-aware for penny-pilot names)."""
    r = _row(ticker)
    base = r.get("tick")
    return TickSize(float(base) if base is not None else float(_BASE_OFFSETS["tick"]),
                    r.get("tick_above_3"))


def structure_tick(ticker: str, *, single: bool) -> float:
    """The step for an order on `ticker`. The above-$3 step (`tick_above_3`)
    is a SINGLE-option rule — a multi-leg spread trades as a complex order
    priced in the base step at any net price — so spreads get the plain base
    step and single legs the price-aware TickSize."""
    t = tick_size(ticker)
    return t if single else float(t)


def ticks_map() -> dict[str, dict[str, float | None]]:
    """Per-ticker {tick, tick_above_3} for the frontend's price inputs."""
    return {t: {"tick": float(tick_size(t)), "tick_above_3": tick_size(t).above} for t in tickers()}


def allowed_roots(ticker: str) -> list[str]:
    """Roots we're willing to trade for `ticker`. Empty list = no restriction.
    Tradier serves chains by BASE symbol with every root mixed in; the generic
    dedup in torque_engine._keep_primary_root handles most names, but DJX must
    trade only DJXW (its AM-settled monthly DJX root never coexists except on
    the monthly expiration itself)."""
    return [str(x).upper() for x in (_row(ticker).get("allowed_roots") or [])]


def yahoo_symbol(ticker: str) -> str | None:
    """Yahoo chart symbol for the live spot quote (indices need the ^ form;
    ^DJX already quotes at the 1/100 DJIA scale the strike grid uses). None =
    fall back to Tradier put-call parity."""
    return _row(ticker).get("yahoo_symbol") or None


def daily_range(ticker: str) -> tuple[float | None, float | None]:
    """(daily_min_range, daily_max_range) — reserved for later use."""
    r = _row(ticker)
    lo, hi = r.get("daily_min_range"), r.get("daily_max_range")
    return (float(lo) if lo is not None else None, float(hi) if hi is not None else None)


DEFAULT_CLOSE_TARGET_PCT = 30.0   # +30% profit target for the auto-close order

# ── Auto-close profit targets ──────────────────────────────────────────────
# The close order is a limit at entry*(1±pct). For it to ever fill, `pct` must
# exceed the round-trip cost of crossing the package bid/ask — otherwise the
# target price sits inside the spread and the position can only be closed at a
# loss. On SPX/NDX the package spread is ~3-4% of mid, so the 30% default has
# huge headroom. On DJX it is 16-43% of mid (measured live), so 30% is BELOW
# the round-trip cost for tighter widths. Hence a per-ticker floor that the API
# clamps UP to — a user cannot select a target that is structurally unfillable.
# Per ticker in torque_tickers_setup (null = the global default):
#   close_target_default = pre-filled in the UI, close_target_min = hard floor
#   enforced server-side.
#   close_target_spread_scaled: also raise the floor to the LIVE package spread at place
#   time. Opt-in per ticker: on tight names the static floor already clears the
#   spread by 10x, and scaling there would silently move long-standing SPX/NDX
#   close targets. DJX needs it because its spread% swings 16% → 800% intraday
#   as premium decays.
#   close_target_required: auto-close cannot be turned OFF for this ticker. The user may
#   raise the profit target but never disarm the exit. Consequence: when the
#   package is wider than MAX_AUTO_CLOSE_SPREAD_PCT the close cannot be armed,
#   so the ENTRY is refused too — a mandatory exit means no exit, no trade.

# A package whose own bid/ask is this wide relative to its mid cannot support ANY
# profit target — the close limit would sit inside the spread forever. Observed on
# DJX 0DTE once premium decays to a few cents (net_mid 0.02, net_bid -0.06). We
# refuse to arm auto-close rather than place an entry whose exit can never fill.
MAX_AUTO_CLOSE_SPREAD_PCT = 100.0

# ── Stop-loss ────────────────────────────────────────────────────────────────
# Global default 30%, mirroring DEFAULT_CLOSE_TARGET_PCT — every order gets both
# a profit target and a stop by default now, not just DJX.
#
# Single-leg: the stop rides the native broker-held OTO bracket (protective side
# gets the more durable mechanism — it must survive this process being down).
# The profit target for that same order is then app-watched instead (missing a
# profit tick if the backend is briefly down is a much smaller cost than missing
# stop protection). Multi-leg spreads still can't use OTO at all (Tradier's legs
# must share one option symbol), so both exits stay app-managed there, same as
# always.
#
# The app-managed watcher path (spreads, or single-leg once the native stop
# closes and we're watching for the profit side) is a PASSIVE resting limit for
# the target — it never crosses, so it never pays the exit half-spread. The stop
# is the opposite — it fires when the trade is going wrong and must CROSS to get
# out, paying the half-spread it would otherwise have avoided. On DJX that exit
# half can be ~90% of the position's mid, so:
#   * the trigger is measured at MID (fair value), never at the bid — measuring
#     at the bid would fire instantly, since a wide package is underwater by
#     half the spread the moment it fills;
#   * if the book is wider than STOP_MAX_EXIT_SPREAD_PCT at trigger time we do
#     NOT dump into it. Crossing a 180%-of-mid market hands back more than the
#     stop was meant to save. The watcher flags `stop_blocked_wide_market` and
#     keeps polling for a sane book instead.
DEFAULT_STOP_LOSS_PCT = 30.0   # global default — same % as DEFAULT_CLOSE_TARGET_PCT,
                                # symmetric protection alongside the profit target
STOP_MAX_EXIT_SPREAD_PCT = 60.0

# A stop-LIMIT (the trigger converts to a limit order, never a market order)
# only ever guarantees the exit PRICE, never the FILL — on a violent enough
# move the market can keep running away from a resting limit while the loss
# grows. A real broker-native "stop-market" order sidesteps this by giving up
# price control entirely once triggered; the reactive watcher gets the same
# guarantee by escalating: after a stop-exit limit sits unfilled for this many
# _STOP_INTERVAL polls (or the book stays too wide to cross this many times
# running), give up on price and resubmit as a genuine market order instead.
# An open loss compounding forever is worse than one wide/marketable crossing.
STOP_ESCALATE_AFTER_TICKS = 2

# A genuine unbounded `type=market` multileg order has NO price protection at
# all — on a thin/wide book it can clear worse than the quoted bid/ask itself,
# which is the opposite of what a stop is for. So market is the LAST resort,
# not the first escalation: each stall instead re-quotes the SAME resting
# order in place (MODIFY, not cancel-and-replace — cheaper, and keeps one
# order id the panel/history can track) at a fresh, bounded, real price (what
# usually fills close to mid with price improvement on a multileg spread) —
# this many times — before finally freezing, alerting, and guaranteeing the
# exit at market. No limit, bounded or not, can ever guarantee a fill; a stop
# that chases forever and never exits on a runaway move is worse than one
# that eventually pays the spread to actually get out.
STOP_MAX_REQUOTES = 5

# ── Native single-leg OTOCO (both exits broker-held) ─────────────────────────
# Verified empirically against Tradier sandbox (2026-09-18), not assumed:
#   * plain "oto" REJECTS a second leg priced below the entry ("must be higher
#     than price[0]") — it only understands "oto" as entry+profit, never a stop.
#   * "otoco" (3 legs: entry + profit limit + stop) is accepted, but Tradier
#     requires the stop leg to be type=stop_limit (stop[N] trigger + price[N]
#     limit) — two plain limits on the same side is rejected as
#     "OcoSameOrderTypeAndSideNotAllowed".
#   * Tradier enforces a MINIMUM $0.10 gap between the two OCO exit prices
#     ("OCO price difference should be at least 0.1$") — on cheap 0DTE premium
#     a 30%/30% profit/stop band can land narrower than that. Below the floor
#     we don't submit a bracket we already know will be rejected; fall through
#     to the app-managed watcher (both exits reactive) instead.
OCO_MIN_PRICE_GAP = 0.10
# Buffer between the stop's TRIGGER and its LIMIT once tripped, so the stop_limit
# leg is actually marketable at the moment it fires rather than resting above/
# below a market that's already moved on. A native bracket can't adapt this to
# the live spread at trigger time the way the reactive watcher's stop_exit_price
# does (it has no book to check until Tradier evaluates the trigger) — this is a
# fixed, pre-committed buffer, the one real trade-off of going broker-native.
STOP_LIMIT_BUFFER_TICKS = 3

# ── Market orders ──────────────────────────────────────────────────────────
# A market order crosses to whatever the book shows. On a penny-wide name that
# costs a cent; on DJX the ask can be 10.00 against a 5.22 mid, so a market
# entry silently pays ~2x fair value. There is no legitimate reason to send a
# market order into that book — force a limit and let the user see the price.
def market_orders_allowed(ticker: str) -> bool:
    """A market order crosses to whatever the book shows — on DJX that silently
    pays ~2x fair value, so such tickers force a limit (market_orders_allowed
    = false in torque_tickers_setup)."""
    return bool(_row(ticker).get("market_orders_allowed", True))


# ── Execution fees ─────────────────────────────────────────────────────────
# Commission is charged PER CONTRACT PER LEG, both opening and closing. Values
# below were read off Tradier's own order preview (`commission` field) against a
# live production account — identical in sandbox:
#     DJX  buy 1 contract  -> 0.53      DJX  buy 10 -> 5.30   (linear)
#     DJX  2-leg vertical  -> 1.06      (= 2 legs x 0.53)
#     SPX  buy 1 contract  -> 0.95
# Index options carry a proprietary exchange fee, hence DJX/SPX > the plain
# equity-option rate. Per ticker in torque_tickers_setup
# (commission_per_contract); null uses DEFAULT_COMMISSION.
#
# `extra_fee_per_contract` covers regulatory pass-throughs (ORF, TAF) which
# Tradier's preview reports as fees=0 because they are assessed at settlement,
# not at order time. It defaults to 0.0 — we do NOT invent a number. Set
# extra_fee_per_contract in torque_tickers_setup if you want the target to
# absorb them too.
DEFAULT_COMMISSION = 0.35
DEFAULT_EXTRA_FEE = 0.0


def commission_per_contract(ticker: str) -> float:
    c = _row(ticker).get("commission_per_contract")
    return float(c) if c is not None else DEFAULT_COMMISSION


def extra_fee_per_contract(ticker: str) -> float:
    f = _row(ticker).get("extra_fee_per_contract")
    return float(f) if f is not None else DEFAULT_EXTRA_FEE


def fee_per_contract(ticker: str) -> float:
    """All-in per-contract, per-leg, one-way execution cost."""
    return commission_per_contract(ticker) + extra_fee_per_contract(ticker)


def stop_loss_default(ticker: str) -> float | None:
    """Ticker-level default ONLY — None means "no stop unless the caller asks
    for one," same as before. DEFAULT_STOP_LOSS_PCT is NOT folded in here: the
    close_target_pct precedent (Field(default=DEFAULT_CLOSE_TARGET_PCT), always
    sent explicitly by the UI's own default state) shows "on by default" belongs
    in the caller, not this ticker-lookup. Folding it in here instead made EVERY
    existing caller that omits stop_loss_pct — including every test built before
    this feature — silently start getting a stop armed, which is exactly the
    kind of surprise a live-order-placement default must not spring on a caller
    that never asked for it."""
    d = _row(ticker).get("stop_loss_default")
    return float(d) if d is not None else None


def close_target_default(ticker: str) -> float:
    d = _row(ticker).get("close_target_default")
    return float(d) if d is not None else DEFAULT_CLOSE_TARGET_PCT


def close_target_min(ticker: str) -> float:
    """Hard floor for the auto-close profit target. Default 1.0 (= the API's
    existing lower bound) for tickers whose spreads are tight enough not to
    need one."""
    m = _row(ticker).get("close_target_min")
    return float(m) if m is not None else 1.0


def close_target_spread_scaled(ticker: str) -> bool:
    """True when the floor must also clear the live package bid/ask (DJX)."""
    return bool(_row(ticker).get("close_target_spread_scaled", False))


def close_target_required(ticker: str) -> bool:
    """True when auto-close may not be disarmed for this ticker (DJX)."""
    return bool(_row(ticker).get("close_target_required", False))


def close_targets_map() -> dict[str, dict[str, float]]:
    """Per-ticker {default,min,spread_scaled,required} for the frontend."""
    return {t: {"default": close_target_default(t), "min": close_target_min(t),
                "spread_scaled": close_target_spread_scaled(t),
                "required": close_target_required(t)}
            for t in tickers()}


def stop_loss_defaults_map() -> dict[str, float]:
    """Per-ticker stop-loss default for the frontend — mirrors close_targets_map
    so the UI's stop-loss % field can react to a ticker switch the same way
    close_target_pct already does (see the [ticker]-keyed useEffect in
    torque.html). Without this, once the UI always sends an explicit
    stop_loss_pct (needed so 0 can mean "off"), the server's own ticker-aware
    stop_loss_default() — DJX's wider 50%, specifically — would never be
    consulted again; the UI would silently ship a flat 30% for every ticker,
    tightening DJX's stop below the level it was configured not to fire on
    noise at."""
    return {t: (stop_loss_default(t) or DEFAULT_STOP_LOSS_PCT) for t in tickers()}


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
# meaningful price) — so the floor is sized per ticker (richness_floor in
# torque_tickers_setup; null uses DEFAULT_RICHNESS_FLOOR).
DEFAULT_RICHNESS_FLOOR = 0.03


def richness_floor(ticker: str) -> float:
    f = _row(ticker).get("richness_floor")
    return float(f) if f is not None else DEFAULT_RICHNESS_FLOOR


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


def ticker_rule(ticker: str, strategy: str) -> dict[str, Any]:
    """Resolved offset rule for (ticker, strategy): base offsets ⊕ the ticker's
    `offsets` from torque_tickers_setup, narrowed to the strategy's kind.
    `tick` is the ticker's TickSize. Returns e.g.
    {"kind":"vertical","tick":0.05,"anchor":20,"width":100,"type":"debit","side":"call","dir":"bull"}.
    """
    sdef = STRATEGY_DEFS.get(strategy)
    if not sdef:
        raise KeyError(f"unknown strategy {strategy!r}")
    kind = sdef["kind"]
    tk = ticker.upper()

    merged = _deep_merge(_deep_merge({}, _BASE_OFFSETS), _row(tk).get("offsets") or {})

    rule = dict(merged.get(kind) or {})
    rule["kind"] = kind
    rule["tick"] = structure_tick(tk, single=(kind == "single"))
    rule["type"] = sdef["type"]
    rule["side"] = sdef["side"]
    if "dir" in sdef:
        rule["dir"] = sdef["dir"]
    return rule


def tickers() -> list[str]:
    """Enabled tickers, in list order (first = the page's default)."""
    return [t for t, r in _SETUP.items() if r.get("enabled", True)]


def known_tickers() -> list[str]:
    """Every configured ticker, enabled or not (e.g. to recognise the
    underlying of an existing position)."""
    return list(_SETUP)


def default_strategy() -> str:
    return DEFAULT_STRATEGY


def operator_uids() -> list[str]:
    """Auth uid(s) (Supabase `profiles.id`) of the server operator(s) to auto-grant
    the full toolset on startup. Sourced from config — never hardcoded: the
    `TORQUE_OPERATOR_UIDS` env var (comma-separated). Empty by default (no
    seeding). Used by the
    backend to seed `profiles.tools_enabled` via service_role (see
    supabase_admin.grant_user_tools); the admin token bypasses the gate anyway."""
    out: list[str] = []
    env = os.environ.get("TORQUE_OPERATOR_UIDS", "")
    out.extend(u.strip() for u in env.split(",") if u.strip())
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
