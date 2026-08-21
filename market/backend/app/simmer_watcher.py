"""Simmer watcher — the 5-minute union-of-watchlists sweep (docs/simmer.md).

Shape follows `poller.py` exactly: a module-level `state = SimmerState()`
singleton that routes import directly, a `try: while True:` loop with
`except asyncio.CancelledError: raise`, per-symbol error swallowing (one bad
ticker never kills the sweep), and every DuckDB write via `asyncio.to_thread`.

Per sweep:
  1. Fetch the DISTINCT active (symbol, expiration) union across ALL users'
     watchlists from Supabase (service-role read). Twenty users watching NVDA
     cost one computation; per-user thresholds apply at READ/ALERT time.
     Supabase unconfigured → fall back to `simmer_config.tickers()` (dev mode).
  2. Compute the MARKET REGIME once, at the index level (VIX/VIX3M term
     structure; SPY/QQQ quotes carried as context). VIX3M unavailable →
     degrade gracefully to "calm" with a data_quality note.
  3. Per symbol: tier-1 research from `simmer_research_cache` honouring the
     per-field TTLs in `simmer_config.TTL`, refreshing only expired groups —
     EXCEPT when a mandatory invalidator fires (hard-block filing landed,
     velocity burst, earnings date changed), which voids every TTL at once.
  4. Per (symbol, expiration): fetch the chain, normalize it with the poller's
     own normalizer, call `simmer_engine.evaluate_readiness` (the SAME function
     `/simmer/analyze` calls — the paths can never diverge), persist a
     `simmer_readiness` row, update `state.latest_by_key`.
  5. Cross-sectional dispersion: many names spiking IV together while the index
     regime reads calm is ONE sector shock, not many opportunities — suppress
     the affected cluster (sector tag from `ticker_rule`).
  6. Alert state machine with hysteresis: fire ONCE per transition into
     `ready`, re-arm only after the score has dwelt below
     threshold − HYSTERESIS_MARGIN. Alerts fan out per watching user with THAT
     user's own `simmer_settings` applied at write time; the persisted
     readiness/outcome rows always record the ENGINE's verdict.

Market data flows through the `SimmerDataProvider` abstraction
(`simmer_data_provider.py`): Tradier by default (identical to the
pre-abstraction behavior), or Yahoo via SIMMER_DATA_PROVIDER=yahoo to keep
the 5-minute sweep off Tradier's 120 req/min budget that Matrix and Torque
share. Every entry point still accepts a raw TradierClient (coerced via
`as_provider`), so pre-abstraction call sites and tests are unchanged.

Slower loops piggybacked on the sweep cadence:
  * paper-outcome evaluator (~30 min): expired readiness rows → did the short
    strike hold at expiry? Settlement spot = previous close via the provider
    quote. `touched` needs daily high/low history: filled when the provider's
    `daily_bars` covers the tenor (Yahoo); with Tradier (no history endpoint)
    it stays NULL and is flagged in the row, exactly as before.
  * daily IV-history job (first sweep of a session): one `simmer_iv_history`
    row per symbol. With provider `daily_bars` the row carries real OHLC and
    Yang-Zhang RV; without (Tradier), the old fallback: OHLC legs NULL,
    `close_px` approximates with the sweep spot, RV falls back to
    close-to-close over the cached closes (noted per row).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Sequence
from zoneinfo import ZoneInfo

from . import simmer_config
from . import simmer_engine
from . import supabase_admin
from .dealer_exposures import compute_dealer_exposures
from .poller import _is_market_open
from .simmer_data_provider import as_provider, get_simmer_provider
from .simmer_calendar import load_macro_calendar
from .simmer_catalysts import fetch_recent_catalysts, sweep_current_filings
from .walls import find_gex_walls_bilateral

log = logging.getLogger("edgelane.simmer.watcher")

# ── Alert hysteresis + slow-loop cadence (module-level so tests can shrink) ──
HYSTERESIS_MARGIN = 5.0        # re-arm requires score < threshold − this ...
READY_DWELL_SEC = 900.0        # ... sustained for this long (dwell in cooling)
OUTCOME_INTERVAL_SEC = 1800.0  # paper-outcome sweep cadence
IV_SPIKE_PERCENTILE = 80.0     # effective IVP at/above this counts as a spike
CATALYST_LOOKBACK_DAYS = 30    # how far back tier-1 reads simmer_catalysts

_EASTERN = ZoneInfo("America/New_York")


class SimmerState:
    """In-memory readiness cache + watcher status. Single instance per process
    (mirrors poller.PollerState). Routes import this directly."""

    def __init__(self) -> None:
        self.latest_by_key: dict[str, dict] = {}      # "SYM|YYYY-MM-DD" -> envelope
        self.last_sweep_at: str | None = None
        self.last_error: str | None = None
        self.last_error_by_symbol: dict[str, str] = {}
        self.is_running: bool = False
        self.watched_count: int = 0
        self.regime: dict | None = None
        self.market_open: bool = False
        self.market_reason: str = "startup"
        self.sweep_count: int = 0
        self.watchlist_source: str = "unknown"        # supabase | config_fallback
        self.dispersion: dict | None = None
        # Alert machine: key -> {"state": cold|ready|cooling|vetoed, "since": epoch}
        self.alert_states: dict[str, dict] = {}
        self.alerts_fired: int = 0
        # Slow loops
        self.last_outcome_run_at: str | None = None
        self.last_outcome_run_ts: float | None = None
        self.outcomes_recorded: int = 0
        self.last_iv_history_date: str | None = None
        # Cross-sectional VRP bootstrap (docs/simmer.md "cold start"): each
        # symbol's live IV/RV ratio, harvested from the previous sweep. iv_rv is
        # a per-(symbol,expiration) quantity known only AFTER analyze runs the
        # chain, so it can't come from tier-1 research — this persistent cache is
        # how peers reach the next sweep and the cross-sectional percentile fills
        # in when there's no time-series IV history yet (day one / weekend).
        self.peer_iv_rv: dict[str, float] = {}
        # The sweep's data provider (set by simmer_loop) — routes reuse it so
        # /simmer/analyze runs on the same source as the sweep.
        self.provider = None


# Module-level singleton — routes import this directly.
state = SimmerState()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _key(symbol: str, expiration: Any) -> str:
    return f"{str(symbol).upper()}|{expiration}"


def _num(v: Any) -> float | None:
    return simmer_engine._num(v)


def _first_price(quote: dict | None) -> float | None:
    """Spot from a Tradier quote — same key order as poller.poll_symbol."""
    for k in ("last", "close", "prevclose"):
        v = (quote or {}).get(k)
        f = _num(v)
        if f and f > 0:
            return f
    return None


def _prev_close(quote: dict | None) -> float | None:
    """Settlement proxy: PREVIOUS close first (the expiry-session close when
    the outcome sweep runs the next session), then close/last."""
    for k in ("prevclose", "close", "last"):
        f = _num((quote or {}).get(k))
        if f and f > 0:
            return f
    return None


def _is_capture_window(tz: ZoneInfo = _EASTERN, now: datetime | None = None) -> bool:
    """Is it the right moment to snapshot IV history for the day?

    IV Rank is an END-OF-DAY quantity — ORATS/VolRadar snapshot the ~6 PM ET
    close, not whenever a process happened to boot. So capture once per day but
    ONLY at/after the close:

      * a WEEKDAY at/after 16:00 ET → True (today's settle is in);
      * a WEEKEND (or, by the same 16:00 rule, a holiday evening) → True: the
        market is closed and Yahoo still returns the most recent COMPLETED
        session's close (e.g. Friday's over the weekend), so a weekend/holiday
        first-run still captures it;
      * during market hours / a weekday morning before the close → False (wait).

    The "only if no row yet for the latest completed session" clause is enforced
    by `record_iv_history`'s existing once-per-day `state.last_iv_history_date`
    guard, so this stays a pure clock predicate (testable with an injected
    `now`)."""
    et = now.astimezone(tz) if now is not None else datetime.now(tz)
    if et.weekday() >= 5:               # Saturday/Sunday — capture Fri's close
        return True
    return et.hour >= 16                # weekday: only after the 16:00 ET close


def _iso_date(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, datetime):
        return v.date().isoformat()
    s = str(v).strip()
    return s[:10] if s else None


# ─────────────────────────────────────────────────────────────────────────────
# Watchlist union
# ─────────────────────────────────────────────────────────────────────────────
async def watchlist_union() -> list[tuple[str, str | None]]:
    """DISTINCT active (symbol, expiration) across all users' watchlists.

    Reads `simmer_watchlist` via the service-role REST helper. `expiration` is
    None when the user left it to the engine (auto-pick). When Supabase is not
    configured (dev) the union falls back to `simmer_config.tickers()` so the
    watcher still runs end-to-end without credentials.
    """
    rows = await supabase_admin.select_many(
        "simmer_watchlist", "symbol,expiration,active", {"active": "eq.true"})
    if rows is None:
        state.watchlist_source = "config_fallback"
        return [(t.upper(), None) for t in simmer_config.tickers()]
    state.watchlist_source = "supabase"
    seen: set[tuple[str, str | None]] = set()
    out: list[tuple[str, str | None]] = []
    for r in rows:
        sym = str(r.get("symbol") or "").strip().upper()
        # Same format rule as the validate route. The watchlist write model is
        # client-RLS (the backend never writes rows), so a hostile client CAN
        # insert arbitrary strings straight into Supabase — this boundary is
        # where the sweep union must refuse them, or garbage "symbols" ride
        # into provider requests and waste sweep budget every 5 minutes.
        if not sym or not re.fullmatch(r"[A-Z.\-]{1,10}", sym):
            if sym:
                log.warning("watchlist row skipped: malformed symbol %r", sym)
            continue
        exp = _iso_date(r.get("expiration"))
        pair = (sym, exp)
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Market regime — once per sweep, index level
# ─────────────────────────────────────────────────────────────────────────────
async def compute_regime(tradier) -> dict:
    """VIX/VIX3M term structure via the existing Tradier client, mapped through
    `simmer_engine.market_regime`'s bands. SPY/QQQ quotes are fetched as index
    context (the regime gate needs index DATA even though index TRADING is
    excluded). Any missing leg degrades gracefully — no VIX3M means no term
    structure, which reads as "calm" with an explicit data_quality note rather
    than an invisible default.

    `tradier` may be a raw TradierClient (pre-abstraction call sites/tests) or
    any SimmerDataProvider — `as_provider` coerces either."""
    provider = as_provider(tradier)
    notes: list[str] = []
    quotes: dict[str, float | None] = {}
    for sym in ("VIX", "VIX3M", "SPY", "QQQ"):
        try:
            quotes[sym] = _first_price(await provider.quote(sym))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            quotes[sym] = None
            notes.append(f"{sym.lower()}_quote_failed:{type(e).__name__}")
    vix, vix3m = quotes.get("VIX"), quotes.get("VIX3M")
    ratio = (vix / vix3m) if (vix and vix3m and vix3m > 0) else None
    if ratio is None:
        notes.append("no_term_structure_degraded_to_calm")
        return {"state": "calm", "calm": True, "vix_ts": None,
                "vix": vix, "vix3m": vix3m,
                "spy": quotes.get("SPY"), "qqq": quotes.get("QQQ"),
                "multiplier": 1.0, "suppress": [], "data_quality": notes,
                "computed_at": _utc_iso()}
    reg = simmer_engine.market_regime({"vix_ts": ratio}, simmer_config.regime())
    reg.update({
        "calm": reg.get("state") == "contango",
        "vix": vix, "vix3m": vix3m,
        "spy": quotes.get("SPY"), "qqq": quotes.get("QQQ"),
        "data_quality": notes,
        "computed_at": _utc_iso(),
    })
    return reg


# ─────────────────────────────────────────────────────────────────────────────
# Tier-1 research cache (per-field TTLs + mandatory invalidation)
# ─────────────────────────────────────────────────────────────────────────────
def _as_naive(ts: Any) -> datetime | None:
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=None) if ts.tzinfo else ts
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "")).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _et_to_naive_utc(ts: Any) -> datetime | None:
    """Naive-ET timestamp (the simmer_catalysts storage convention) → naive
    UTC, the clock the tier-1 cache stamps use."""
    dt = _as_naive(ts)
    if dt is None:
        return None
    return dt.replace(tzinfo=_EASTERN).astimezone(timezone.utc).replace(tzinfo=None)


def _group_expired(row: dict | None, field: str, ttl_sec: int,
                   now: datetime | None = None) -> bool:
    """True when the field group needs a refresh. TTL ≤ 0 = every sweep;
    TTL < 0 (permanent) never expires once stamped."""
    ts = _as_naive((row or {}).get(field))
    if ttl_sec < 0:
        return ts is None
    if ttl_sec == 0:
        return True
    if ts is None:
        return True
    now = now or _now_naive_utc()
    return (now - ts).total_seconds() > ttl_sec


async def _mandatory_invalidation(db, symbol: str, row: dict | None) -> list[str]:
    """TTL expiry is not the only trigger — a stale "no catalyst" is precisely
    the dangerous value. Returns the invalidator names that fired (subset of
    simmer_config.CACHE_INVALIDATORS); any hit voids every tier-1 TTL."""
    if not row:
        return []
    reasons: list[str] = []
    catalyst_at = _as_naive(row.get("catalyst_at"))
    try:
        blocking = await asyncio.to_thread(
            fetch_recent_catalysts, db, symbol, 5, True)
    except Exception as e:  # DB hiccup: don't invalidate on noise
        log.warning("invalidation check failed for %s: %s", symbol, e)
        blocking = []
    for c in blocking:
        # Catalyst rows store naive-ET acceptance timestamps (simmer_catalysts
        # convention); the cache stamps are naive-UTC. Convert before comparing
        # or a filing accepted seconds ago reads ~4-5 h "older" than the cache
        # and the invalidator silently never fires.
        acc = _et_to_naive_utc(c.get("acceptance_at"))
        if catalyst_at is None or (acc is not None and acc > catalyst_at):
            reasons.append("hard_block_filing")
            break
    if str(row.get("velocity_tier") or "").lower() == "alert":
        reasons.append("velocity_burst")
    # Earnings date changed: compare the cached flag against the newest
    # earnings-type catalyst row in the DB.
    try:
        flags = json.loads(row.get("catalyst_flags") or "{}")
    except (TypeError, ValueError):
        flags = {}
    cached_ern = flags.get("earnings_date")
    if cached_ern:
        try:
            cats = await asyncio.to_thread(
                fetch_recent_catalysts, db, symbol, CATALYST_LOOKBACK_DAYS)
        except Exception:
            cats = []
        latest_ern = _latest_earnings_date(cats)
        if latest_ern and latest_ern != cached_ern:
            reasons.append("earnings_date_changed")
    return reasons


def _latest_earnings_date(cats: Sequence[dict]) -> str | None:
    dates = []
    for c in cats or []:
        if str(c.get("event_type") or "") != "earnings":
            continue
        d = _iso_date(c.get("event_date"))
        if d:
            dates.append(d)
    return max(dates) if dates else None


async def _refresh_news(db, symbol: str, row: dict) -> dict:
    """News group (Phase 3a): Alpaca/RSS ingest + Gemini scoring + trailing
    sentiment/velocity aggregates, all via `simmer_news.refresh_news` — the
    same entry point an operator call uses. Runs on the existing
    `ticker_sentiment` TTL (15 min, simmer_config.TTL).

    The updates carry `velocity_tier`, which `load_research` persists to the
    research row — the exact field `_mandatory_invalidation` reads for the
    `velocity_burst` invalidator, so an "alert" tier voids every tier-1 TTL on
    the NEXT sweep by construction.

    Missing config keys → simmer_news returns a clean no-op (None fields +
    note); any failure here is swallowed so a news problem NEVER kills the
    sweep. news_at is always stamped so the TTL machinery keeps cycling."""
    from . import simmer_news
    from .config import get_settings
    try:
        out = await simmer_news.refresh_news(
            db, [symbol], get_settings(), persist_research=False)
        updates = out.get(str(symbol).upper()) or {}
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("news refresh failed for %s: %s", symbol, e)
        updates = {"_news_notes": [f"news:refresh_failed:{type(e).__name__}"]}
    updates.setdefault("news_at", _now_naive_utc())
    return updates


async def _fetch_next_earnings(symbol: str, provider) -> dict | None:
    """The NEXT earnings as {date, days, confirmed}, from the configured
    SIMMER_EARNINGS_PROVIDER (yahoo). None when off / unavailable / not upcoming
    within a sane horizon. The SEC feed only confirms PAST earnings — this is
    what surfaces the upcoming event."""
    from .config import get_settings
    if getattr(get_settings(), "simmer_earnings_provider", "off") != "yahoo":
        return None
    fn = getattr(provider, "next_earnings", None)
    if fn is None:
        return None
    try:
        r = await fn(symbol)
    except Exception as e:
        log.debug("next_earnings failed for %s: %s", symbol, e)
        return None
    if not r or not r.get("date"):
        return None
    try:
        days = (date.fromisoformat(str(r["date"])) - date.today()).days
    except ValueError:
        return None
    if days < 0 or days > 120:          # only upcoming, within ~4 months
        return None
    return {"date": str(r["date"]), "days": days, "confirmed": bool(r.get("confirmed"))}


async def _refresh_catalysts(db, symbol: str, row: dict, provider=None) -> dict:
    """Catalyst group: read `simmer_catalysts` (populated by the EDGAR sweep)
    and derive the flags the engine consumes. Rows carry ET acceptance
    timestamps; the engine wants days-from-now, computed here because the
    engine deliberately owns no clock."""
    try:
        cats = await asyncio.to_thread(
            fetch_recent_catalysts, db, symbol, CATALYST_LOOKBACK_DAYS)
    except Exception as e:
        log.warning("catalyst refresh failed for %s: %s", symbol, e)
        cats = []
    today = date.today()
    engine_cats: list[dict] = []
    blocking_any = False
    for c in cats:
        try:
            detail = json.loads(c.get("detail") or "{}")
        except (TypeError, ValueError):
            detail = {}
        impact = _iso_date(detail.get("impact_date")) or _iso_date(c.get("event_date"))
        days = None
        if impact:
            try:
                days = (date.fromisoformat(impact) - today).days
            except ValueError:
                days = None
        is_blocking = bool(c.get("blocking"))
        is_earnings = (str(c.get("event_type")) == "earnings"
                       or detail.get("earnings_ground_truth"))
        if not (is_blocking or is_earnings):
            continue   # info-tier filings never gate; keep the list small
        blocking_any = blocking_any or is_blocking
        engine_cats.append({
            "type": ("earnings" if is_earnings and not is_blocking
                     else str(c.get("event_type") or "sec_filing")),
            "days": days,
            "confirmed": bool(c.get("confirmed", True)),
        })
    # Next earnings DATE from the configured provider (Yahoo). The SEC feed above
    # only confirms PAST earnings; this makes the upcoming event visible to the
    # gate, the earnings window, and the card toggle.
    earnings_date = _latest_earnings_date(cats)
    fut = await _fetch_next_earnings(symbol, provider)
    if fut:
        engine_cats.append({"type": "earnings", "days": fut["days"],
                            "confirmed": fut["confirmed"]})
        earnings_date = fut["date"]      # prefer the upcoming date for the UI
    return {
        "catalyst_flags": json.dumps({
            "events": engine_cats,
            "earnings_date": earnings_date,
        }, default=str),
        "catalyst_blocking": blocking_any,
        "catalyst_at": _now_naive_utc(),
        "_catalysts": engine_cats,
    }


async def _refresh_daily(db, symbol: str, row: dict, provider=None) -> dict:
    """Daily group: IV rank/percentile + RV off `simmer_iv_history`. It is a
    rank over DAILY history — recomputing it intraday is meaningless, hence
    the until-next-close TTL.

    COLD START (the day-one path from docs/simmer.md): the history table only
    accrues one row per session, so on a fresh deploy RV would be None for ~20
    sessions and every candidate would veto `vrp_unavailable`. When the table
    can't supply RV and the provider has real daily bars (Yahoo), compute
    Yang-Zhang + close-to-close DIRECTLY from the bars — the cross-sectional
    VRP percentile then works from the very first sweep. Found live: the first
    sweep also ran before the IV-history job wrote its row, then the
    until-next-close TTL hid that row all day, so the veto never lifted.
    Hence: `daily_at` is only stamped when the group actually produced
    something — an empty result retries next sweep instead of going blind
    for 24h."""
    v = simmer_config.vol()
    try:
        hist = await asyncio.to_thread(
            db.fetch_simmer_iv_history, symbol, int(v["iv_history_days"]))
    except Exception as e:
        log.warning("iv-history read failed for %s: %s", symbol, e)
        hist = []
    ivs = [x for x in ((_num(r.get("atm_iv_xern")) or _num(r.get("atm_iv")))
                       for r in hist) if x is not None]
    yz = next((_num(r.get("rv20_yz")) for r in reversed(hist)
               if _num(r.get("rv20_yz")) is not None), None)
    cc = next((_num(r.get("rv20_cc")) for r in reversed(hist)
               if _num(r.get("rv20_cc")) is not None), None)
    ohlc = [{"open": r.get("open_px"), "high": r.get("high_px"),
             "low": r.get("low_px"), "close": r.get("close_px")}
            for r in hist if r.get("close_px") is not None]
    closes = [r.get("close_px") for r in hist]
    # Tradier structurally has no history endpoint — its daily_bars is a
    # guaranteed [] plus a data_quality note, so don't burn the call (and the
    # default-path envelope stays byte-identical, which a test pins).
    _bars_capable = (provider is not None and hasattr(provider, "daily_bars")
                     and getattr(provider, "provider_name", "") != "tradier")
    if yz is None and _bars_capable:
        try:
            bars = await provider.daily_bars(symbol, 30)
        except Exception as e:
            log.warning("cold-start daily_bars failed for %s: %s", symbol, e)
            bars = []
        if bars:
            yz = simmer_engine.yang_zhang_rv(bars)
            cc = cc if cc is not None else simmer_engine.close_to_close_rv(bars)
            ohlc = [{"open": b.get("open"), "high": b.get("high"),
                     "low": b.get("low"), "close": b.get("close")} for b in bars]
            closes = [b.get("close") for b in bars]
    out: dict[str, Any] = {"_iv_history": ivs, "_rv_yz": yz, "_rv_cc": cc,
                           "_ohlc": ohlc, "_closes": closes,
                           # Persisted twins of the transients: underscore keys
                           # are stripped before the cache upsert, so without
                           # these every CACHE-HIT sweep would feed the engine
                           # an empty vol block and veto vrp_unavailable
                           # (found live). Hydrated back in load_research.
                           "rv20_yz": yz, "rv20_cc": cc,
                           "iv_history_json": json.dumps(ivs) if ivs else None,
                           "ohlc_json": json.dumps(ohlc) if ohlc else None,
                           "closes_json": json.dumps([c for c in closes if c is not None]) if closes else None}
    if ivs or yz is not None:
        out["daily_at"] = _now_naive_utc()
    if ivs:
        out["iv_rank"] = simmer_engine.iv_rank(ivs[-1], min(ivs), max(ivs))
        out["iv_percentile"] = simmer_engine.iv_percentile(ivs[-1], ivs[:-1] or ivs)
    return out


def _persistable(row: dict) -> dict:
    """Strip transient (underscore) keys before the cache upsert."""
    return {k: v for k, v in row.items() if not str(k).startswith("_")}


async def load_research(db, symbol: str, provider=None, force: bool = False) -> dict:
    """The single tier-1 accessor: cache read + per-group TTL refresh +
    mandatory invalidation, enforced in ONE place rather than at call sites.
    `force` refreshes every group regardless of TTL (operator re-pull)."""
    symbol = symbol.upper()
    if db is None:
        return {"symbol": symbol}
    row = await asyncio.to_thread(db.get_simmer_research, symbol) or {}
    ttls = simmer_config.ttls()
    now = _now_naive_utc()
    invalidated = await _mandatory_invalidation(db, symbol, row)
    if force:
        invalidated = list(invalidated or []) + ["forced"]
    if invalidated:
        log.info("tier-1 cache for %s invalidated (%s) — refreshing all groups",
                 symbol, ",".join(invalidated))
    updates: dict[str, Any] = {}
    if invalidated or _group_expired(row, "news_at", int(ttls.get("ticker_sentiment", 900)), now):
        updates.update(await _refresh_news(db, symbol, row))
    if invalidated or _group_expired(row, "catalyst_at", int(ttls.get("catalysts", 86_400)), now):
        updates.update(await _refresh_catalysts(db, symbol, row, provider))
    _pre_upgrade_row = (row.get("daily_at") is not None
                        and row.get("rv20_yz") is None
                        and row.get("iv_history_json") is None)
    if invalidated or _pre_upgrade_row or _group_expired(
            row, "daily_at", int(ttls.get("iv_rank", 86_400)), now):
        updates.update(await _refresh_daily(db, symbol, row, provider))
    else:
        # Cache hit: rebuild the engine-facing transients from their persisted
        # twins (underscore keys never survive the DB round-trip).
        try:
            hyd: dict[str, Any] = {
                "_rv_yz": _num(row.get("rv20_yz")),
                "_rv_cc": _num(row.get("rv20_cc")),
                "_iv_history": json.loads(row["iv_history_json"]) if row.get("iv_history_json") else [],
                "_ohlc": json.loads(row["ohlc_json"]) if row.get("ohlc_json") else [],
                "_closes": json.loads(row["closes_json"]) if row.get("closes_json") else [],
            }
            updates.update(hyd)
        except (ValueError, TypeError) as e:
            log.warning("research hydrate failed for %s: %s — refreshing", symbol, e)
            updates.update(await _refresh_daily(db, symbol, row, provider))
    if updates:
        merged = {**row, **updates, "symbol": symbol}
        try:
            await asyncio.to_thread(db.upsert_simmer_research, _persistable(merged))
        except Exception as e:
            log.warning("research cache upsert failed for %s: %s", symbol, e)
        row = merged
    else:
        row = dict(row)
    if "_catalysts" not in row:
        # Cache hit: rebuild the engine-shaped catalyst list from the stored flags.
        try:
            row["_catalysts"] = (json.loads(row.get("catalyst_flags") or "{}")
                                 .get("events") or [])
        except (TypeError, ValueError):
            row["_catalysts"] = []
    row["symbol"] = symbol
    row["_invalidated"] = invalidated
    return row


# ─────────────────────────────────────────────────────────────────────────────
# Chain → engine inputs
# ─────────────────────────────────────────────────────────────────────────────
def _dte_days(exp_iso: str) -> float:
    try:
        return float((date.fromisoformat(str(exp_iso)[:10]) - date.today()).days)
    except (TypeError, ValueError):
        return 0.0


def choose_expiration(exps: Sequence[str], requested: str | None,
                      gates: dict | None = None) -> str:
    """`requested` must be listed (ValueError otherwise). Auto-pick: the listed
    expiration inside the DTE window closest to 30 DTE; else the nearest
    future one (the engine's DTE gate will say why it scored badly)."""
    listed = sorted({str(e)[:10] for e in exps or [] if e})
    if not listed:
        raise ValueError("no expirations listed")
    if requested:
        req = str(requested)[:10]
        if req not in listed:
            raise ValueError(f"expiration {req} is not listed "
                             f"(available: {', '.join(listed[:8])}…)")
        return req
    g = gates or simmer_config.gates()
    lo, hi = float(g["dte_min"]), float(g["dte_max"])
    in_window = [e for e in listed if lo <= _dte_days(e) <= hi]
    if in_window:
        return min(in_window, key=lambda e: abs(_dte_days(e) - 30.0))
    future = [e for e in listed if _dte_days(e) >= 0]
    return future[0] if future else listed[-1]


def _compute_walls(contracts: list[dict], spot: float, expiration: str) -> dict:
    """Bilateral GEX walls off the chain, reusing the parity-locked dealer
    aggregator + wall finder (reuse, not new math)."""
    try:
        de = compute_dealer_exposures(contracts, spot)
        buckets = de.get("exposures_by_date") or {}
        expos = buckets.get(expiration)
        if expos is None and buckets:
            expos = next(iter(buckets.values()))
        bw = find_gex_walls_bilateral(None, expos)
    except Exception as e:
        log.warning("wall computation failed: %s", e)
        return {}
    pw, cw = bw.get("put_wall"), bw.get("call_wall")
    return {
        "put_wall": pw.get("strike") if pw else None,
        "call_wall": cw.get("strike") if cw else None,
        "put_wall_strength": pw.get("strength") if pw else None,
        "call_wall_strength": cw.get("strength") if cw else None,
        "zero_gamma": None,   # index gamma-flip level: not derived per-name yet
    }


def _flow_aggregates(contracts: list[dict]) -> dict:
    call_vol = sum(int(c.get("volume") or 0) for c in contracts if c.get("side") == "call")
    put_vol = sum(int(c.get("volume") or 0) for c in contracts if c.get("side") == "put")
    total_oi = sum(int(c.get("open_interest") or 0) for c in contracts)
    total_vol = call_vol + put_vol
    return {
        "pcr_volume": (put_vol / call_vol) if call_vol else None,
        "vol_oi_ratio": (total_vol / total_oi) if total_oi else None,
    }


def _macro_valid_dte() -> float | None:
    try:
        cal = load_macro_calendar()
        return float((cal.valid_through - date.today()).days)
    except Exception as e:
        log.warning("macro calendar unavailable: %s", e)
        return None      # engine turns this into a visible macro veto


def _earnings_date_from_research(research: dict) -> str | None:
    """The resolved earnings ISO date, from the persisted catalyst_flags blob."""
    cf = (research or {}).get("catalyst_flags")
    if isinstance(cf, str):
        try:
            cf = json.loads(cf)
        except Exception:
            cf = None
    if isinstance(cf, dict) and cf.get("earnings_date"):
        return str(cf["earnings_date"])
    return None


def _earnings_inside_tenor(research: dict, dte: float) -> bool:
    return _earnings_within_days(research, dte)


def _earnings_within_days(research: dict, days: float | None) -> bool:
    """Any confirmed earnings catalyst falling within `days` from now."""
    if days is None:
        return False
    for c in research.get("_catalysts") or []:
        if str(c.get("type")) == "earnings":
            d = _num(c.get("days"))
            if d is not None and 0 <= d <= days:
                return True
    return False


# ── Per-ticker term structure (single-name ~30d vs ~90d slope) ──────────────
TERM_FAR_DTE_TARGET = 90.0      # sample the listed expiry nearest this DTE ...
TERM_FAR_DTE_MIN = 60.0         # ... but never nearer than this (else no signal)


def _nearest_dte(exps: Sequence[str], target: float,
                 min_dte: float | None = None) -> str | None:
    """Listed expiration whose DTE is closest to `target`, optionally floored at
    `min_dte`. Returns None when nothing qualifies (→ term_slope stays None)."""
    listed = sorted({str(e)[:10] for e in exps or [] if e})
    cands = [e for e in listed
             if _dte_days(e) >= (min_dte if min_dte is not None else 0.0)]
    if not cands:
        return None
    return min(cands, key=lambda e: abs(_dte_days(e) - target))


async def _term_structure_inputs(provider, symbol: str, exps: Sequence[str],
                                  near_exp: str, spot: float,
                                  iv_near: float | None) -> dict:
    """Fetch a far (~90 DTE) chain and derive the single-name term slope plus the
    far-expiry ATM IV/tenor the earnings-VRP decomposition reuses. One extra
    provider.chain call per symbol per sweep; skips gracefully (term_slope stays
    None, with a note) when no listed expiry ≥ ~60 DTE exists or the fetch fails.

    Returns a dict to merge into the engine's `research` block:
    term_slope / term_slope_pp / iv_far / t_far, plus `_term_note` when skipped."""
    near_dte = _dte_days(near_exp)
    far_exp = _nearest_dte(exps, TERM_FAR_DTE_TARGET, TERM_FAR_DTE_MIN)
    if far_exp is None or far_exp == near_exp:
        return {"_term_note": "no_far_expiry_ge_60dte"}
    try:
        far_chain = await provider.chain(symbol, far_exp)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("term-structure far chain failed for %s@%s: %s",
                    symbol, far_exp, e)
        return {"_term_note": f"far_chain_failed:{type(e).__name__}"}
    iv_far = simmer_engine.atm_iv(far_chain or [], spot)
    if iv_near is None or iv_far is None:
        return {"_term_note": "far_atm_iv_unavailable"}
    ts = simmer_engine.term_structure(iv_near, iv_far)
    t_far = _dte_days(far_exp) / 365.0
    return {"term_slope": ts["term_slope"], "term_slope_pp": ts["term_slope_pp"],
            "iv_far": iv_far, "t_far": t_far, "far_expiration": far_exp,
            "_near_dte": near_dte}


def _persist_readiness(db, env: dict) -> int:
    """Envelope → simmer_readiness row (sync; call via to_thread). The condor's
    full four legs live inside the components JSON; short/long columns carry
    the put vertical so the outcome evaluator always has a primary short."""
    strikes = env.get("strikes") or {}
    if "put" in strikes:            # iron condor legs dict
        short_k, long_k = strikes["put"].get("short"), strikes["put"].get("long")
        width = None
    else:
        short_k, long_k = strikes.get("short"), strikes.get("long")
        width = strikes.get("width")
    vetoed = bool(env.get("veto_reasons"))
    components_json = json.dumps({
        "components": env.get("components") or {},
        "strikes": strikes,
        "avoid_if": env.get("avoid_if") or [],
        "data_quality": env.get("data_quality") or {},
        "metrics": {k: env.get("metrics", {}).get(k) for k in
                    ("iv_rank", "iv_percentile", "iv_percentile_effective",
                     "vrp", "em_1sd", "atm_iv",
                     "term_slope", "term_slope_pp", "max_pain",
                     "vrp_total_pp", "vrp_earnings_pp", "vrp_ex_earnings_pp")},
    }, default=str)
    return db.insert_simmer_readiness({
        "ts": _now_naive_utc(),
        "symbol": env.get("symbol"),
        "expiration": env.get("expiration"),
        "spot": env.get("spot"),
        "score": None if vetoed else env.get("score"),
        "vetoed": vetoed,
        "veto_reasons": json.dumps(env.get("veto_reasons") or []),
        "components": components_json,
        "regime": (env.get("regime") or {}).get("state"),
        "structure": env.get("structure"),
        "short_strike": short_k,
        "long_strike": long_k,
        "width": width,
        "credit_mid": env.get("credit_mid"),
        "credit_fill": env.get("credit_fill"),
        "max_loss": env.get("max_loss"),
        "pop_breakeven": env.get("pop_breakeven"),
        "expected_value": env.get("expected_value"),
        "alpha": env.get("alpha"),
        "engine_version": env.get("engine_version"),
    })


async def analyze_symbol(tradier, db, symbol: str, expiration: str | None = None,
                         user_settings: dict | None = None,
                         regime: dict | None = None,
                         research: dict | None = None,
                         peer_vrp: Sequence[float] | None = None,
                         earnings_mode: bool = True,
                         persist: bool = True) -> dict:
    """THE single evaluation path. The sweep calls it per (symbol, expiration)
    with persist=True; `/simmer/analyze/{symbol}` calls it with persist=False.
    Same function, so on-demand and scheduled results can never diverge —
    mirroring how `engine.compute_engine_output` serves both the poller and
    `/snapshot`.

    Persisted rows are always the ENGINE's verdict: `user_settings` (route-side
    personalization) forces persist off, so calibration stays comparable across
    configurations.

    Market data flows through the SimmerDataProvider abstraction
    (simmer_data_provider.py): `tradier` may be a raw TradierClient — coerced
    into a TradierDataProvider, the identical pre-abstraction data path — or a
    configured provider (e.g. Yahoo) handed down by the sweep. Both return
    contracts already normalized to the poller's contract shape."""
    symbol = str(symbol).upper()
    provider = as_provider(tradier)
    cfg = simmer_config.resolved(symbol)
    if research is None:
        research = await load_research(db, symbol, provider)
    if regime is None:
        regime = state.regime or {}

    quote = await provider.quote(symbol)
    spot = _first_price(quote)
    if not spot:
        raise RuntimeError(f"no spot price for {symbol}")
    exps = await provider.expirations(symbol)
    chosen_exp = choose_expiration(exps, expiration, cfg["gates"])
    contracts = await provider.chain(symbol, chosen_exp)
    if not contracts:
        raise RuntimeError(f"empty chain for {symbol}@{chosen_exp}")
    dte = _dte_days(chosen_exp)
    walls = _compute_walls(contracts, spot, chosen_exp)
    flow = _flow_aggregates(contracts)

    # Per-ticker term structure + earnings-VRP inputs: sample a far (~90 DTE)
    # chain for the single-name slope, replacing the market-only VIX slope for
    # this signal. The far-expiry ATM IV feeds the earnings VRP decomposition.
    iv_near = simmer_engine.atm_iv(contracts, spot)
    term = await _term_structure_inputs(provider, symbol, exps, chosen_exp,
                                        spot, iv_near)
    # Earnings inside the ~90d window (but the catalyst gate hard-vetoes those
    # inside the trade tenor) makes the front IV carry event premium — flag it
    # so the decomposition attributes it and the soft avoid_if can fire.
    far_window = _num(term.get("t_far"))
    earnings_vrp_event = _earnings_within_days(
        research, far_window * 365.0 if far_window else None)

    inputs: dict[str, Any] = {
        "symbol": symbol,
        "spot": spot,
        "expiration": chosen_exp,
        "dte": dte,
        "chain": contracts,
        "research": {
            "iv_history": research.get("_iv_history") or [],
            "iv_history_days": len(research.get("_iv_history") or []),
            "iv_rank": research.get("iv_rank"),
            "iv_percentile": research.get("iv_percentile"),
            "rv_yang_zhang": research.get("_rv_yz"),
            "rv_close_to_close": research.get("_rv_cc"),
            "ohlc": research.get("_ohlc") or [],
            "peer_vrp_ratios": list(peer_vrp or []),
            # Per-ticker term structure (real single-name slope, not the index
            # VIX/VIX3M overlay) + the far-expiry legs the earnings-VRP
            # decomposition reuses. Absent keys → term_slope stays None.
            "term_slope": term.get("term_slope"),
            "term_slope_pp": term.get("term_slope_pp"),
            "iv_far": term.get("iv_far"),
            "t_far": term.get("t_far"),
            "earnings_vrp_event": earnings_vrp_event,
            "walls": walls,
            "catalysts": research.get("_catalysts") or [],
            "macro_valid_through_dte": _macro_valid_dte(),
            "sentiment": {"score": research.get("sentiment_score"),
                          "velocity_p": research.get("velocity_p")},
            "days_to_cover": research.get("days_to_cover"),
            "earnings_inside_tenor": _earnings_inside_tenor(research, dte),
            "index": {"vix_ts": (regime or {}).get("vix_ts"),
                      "dispersion": (state.dispersion or {}).get("share")
                      if state.dispersion else None},
        },
    }
    if user_settings:
        inputs["settings"] = user_settings

    # Earnings mode: when the chosen expiry sits in an earnings window, pass the
    # CACHED bias (computed on demand by /simmer/earnings) into the engine so it
    # skips the earnings veto and folds the bias into the score. The sweep never
    # calls Gemini here — it reads the cache only; a missing verdict just means
    # no lift yet (the name scores on its base VRP).
    if inputs["research"].get("earnings_inside_tenor"):
        _erow = None
        _edate = _earnings_date_from_research(research)
        if _edate and db is not None:
            try:
                _erow = await asyncio.to_thread(
                    db.get_simmer_earnings_bias, symbol, _edate)
            except Exception as e:
                log.debug("earnings bias read failed for %s: %s", symbol, e)
        inputs["earnings"] = {
            "mode": bool(earnings_mode),
            "in_window": True,
            "earnings_date": _edate,
            "direction": (_erow or {}).get("direction"),
            "confidence": (_erow or {}).get("confidence"),
            "go": bool((_erow or {}).get("go")) if _erow else False,
            "rationale": (_erow or {}).get("rationale"),
        }

    env = simmer_engine.evaluate_readiness(inputs, cfg)
    env["computed_at"] = _utc_iso()
    # Market state at compute time — lets the UI stamp a closed-hours result
    # "as of last session" and show a check-back hint that clears when live.
    env["market_open"] = bool(state.market_open)
    env["market_reason"] = state.market_reason
    env["flow"] = flow
    # Surface WHY the single-name term slope is absent (no far expiry, fetch
    # failure) rather than leaving a silent None. Attached only when skipped, so
    # the healthy path's envelope shape is unchanged.
    if term.get("_term_note"):
        dq0 = env.get("data_quality")
        if not isinstance(dq0, dict):
            dq0 = env["data_quality"] = {}
        dq0["term_structure"] = term["_term_note"]
    # Provider-level data-quality notes (Yahoo chain rejects, crumb failures,
    # …) ride the engine's own data_quality dict so they persist with the row.
    # Attached only when non-empty: the default Tradier path emits none, so
    # pre-abstraction envelope shapes are byte-identical.
    provider_notes = provider.take_data_quality_notes()
    if provider_notes:
        dq = env.get("data_quality")
        if not isinstance(dq, dict):
            dq = env["data_quality"] = {}
        dq["provider"] = {"name": provider.provider_name, "notes": provider_notes}
    if persist and db is not None and not user_settings:
        try:
            env["readiness_id"] = await asyncio.to_thread(_persist_readiness, db, env)
        except Exception as e:
            log.warning("readiness persist failed for %s@%s: %s", symbol, chosen_exp, e)
    return env


async def _writeback_flow(db, symbol: str, research: dict, env: dict) -> None:
    """Per-sweep flow/VRP write-back into tier-1 so next sweep's cross-sectional
    percentile has fresh peers. Best-effort."""
    if db is None:
        return
    m = env.get("metrics") or {}
    merged = {**_persistable(research),
              "symbol": symbol,
              "iv_rv_ratio": m.get("vrp"),
              "vrp_xsect_pct": m.get("iv_percentile_cross_sectional"),
              "vol_oi_ratio": (env.get("flow") or {}).get("vol_oi_ratio"),
              "pcr_volume": (env.get("flow") or {}).get("pcr_volume"),
              "flow_at": _now_naive_utc()}
    try:
        await asyncio.to_thread(db.upsert_simmer_research, merged)
    except Exception as e:
        log.warning("flow write-back failed for %s: %s", symbol, e)


# ─────────────────────────────────────────────────────────────────────────────
# Cross-sectional dispersion
# ─────────────────────────────────────────────────────────────────────────────
def apply_dispersion(results: dict[str, dict], regime: dict | None) -> dict | None:
    """Many names spiking IV together while the index reads calm is ONE sector
    shock, not many opportunities — the case none of the index fingerprints can
    see, because on the name itself the shock looks like opportunity (rich
    premium, no red flags). Suppress the affected sector cluster(s)."""
    symbols = {str(env.get("symbol")) for env in results.values()}
    if not symbols:
        state.dispersion = None
        return None
    spiking: set[str] = set()
    for env in results.values():
        ivp = (env.get("metrics") or {}).get("iv_percentile_effective")
        if ivp is not None and float(ivp) >= IV_SPIKE_PERCENTILE:
            spiking.add(str(env.get("symbol")))
    share = len(spiking) / len(symbols)
    threshold = float(simmer_config.regime().get("dispersion_threshold", 0.35))
    calm = bool((regime or {}).get("calm"))
    triggered = share > threshold and calm and len(spiking) >= 2
    clusters: set[str] = set()
    if triggered:
        for sym in spiking:
            clusters.add(str(simmer_config.ticker_rule(sym).get("sector") or "unknown"))
        for env in results.values():
            sym = str(env.get("symbol"))
            sector = str(simmer_config.ticker_rule(sym).get("sector") or "unknown")
            if sector in clusters:
                env["sector_dispersion_suppressed"] = True
                if "sector_dispersion" not in (env.get("avoid_if") or []):
                    env.setdefault("avoid_if", []).append("sector_dispersion")
        log.warning("sector dislocation: %.0f%% of watched names spiking IV in a "
                    "calm regime — suppressing cluster(s) %s",
                    share * 100, sorted(clusters))
    disp = {"share": round(share, 4), "spiking": sorted(spiking),
            "threshold": threshold, "regime_calm": calm,
            "triggered": triggered, "clusters": sorted(clusters)}
    state.dispersion = disp
    return disp


# ─────────────────────────────────────────────────────────────────────────────
# Alert state machine (hysteresis) + per-user fan-out
# ─────────────────────────────────────────────────────────────────────────────
def update_alert_state(key: str, env: dict, threshold: float,
                       now: float | None = None) -> bool:
    """Advance one (symbol, expiration) through cold/ready/cooling/vetoed.
    Returns True exactly when an alert should FIRE (a transition into `ready`
    from an armed state). Re-arms only after the score has stayed below
    threshold − HYSTERESIS_MARGIN for READY_DWELL_SEC — without that, a score
    oscillating around the threshold would emit an alert every sweep."""
    now = time.time() if now is None else now
    st = state.alert_states.get(key) or {"state": "cold", "since": now}
    cur = st["state"]
    vetoed = bool(env.get("veto_reasons"))
    suppressed = bool(env.get("sector_dispersion_suppressed"))
    score = float(env.get("score") or 0.0)
    ready_cond = (not vetoed) and (not suppressed) and score >= threshold
    fire = False

    if vetoed:
        st = {"state": "vetoed", "since": now}
    elif cur in ("cold", "vetoed"):
        if ready_cond:
            st = {"state": "ready", "since": now}
            fire = True
    elif cur == "ready":
        if score < threshold - HYSTERESIS_MARGIN or suppressed:
            st = {"state": "cooling", "since": now}
        # inside [threshold − margin, ∞): stay ready, no re-fire
    elif cur == "cooling":
        if ready_cond:
            st = {"state": "ready", "since": now}     # resumed — NOT a new alert
        elif score >= threshold - HYSTERESIS_MARGIN:
            st = {"state": "cooling", "since": now}   # dwell clock resets
        elif now - float(st.get("since") or now) >= READY_DWELL_SEC:
            st = {"state": "cold", "since": now}      # re-armed
    state.alert_states[key] = st
    return fire


def _passes_user_filters(env: dict, user_settings: dict, regime: dict | None) -> bool:
    """EACH user's own thresholds, applied at WRITE time. The shared readiness
    row is the engine's verdict; this is the per-user view over it."""
    score = float(env.get("score") or 0.0)
    ms = _num(user_settings.get("min_score"))
    if ms is not None and score < simmer_config.clamp_setting("min_score", ms):
        return False
    structs = user_settings.get("structures_enabled")
    if isinstance(structs, (list, tuple)) and structs \
            and env.get("structure") not in structs:
        return False
    strictness = str(user_settings.get("regime_strictness") or "balanced")
    reg_state = (regime or {}).get("state") or "calm"
    if strictness == "strict" and reg_state not in ("calm", "contango"):
        return False
    if strictness == "balanced" and reg_state == "stressed":
        return False
    return True


def _json_safe(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


async def fanout_alert(env: dict, regime: dict | None) -> int:
    """Write `simmer_alerts` rows via the service-role key (deliberately no RLS
    insert policy for users — only the backend writes alerts), one row per user
    watching this symbol whose own settings pass. Payload = the full engine
    envelope, so the UI can show WHY without recomputing and calibration can
    later ask which components predicted outcomes."""
    symbol = str(env.get("symbol") or "").upper()
    expiration = _iso_date(env.get("expiration"))
    rows = await supabase_admin.select_many(
        "simmer_watchlist", "user_id,expiration",
        {"symbol": f"eq.{symbol}", "active": "eq.true"})
    if rows is None:
        log.info("alert (supabase unconfigured, not fanned out): %s@%s score=%.1f %s",
                 symbol, expiration, float(env.get("score") or 0.0), env.get("structure"))
        state.alerts_fired += 1
        return 0
    written = 0
    payload = _json_safe(env)
    for r in rows:
        wexp = _iso_date(r.get("expiration"))
        if wexp and wexp != expiration:
            continue     # user pinned a different expiry for this name
        uid = r.get("user_id")
        if not uid:
            continue
        settings_row = await supabase_admin._select_one(
            "simmer_settings", "user_id", str(uid),
            "min_score,structures_enabled,regime_strictness,gate_overrides") or {}
        if not _passes_user_filters(env, settings_row, regime):
            continue
        ok = await supabase_admin.insert_row("simmer_alerts", {
            "user_id": uid,
            "symbol": symbol,
            "expiration": expiration,
            "score": round(float(env.get("score") or 0.0), 2),
            "state": "ready",
            "structure": env.get("structure"),
            "payload": payload,
        })
        written += 1 if ok else 0
    state.alerts_fired += 1
    return written


async def process_alerts(results: dict[str, dict], regime: dict | None) -> None:
    threshold = float(simmer_config.decision_bands()["ready"])
    for key, env in results.items():
        try:
            if update_alert_state(key, env, threshold):
                await fanout_alert(env, regime)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("alert processing failed for %s", key)


# ─────────────────────────────────────────────────────────────────────────────
# Daily IV-history job
# ─────────────────────────────────────────────────────────────────────────────
async def record_iv_history(db, results: dict[str, dict], provider=None) -> int:
    """Once per day, at/after the close (`_is_capture_window`): append one
    `simmer_iv_history` row per symbol — IV Rank is an end-of-day quantity.

    When the sweep's data provider supplies real daily OHLC (`daily_bars`,
    e.g. Yahoo), the row gets genuine open/high/low/close and a real
    Yang-Zhang RV — closing the long-noted history gap. When it does not
    (Tradier: `TradierClient` exposes no history endpoint, `daily_bars`
    returns []), the pre-provider fallback is preserved verbatim: OHL columns
    NULL, `close_px` approximates with the sweep spot, `rv20_yz` NULL, and RV
    falls back to zero-mean close-to-close over the cached closes (the
    engine's `rv_agreement` cross-check simply has one estimator).
    `atm_iv_xern` mirrors `atm_iv` until the earnings-hump removal ships with
    the Phase 3 calendar work."""
    if db is None:
        return 0
    today = date.today().isoformat()
    if state.last_iv_history_date == today:
        return 0
    # IV Rank is an END-OF-DAY quantity: only snapshot at/after the close (or on
    # a weekend/holiday off the last completed session), never mid-session — so
    # an off-hours boot no longer freezes IV history at whatever time the process
    # started. During market hours this returns without stamping the guard, so
    # the capture still happens once the session closes.
    if not _is_capture_window():
        return 0
    ann = int(simmer_config.vol()["annualization_days"])
    written = 0
    seen: set[str] = set()
    for env in results.values():
        sym = str(env.get("symbol") or "").upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        m = env.get("metrics") or {}
        try:
            bars: list[dict] = []
            if provider is not None:
                try:
                    # 21 trading days for rv20 + slack for holidays.
                    bars = await provider.daily_bars(sym, 30)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("daily_bars failed for %s (%s) — using "
                                "close-to-close fallback: %s",
                                sym, getattr(provider, "provider_name", "?"), e)
                    bars = []
            if bars:
                last_bar = bars[-1]
                rv_yz = simmer_engine.yang_zhang_rv(bars[-21:], ann)
                closes = [_num(b.get("close")) for b in bars[-21:]]
                closes = [c for c in closes if c]
                rv_cc = simmer_engine.close_to_close_rv(closes, ann) \
                    if len(closes) >= 4 else None
                row = {
                    "rv20_yz": rv_yz,
                    "rv20_cc": rv_cc,
                    "open_px": last_bar.get("open"),
                    "high_px": last_bar.get("high"),
                    "low_px": last_bar.get("low"),
                    "close_px": last_bar.get("close"),
                }
            else:
                prior = await asyncio.to_thread(db.fetch_simmer_iv_history, sym, 21)
                closes = [_num(r.get("close_px")) for r in prior]
                closes = [c for c in closes if c] + [env.get("spot")]
                rv_cc = simmer_engine.close_to_close_rv(closes, ann) \
                    if len(closes) >= 4 else None
                row = {
                    "rv20_yz": None,              # needs OHLC history (gap noted)
                    "rv20_cc": rv_cc,
                    "close_px": env.get("spot"),  # sweep spot ≈ close (gap noted)
                }
            await asyncio.to_thread(db.insert_simmer_iv_history, {
                "session_date": today,
                "symbol": sym,
                "atm_iv": m.get("atm_iv"),
                "atm_iv_xern": m.get("atm_iv"),   # ex-earnings removal: Phase 3
                "skew_25d": m.get("rr25_put_minus_call"),
                **row,
            })
            written += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("iv-history append failed for %s: %s", sym, e)
    if written or seen:
        state.last_iv_history_date = today
    return written


# ─────────────────────────────────────────────────────────────────────────────
# Paper-outcome evaluator
# ─────────────────────────────────────────────────────────────────────────────
def _outcome_held(structure: str | None, short_strike: float | None,
                  components_json: str | None, spot: float) -> bool | None:
    """Did the short strike(s) hold at expiry? Keyed on the ENGINE's persisted
    verdict — never on any user's filtered view."""
    legs = None
    try:
        legs = (json.loads(components_json or "{}").get("strikes") or {})
    except (TypeError, ValueError):
        legs = {}
    if structure == "iron_condor" and isinstance(legs.get("put"), dict) \
            and isinstance(legs.get("call"), dict):
        ps = _num(legs["put"].get("short"))
        cs = _num(legs["call"].get("short"))
        if ps is None or cs is None:
            return None
        return spot > ps and spot < cs
    if short_strike is None:
        return None
    if structure == "bear_call":
        return spot < float(short_strike)
    # bull_put (default: the persisted short column is the put short)
    return spot > float(short_strike)


def _outcome_touched(structure: str | None, short_strike: float | None,
                     components_json: str | None, bars: Sequence[dict],
                     entry_ts: Any, expiration: Any) -> bool | None:
    """Was the short strike breached INTRADAY at any point during the tenor
    (entry date → expiration)? Determinable only when the provider supplies
    daily high/low bars covering the whole tenor; anything less returns None
    (recorded, never guessed — the same posture `held` takes on a missing
    settlement read)."""
    if not bars:
        return None
    try:
        exp_d = date.fromisoformat(str(_iso_date(expiration)))
    except (TypeError, ValueError):
        return None
    start_d = None
    if isinstance(entry_ts, datetime):
        start_d = entry_ts.date()
    else:
        s = _iso_date(entry_ts)
        if s:
            try:
                start_d = date.fromisoformat(s)
            except ValueError:
                start_d = None
    if start_d is None or start_d > exp_d:
        return None
    window: list[dict] = []
    max_d: date | None = None
    for b in bars:
        try:
            bd = date.fromisoformat(str(b.get("date"))[:10])
        except (TypeError, ValueError):
            continue
        max_d = bd if (max_d is None or bd > max_d) else max_d
        if start_d <= bd <= exp_d:
            window.append(b)
    if not window or max_d is None or max_d < exp_d:
        return None      # bars don't (yet) cover the tenor — leave NULL
    highs = [h for h in (_num(b.get("high")) for b in window) if h is not None]
    lows = [lo for lo in (_num(b.get("low")) for b in window) if lo is not None]
    if not highs or not lows:
        return None
    if structure == "iron_condor":
        try:
            legs = json.loads(components_json or "{}").get("strikes") or {}
        except (TypeError, ValueError):
            legs = {}
        ps = _num((legs.get("put") or {}).get("short")) \
            if isinstance(legs.get("put"), dict) else None
        cs = _num((legs.get("call") or {}).get("short")) \
            if isinstance(legs.get("call"), dict) else None
        if ps is None or cs is None:
            return None
        return min(lows) <= ps or max(highs) >= cs
    if short_strike is None:
        return None
    if structure == "bear_call":
        return max(highs) >= float(short_strike)
    # bull_put (default: the persisted short column is the put short)
    return min(lows) <= float(short_strike)


async def evaluate_paper_outcomes(tradier, db, as_of: date | None = None) -> int:
    """Expired readiness rows without outcomes → one `simmer_outcomes` row each.

    Settlement spot = previous close via the provider quote (the loop runs
    every ~30 min, so the first post-expiry session grades the expiry-session
    close). `touched` (did the short strike get breached intraday during the
    tenor?) is filled when the provider's `daily_bars` covers the tenor
    (Yahoo); with the Tradier provider `daily_bars` is [] (no history
    endpoint) and `touched` stays NULL exactly as before. Snapshot columns
    (score_at_entry, components, engine_version) freeze the engine's verdict
    at emission time."""
    if db is None:
        return 0
    provider = as_provider(tradier)
    as_of = as_of or date.today()
    pending = await asyncio.to_thread(db.fetch_pending_simmer_outcomes, as_of)
    n = 0
    bars_by_sym: dict[str, list[dict]] = {}
    for row in pending:
        sym = str(row.get("symbol") or "").upper()
        try:
            quote = await provider.quote(sym)
            spot = _prev_close(quote)
            if not spot:
                continue     # no settlement read — retry next run
            held = _outcome_held(row.get("structure"), _num(row.get("short_strike")),
                                 row.get("components"), spot)
            adverse = None
            ks = _num(row.get("short_strike"))
            if held is False and ks:
                adverse = round(abs(spot - ks) / ks * 100.0, 4)
            if sym not in bars_by_sym:
                try:
                    bars_by_sym[sym] = await provider.daily_bars(sym, 60)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("daily_bars failed for %s — touched stays NULL: %s",
                                sym, e)
                    bars_by_sym[sym] = []
            touched = _outcome_touched(
                row.get("structure"), ks, row.get("components"),
                bars_by_sym[sym], row.get("ts"), row.get("expiration"))
            await asyncio.to_thread(db.insert_simmer_outcome, {
                "readiness_id": int(row["id"]),
                "symbol": sym,
                "expiration": row.get("expiration"),
                "evaluated_at": _now_naive_utc(),
                "spot_at_expiry": spot,
                "short_strike": ks,
                "held": held,
                "touched": touched,
                "max_adverse_pct": adverse,
                "score_at_entry": row.get("score"),
                "components": row.get("components"),
                "engine_version": row.get("engine_version"),
            })
            n += 1
            log.info("paper outcome %s@%s: spot=%.2f short=%s held=%s",
                     sym, row.get("expiration"), spot, ks, held)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("paper outcome failed for %s@%s", sym, row.get("expiration"))
    state.last_outcome_run_at = _utc_iso()
    state.last_outcome_run_ts = time.time()
    state.outcomes_recorded += n
    return n


# ─────────────────────────────────────────────────────────────────────────────
# The sweep + loop
# ─────────────────────────────────────────────────────────────────────────────
async def sweep(tradier, db, edgar=None) -> dict[str, dict]:
    """One full pass. Returns the {key: envelope} it computed (tests read it
    directly; production reads state.latest_by_key).

    `tradier` is coerced once, sweep-level, through `as_provider` — a raw
    TradierClient becomes a TradierDataProvider (identical data path), while
    `simmer_loop` hands in whatever `get_simmer_provider` chose from config."""
    provider = as_provider(tradier)
    # 0. Live EDGAR pass (2 requests max; ticker map cached 24 h) so the
    #    mandatory-invalidation check sees fresh hard-block filings.
    if edgar is not None and db is not None:
        try:
            await sweep_current_filings(edgar, db)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("EDGAR sweep failed (continuing): %s", e)

    pairs = await watchlist_union()
    symbols = sorted({s for s, _ in pairs})
    state.watched_count = len(symbols)

    regime = await compute_regime(provider)
    state.regime = regime

    # Tier-1 pass, once per SYMBOL (both expirations of a ticker share it).
    research_by_sym: dict[str, dict] = {}
    for sym in symbols:
        try:
            research_by_sym[sym] = await load_research(db, sym, provider)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("tier-1 research failed for %s", sym)
            state.last_error_by_symbol[sym] = f"{type(e).__name__}: {e}"
            research_by_sym[sym] = {"symbol": sym}

    # Cross-sectional peers: each symbol's live IV/RV from the PREVIOUS sweep
    # (see SimmerState.peer_iv_rv). Empty only on the very first sweep of a
    # fresh deploy; converges the sweep after.
    peer_vrp = [v for v in state.peer_iv_rv.values() if _num(v) is not None]

    results: dict[str, dict] = {}
    for sym, exp in pairs:
        try:
            env = await analyze_symbol(
                provider, db, sym, exp, regime=regime,
                research=research_by_sym.get(sym), peer_vrp=peer_vrp,
                persist=True)
            _ivrv = _num((env.get("metrics") or {}).get("vrp"))
            if _ivrv is not None:
                state.peer_iv_rv[sym.upper()] = _ivrv
            key = _key(sym, env.get("expiration"))
            results[key] = env
            state.latest_by_key[key] = env
            state.last_error_by_symbol.pop(sym, None)
            await _writeback_flow(db, sym, research_by_sym.get(sym) or {}, env)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("simmer evaluation failed for %s@%s", sym, exp)
            msg = f"{type(e).__name__}: {e}"
            state.last_error_by_symbol[sym] = msg
            state.last_error = f"{sym}: {msg}"

    # Daily IV-history append (first sweep of a session; idempotent per day).
    try:
        await record_iv_history(db, results, provider)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("iv-history job failed")

    # Sector-dispersion check, then alerts over the (possibly suppressed) set.
    apply_dispersion(results, regime)
    await process_alerts(results, regime)

    # Prune per-key state for pairs no longer WATCHED — without this,
    # latest_by_key / alert_states / the error map grow forever as users add
    # and remove tickers (slow leak flagged in security review). Keyed on the
    # union, NOT on this sweep's results: a watched symbol that merely FAILED
    # must keep its error record and its alert state. Alert hysteresis for a
    # re-added pair restarts from cold — the conservative behavior anyway.
    watched_syms = set(symbols)
    watched_keys = {_key(s, e) for s, e in pairs} | set(results.keys())
    for m in (state.latest_by_key, state.alert_states):
        for k in [k for k in m
                  if "|" in k and k not in watched_keys
                  and k.split("|", 1)[0] not in watched_syms]:
            m.pop(k, None)
    for s in [s for s in state.last_error_by_symbol if s not in watched_syms]:
        state.last_error_by_symbol.pop(s, None)

    state.last_sweep_at = _utc_iso()
    state.sweep_count += 1
    return results


def _row_to_envelope(row: dict) -> dict:
    """Reconstruct a serviceable envelope from a persisted `simmer_readiness`
    row, for startup rehydration and DB-fallback reads. Not byte-identical to a
    live envelope (the full candidate set isn't persisted), but carries
    everything the UI renders — decision, score, veto reasons, components,
    strikes, credits, POP/EV/alpha — and is marked `rehydrated` so the client
    can show its age instead of implying a fresh sweep."""
    bands = simmer_config.decision_bands()
    score = _num(row.get("score"))
    if row.get("vetoed"):
        decision = "vetoed"
    elif score is None:
        decision = "unknown"
    elif score >= float(bands.get("ready", 70)):
        decision = "ready"
    elif score >= float(bands.get("watch", 50)):
        decision = "watch"
    else:
        decision = "avoid"

    def _j(key):
        v = row.get(key)
        if not v:
            return [] if key == "veto_reasons" else {}
        try:
            return json.loads(v) if isinstance(v, str) else v
        except (ValueError, TypeError):
            return [] if key == "veto_reasons" else {}

    env = {
        "symbol": row.get("symbol"),
        "expiration": _iso_date(row.get("expiration")),
        "computed_at": row.get("ts"),
        "rehydrated": True,
        "market_open": False,   # rehydrated rows are by definition not from this instant

        "decision": decision,
        "score": score,
        "vetoed": bool(row.get("vetoed")),
        "veto_reasons": _j("veto_reasons"),
        "components": _j("components"),
        "regime": row.get("regime"),
        "structure": row.get("structure"),
        "spot": _num(row.get("spot")),
        "strikes": ({"short": _num(row.get("short_strike")),
                     "long": _num(row.get("long_strike")),
                     "width": _num(row.get("width"))}
                    if row.get("short_strike") is not None else None),
        "credit_mid": _num(row.get("credit_mid")),
        "credit_fill": _num(row.get("credit_fill")),
        "max_loss": _num(row.get("max_loss")),
        "pop_breakeven": _num(row.get("pop_breakeven")),
        "expected_value": _num(row.get("expected_value")),
        "alpha": _num(row.get("alpha")),
        "engine_version": row.get("engine_version"),
    }
    return env


def rehydrate_readiness(db) -> int:
    """Load the newest persisted readiness per (symbol, expiration) into
    `state.latest_by_key` BEFORE the loop's first sweep. A restart or a
    market-closed idle period must never hide results sitting on disk — a
    signed-in user sees their tickers' last analysis immediately, stamped
    `rehydrated` with its original timestamp. Mirrors evaluator's
    rehydrate_regime precedent."""
    if db is None:
        return 0
    try:
        rows = db.latest_simmer_readiness_all()
    except Exception as e:
        log.warning("readiness rehydrate failed: %s", e)
        return 0
    n = 0
    for row in rows:
        env = _row_to_envelope(row)
        key = _key(env["symbol"], env["expiration"])
        state.latest_by_key.setdefault(key, env)
        n += 1
    if n:
        log.info("rehydrated %d readiness entries from DuckDB", n)
    return n


async def _resolve_sweep_seconds() -> int:
    """Open-market sweep cadence in seconds.

    A UI/CLI-set simmer_settings.sweep_interval (MINUTES, migration 0011) drives
    it — the sweep is ONE global loop, so the MIN across all rows wins (the
    tightest cadence any user asked for) — falling back to the config default
    (simmer_config CADENCE.sweep_seconds) when Supabase is unconfigured or no row
    sets it. Read straight from Supabase (no DuckDB mirror), so a change applies
    on the next sweep and a container restart picks it up on the first cycle."""
    cfg_default = max(30, int(simmer_config.cadence().get("sweep_seconds", 300)))
    rows = await supabase_admin.select_many("simmer_settings", "sweep_interval")
    if not rows:                       # None = Supabase off; [] = no rows
        return cfg_default
    mins = [int(r["sweep_interval"]) for r in rows
            if r.get("sweep_interval") is not None]
    return max(60, min(mins) * 60) if mins else cfg_default


async def simmer_loop(tradier_client, db, settings) -> None:
    """Started by main.py lifespan as `edgelane.simmer_loop`, alongside
    poll_task and eval_task. Market-hours gated via poller._is_market_open;
    honours the same poll-when-closed policy the poller resolves upstream."""
    state.is_running = True
    try:
        await asyncio.to_thread(rehydrate_readiness, db)
    except Exception as e:
        log.warning("rehydrate skipped: %s", e)
    # The sweep's market-data source is a config switch (SIMMER_DATA_PROVIDER):
    # "tradier" (default — identical to the pre-abstraction behavior) or
    # "yahoo" (keeps the sweep off Tradier's 120 req/min budget). Constructed
    # ONCE here, sweep-level, so per-provider caches/sessions persist.
    #
    # ⚠️ `settings` here is main.py's `_settings_view()` SimpleNamespace — the
    # poller's duck-typed COPY, which does NOT carry simmer_data_provider (a
    # getattr default would silently pin the provider to tradier forever, which
    # is exactly what happened in live testing). Read the real Settings for
    # provider selection; keep the passed-in view for the poll-cadence fields
    # it does carry.
    from .config import get_settings as _real_settings
    provider = get_simmer_provider(_real_settings(), tradier_client)
    # Publish for the routes: /simmer/analyze must run on the SAME provider as
    # the sweep, or on-demand and scheduled results diverge (e.g. Tradier has no
    # daily bars → no RV → veto, while the yahoo sweep scores normally).
    state.provider = provider
    log.info("simmer data provider: %s", provider.provider_name)
    tz_name = getattr(settings, "market_hours_tz", "America/New_York")
    poll_when_closed = bool(getattr(settings, "should_poll_when_closed",
                                    getattr(settings, "force_poll_when_closed", False)))
    # Open-market cadence: UI/CLI-set sweep_interval (minutes) overrides the
    # config default. Re-read each cycle below — a restart applies it on the
    # first pass, and a later change applies on the next sweep, no restart.
    interval = await _resolve_sweep_seconds()
    # Simmer is a MULTI-DAY premium tool on Yahoo data (no Tradier quota to
    # protect), so unlike the intraday 0DTE poller it ALSO sweeps off-hours —
    # from the last session's closing chain, which is the correct basis for a
    # spread you would open next session. Off-hours it just sweeps slowly.
    closed_interval = max(300, int(simmer_config.cadence().get(
        "closed_sweep_seconds", 3600)))
    edgar = None
    try:
        from .simmer_catalysts import EdgarClient
        edgar = EdgarClient()
    except Exception as e:
        log.warning("EDGAR client unavailable: %s", e)
    log.info("simmer loop started — interval=%ss tz=%s closed_policy=%s",
             interval, tz_name, "sweep-anytime" if poll_when_closed else "skip-off-hours")
    try:
        while True:
            try:
                open_, reason = _is_market_open(tz_name)
                state.market_open = open_
                state.market_reason = reason
                # Sweep in BOTH states — closed just uses last-session data and
                # a slower cadence. A user who adds a ticker on a weekend still
                # gets a readiness card (stamped last-session), instead of an
                # indefinite "pending".
                await sweep(provider, db, edgar=edgar)
                now = time.time()
                if state.last_outcome_run_ts is None \
                        or now - state.last_outcome_run_ts >= OUTCOME_INTERVAL_SEC:
                    try:
                        n = await evaluate_paper_outcomes(provider, db)
                        if n:
                            log.info("recorded %d paper outcomes", n)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("paper-outcome sweep failed")
                        state.last_outcome_run_ts = time.time()
                # Re-resolve each cycle so a UI/CLI sweep_interval change lands
                # on the next sweep without a restart (falls back to config).
                interval = await _resolve_sweep_seconds()
                await asyncio.sleep(interval if state.market_open else closed_interval)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("simmer_loop outer error — sleeping 10s then retrying")
                state.last_error = f"{type(e).__name__}: {e}"
                await asyncio.sleep(10)
    finally:
        state.is_running = False
        if edgar is not None:
            try:
                await edgar.close()
            except Exception:
                pass
        if hasattr(provider, "close"):
            try:
                await provider.close()
            except Exception:
                pass
        log.info("simmer loop stopped")
