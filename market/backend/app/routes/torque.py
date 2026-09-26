"""Torque — standalone fast multi-leg order builder.

Serves `torque.html` and the JSON API behind it:
  GET  /torque                 → the page
  GET  /torque/config          → ticker list + strategy registry + defaults
  GET  /torque/analyze/{sym}   → spot + directional lean (poll ~5s)
  POST /torque/build           → auto-filled legs for (ticker, strategy, steps)
  POST /torque/price           → live net bid/mid/ask for a set of legs (poll ~2s)
  POST /torque/place           → place entry, confirm fill, auto-place +30% close
  GET  /torque/order/{id}      → single order status (fill polling)

Auto-close is HYBRID: a single-leg entry with a limit price is sent as a native
Tradier OTO bracket (broker-held). Everything else (spreads, market entries) is
app-managed: place → poll until FILLED → then place the close, so a rejected or
unfilled entry never leaves a naked close order behind.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .. import auth
from ..auth import get_current_user
from ..config import get_settings
from ..broker_resolver import resolve_broker
from ..entitlements import ensure_tool
from .. import supabase_admin
from ..order_builder import build_tradier_order_payload
from .. import torque_config as tcfg
from .. import torque_engine as teng

log = logging.getLogger("edgelane.market.torque")
TORQUE_VERSION = "0.2.0"
router = APIRouter()

# Access gate for Torque's data/action endpoints. get_current_user maps the
# admin token (X-Admin-Token / ?token=) to auth='admin' and returns a synthetic
# dev user when AUTH_ENABLED=false. ensure_tool() then allows ONLY: the admin,
# dev mode, or a signed-in user entitled to the "torque" tool
# (profiles.tools_enabled). A bare Supabase JWT is NOT enough — without this, any
# signed-in user could POST /torque/place, bypassing the page's admin-token gate.
#
# Broker routing is separate (see _resolve_torque_broker): an entitled user
# trades their OWN active broker connection; the admin/dev path uses the house
# account.
async def require_torque_access(request: Request,
                                user: dict = Depends(get_current_user)) -> dict:
    await ensure_tool(user, "torque")
    return user


_GATE = [Depends(require_torque_access)]


async def _resolve_torque_broker(request: Request, user: dict,
                                 req_account: str | None = None):
    """Resolve the broker client + account for a Torque TRADING action.

    Mirrors the market /orders path: an entitled signed-in user trades their own
    active broker connection; the admin/dev (server-owner) path uses the house
    client + configured house account.

    Tradier and Webull are both accepted. Webull is capability-limited (no OTO
    bracket, no order-status/cancel/modify surface on WebullClient), so the
    individual routes gate on `broker` — see _require_tradier().

    Returns (broker, client, account_id, per_user). When per_user is True the
    caller owns the client and MUST close it (or hand it to the watcher).
    """
    broker, client, account_override, per_user = await resolve_broker(request, user)
    if broker not in ("tradier", "webull"):
        if per_user:
            await client.close()
        raise HTTPException(400, f"Torque does not support the {broker!r} broker.")
    if broker == "webull":
        account_id = (account_override or "").strip() or (req_account or "").strip()
        if not account_id:
            try:
                account_id = await client.first_account_id()
            except Exception as e:
                await client.close()
                raise HTTPException(502, f"could not resolve your Webull account: {e}")
        if not account_id:
            await client.close()
            raise HTTPException(400, "could not determine a Webull account from your connection.")
        return broker, client, account_id, per_user
    if per_user:
        account_id = (account_override or "").strip() or (req_account or "").strip()
        if not account_id:
            try:
                account_id = await client.resolve_account_id()
            except Exception as e:
                await client.close()
                raise HTTPException(502, f"could not resolve your Tradier account: {e}")
        if not account_id:
            await client.close()
            raise HTTPException(400, "could not determine an account from your broker connection; "
                                     "set the account id on your broker connection in Settings.")
    else:
        account_id = _account_id(req_account)
    return broker, client, account_id, per_user


async def _require_tradier(broker: str, client, per_user: bool, what: str):
    """Torque features that have no Webull equivalent yet. WebullClient exposes
    only list_accounts / first_account_id / preview_option / place_option — no
    order status, cancel, or modify — so these routes cannot be served for a
    Webull connection. Fail with a clear 501 instead of an AttributeError."""
    if broker != "tradier":
        if per_user:
            await client.close()
        raise HTTPException(501, f"{what} is not available for {broker} connections yet.")


def _fee_kw(symbol: str, legs) -> dict:
    """Fee params for close_target_price: commission is per contract PER LEG,
    charged on the open and again on the close. Sourced from torque_config,
    calibrated against Tradier's own order-preview `commission` field."""
    n = len(legs) if isinstance(legs, (list, tuple)) else int(legs or 1)
    return {"legs": n, "fee_per_contract": tcfg.fee_per_contract(symbol)}


_OCC_RE = re.compile(r"^[A-Z]+(\d{6})[CP]\d{8}$")


def _expiration_from_occ(sym: str) -> str | None:
    """`DJXW260710C00526000` → `2026-07-10`. Webull legs are structured
    (strike + expiry + type) rather than OCC strings, so the expiration has to
    be recovered from the symbol Torque already resolved off the chain."""
    m = _OCC_RE.match((sym or "").upper())
    if not m:
        return None
    yy, mm, dd = m.group(1)[0:2], m.group(1)[2:4], m.group(1)[4:6]
    return f"20{yy}-{mm}-{dd}"


def _page_authorized(request: Request) -> bool:
    """Authorize the initial page navigation. Browsers can't attach a custom
    header on a top-level GET, so the shell accepts ?token=<ADMIN_API_TOKEN> in
    the URL (the page then stores it and sends it as X-Admin-Token on fetches).
    A header/Bearer is also honored if present."""
    settings = get_settings()
    if not settings.auth_enabled:
        return True
    if auth._admin_token_ok(request):
        return True
    tok = (request.query_params.get("token") or "").strip()
    if settings.admin_api_token and tok == settings.admin_api_token:
        return True
    return False

_HTML_PATH = Path(__file__).resolve().parents[2] / "ui" / "torque.html"
_ASSETS_DIR = Path(__file__).resolve().parents[2] / "ui" / "assets"

# Fill-confirmation poll cadence (module-level so tests can shrink them).
_POLL_TIMEOUT = 30.0
_POLL_INTERVAL = 1.5

# Background auto-close watcher: a limit entry may fill minutes after the
# request returns, so we DON'T block the request waiting — we register a watcher
# that polls until the entry fills (or the day ends) and then places the close.
_WATCH_TIMEOUT = 6 * 3600.0     # stop watching after ~a trading day
_WATCH_INTERVAL = 5.0
_CLOSE_RETRIES = 4              # retry the close if the position isn't settled yet
_CLOSE_RETRY_DELAY = 2.0
# entry_order_id -> watcher state dict (also holds the asyncio task ref so it's
# not garbage-collected mid-flight).
_WATCHERS: dict[str, dict] = {}

# Stop-loss poll cadence (slower than the fill poll — this runs for hours).
_STOP_INTERVAL = 10.0

# close_order_id -> {symbol, entry_type, entry_fill, tick, floor_pct}
# Populated when a profit-target close is placed, so /torque/modify can re-apply
# the ticker's auto-close floor if the user edits that order in the orders panel.
# Without this, editing the close price bypasses every guardrail /torque/place
# enforced (the modify payload carries only a price — no symbol, no strategy).
_CLOSE_GUARD: dict[str, dict] = {}

# tiny TTL cache so analyze + build hitting within one tick share a chain fetch
_CHAIN_CACHE: dict[str, tuple[float, float, str, list[dict]]] = {}
_CHAIN_TTL = 2.0
_CHAIN_FAIL: dict[str, tuple[float, int, str]] = {}   # sym -> (t, status, detail)
_CHAIN_FAIL_TTL = 4.0   # brief negative cache: analyze/build/price all call _get_chain,
                        # so during a Tradier blip serve the last error for this long
                        # instead of each poll re-hitting — recovers within ~4s.

# Yahoo live-spot source (primary; Tradier parity is the fallback).
_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_YAHOO_TIMEOUT = 2.0


async def _yahoo_spot(ticker: str) -> float | None:
    """Live regularMarketPrice from Yahoo, or None on any failure (so the
    caller falls back to Tradier parity). Short timeout — must not stall the
    3s poll. Wrapped by the 2s chain cache, so hit at most ~every 2s."""
    ysym = tcfg.yahoo_symbol(ticker)
    if not ysym:
        return None
    try:
        async with httpx.AsyncClient(timeout=_YAHOO_TIMEOUT) as cli:
            r = await cli.get(_YAHOO_URL.format(sym=ysym),
                              headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]["meta"]
            px = res.get("regularMarketPrice") or res.get("previousClose")
            return float(px) if px is not None else None
    except Exception as e:                       # network, parse, http — all soft
        log.warning("yahoo spot failed for %s (%s): %s", ticker, ysym, e)
        return None


_MKT_CACHE: dict = {"t": 0.0, "state": None, "open": None}
_MKT_OK_TTL = 30.0     # serve a good read this long
_MKT_FAIL_TTL = 8.0    # serve a FAILED read briefly too, so an outage doesn't
                       # re-hit every poll — still recovers within seconds
_NY_TZ = ZoneInfo("America/New_York")

# The whole month's calendar, refetched only when the month rolls over — one
# Tradier call a day in practice, not one per /torque/clock poll.
_CALENDAR_CACHE: dict = {"ym": None, "days": {}}


async def _tradier_calendar_days(client) -> dict[str, dict]:
    """This month's Tradier trading calendar, keyed by `YYYY-MM-DD`."""
    now_ny = datetime.now(_NY_TZ)
    ym = (now_ny.year, now_ny.month)
    if _CALENDAR_CACHE["ym"] == ym:
        return _CALENDAR_CACHE["days"]
    try:
        d = await client.market_calendar(now_ny.month, now_ny.year)
        raw = ((d.get("calendar") or {}).get("days") or {}).get("day") or []
        if isinstance(raw, dict):
            raw = [raw]
        days = {x["date"]: x for x in raw if x.get("date")}
        _CALENDAR_CACHE.update(ym=ym, days=days)
        return days
    except Exception as e:
        log.warning("tradier market calendar fetch failed: %s", e)
        return _CALENDAR_CACHE["days"]   # stale beats nothing; {} if never fetched


def _hhmm_today(hhmm: str, now_ny: datetime) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return now_ny.replace(hour=h, minute=m, second=0, microsecond=0)


async def _tradier_market_state(client) -> tuple[str | None, bool | None]:
    """Authoritative open/closed from Tradier's own market calendar — holidays
    and early closes both handled exactly, no external dependency (Torque
    already has the credentials/client). Recommended by
    facades-news-reactor's own integration doc (2026-09-26) after finding
    Yahoo's `marketState` field had silently disappeared from the v8 chart
    endpoint; this replaces that Yahoo-based check entirely rather than
    layering on top of it. Verified against a real closed day (Thanksgiving
    2026-11-26) and a real early close (2026-11-27, 13:00) via the live API.

    Returns (state, open) mirroring the old Yahoo shape (PRE/REGULAR/POST/
    CLOSED, open bool) so the frontend's existing label map needs no change.
    (None, None) only on a total calendar-fetch failure with nothing cached
    yet."""
    now_ny = datetime.now(_NY_TZ)
    date_str = now_ny.strftime("%Y-%m-%d")
    days = await _tradier_calendar_days(client)
    day = days.get(date_str)
    if not day:
        return None, None
    if str(day.get("status") or "").lower() == "closed":
        return "CLOSED", False
    for label, is_open in (("open", True), ("premarket", False), ("postmarket", False)):
        window = day.get(label) or {}
        start, end = window.get("start"), window.get("end")
        if not (start and end):
            continue
        if _hhmm_today(start, now_ny) <= now_ny < _hhmm_today(end, now_ny):
            return ("REGULAR" if is_open else ("PRE" if label == "premarket" else "POST")), is_open
    return "CLOSED", False


# ── helpers ────────────────────────────────────────────────────────────────
def _client(request: Request):
    c = getattr(request.app.state, "tradier", None)
    if c is None:
        raise HTTPException(500, "tradier client not initialized")
    return c


def _account_id(req_account: str | None = None) -> str:
    settings = get_settings()
    from .. import main as _main
    aid = (req_account or settings.active_account_id
           or getattr(_main, "_tradier_account_id", "") or "").strip()
    if not aid:
        raise HTTPException(400, "no account_id (set TRADIER_ACCOUNT_ID[_SANDBOX] or pass account_id)")
    return aid


async def _get_chain(client, symbol: str) -> tuple[float, str, list[dict]]:
    """Return (spot, expiration, normalized_contracts) with a 2s TTL cache."""
    sym = symbol.upper()
    now = time.time()
    hit = _CHAIN_CACHE.get(sym)
    if hit and now - hit[0] < _CHAIN_TTL:
        return hit[1], hit[2], hit[3]
    # brief negative cache: serve the last error instead of re-hitting Tradier on
    # every poll during an outage (recovers after _CHAIN_FAIL_TTL).
    fail = _CHAIN_FAIL.get(sym)
    if fail and now - fail[0] < _CHAIN_FAIL_TTL:
        raise HTTPException(fail[1], fail[2])
    try:
        q = await client.stock_quote(sym)
        spot = teng._f(q.get("last") or q.get("close") or q.get("prevclose"))
        exps = await client.option_expirations(sym)
        cands = teng.expiration_candidates(exps)
        if not cands:
            raise HTTPException(404, f"no listed option expirations for {sym}")
        # Walk forward until an expiration yields a tradeable chain. For tickers
        # with a root allowlist (DJX→DJXW) the monthly 3rd-Friday dates list only
        # the excluded AM-settled root and normalize to []; skip them.
        exp, contracts = None, []
        for cand in cands:
            raw = await client.options_chain(sym, cand)
            rows = teng.normalize_chain(raw, sym)
            if rows:
                exp, contracts = cand, rows
                break
        if not contracts:
            raise HTTPException(404, f"no tradeable option chain for {sym} in {cands}")
    except HTTPException as e:
        _CHAIN_FAIL[sym] = (now, e.status_code, e.detail if isinstance(e.detail, str) else str(e.detail))
        raise
    except Exception as e:
        msg = f"Tradier chain fetch failed for {sym}: {e}"
        _CHAIN_FAIL[sym] = (now, 502, msg)
        raise HTTPException(502, msg)
    # Spot source order: Yahoo live quote → Tradier put-call parity → raw broker
    # quote → median strike. Yahoo is the primary live print; parity covers a
    # Yahoo outage and is computed from the real-time option bid/ask we already
    # hold (the broker's own derived index quote lags during fast moves).
    parity = teng.implied_spot(contracts, fallback=spot)
    spot = await _yahoo_spot(sym) or parity
    if spot is None and contracts:
        ks = sorted(c["strike"] for c in contracts)
        spot = ks[len(ks) // 2]
    _CHAIN_CACHE[sym] = (now, spot, exp, contracts)
    _CHAIN_FAIL.pop(sym, None)   # recovered — drop any stale negative-cache entry
    return spot, exp, contracts


def _grid_neighbors(contracts: list[dict], side: str, strike: float, span: int = 6) -> list[float]:
    grid = teng._grid(contracts, side)
    if strike not in grid and grid:
        strike = teng._nearest(grid, strike)
    if strike not in grid:
        return grid[:span]
    i = grid.index(strike)
    return grid[max(0, i - span): i + span + 1]


# ── page + config ──────────────────────────────────────────────────────────
@router.get("/torque")
async def torque_page(request: Request):
    if not _page_authorized(request):
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Torque — locked</title>"
            "<body style='background:#0b0f14;color:#aab7c5;font:15px system-ui;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'><h2 style='color:#36d399'>Torque is locked</h2>"
            "<p>Append <code>?token=YOUR_ADMIN_TOKEN</code> to the URL to access.</p></div>",
            status_code=401, headers={"Cache-Control": "no-store, max-age=0"})
    if not _HTML_PATH.is_file():
        raise HTTPException(404, f"torque.html not found at {_HTML_PATH}")
    # never cache the page — so UI fixes land on a normal reload, not only a
    # hard refresh.
    return FileResponse(str(_HTML_PATH), media_type="text/html",
                        headers={"Cache-Control": "no-store, max-age=0"})


@router.get("/torque/assets/{name}")
async def torque_asset(name: str):
    """Serve the logo / favicon from market/backend/ui/assets."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "", name)
    f = _ASSETS_DIR / safe
    if not safe or not f.is_file():
        raise HTTPException(404, "asset not found")
    media = "image/svg+xml" if safe.endswith(".svg") else "application/octet-stream"
    return FileResponse(str(f), media_type=media, headers={"Cache-Control": "max-age=3600"})


@router.get("/torque/config", dependencies=_GATE)
async def torque_cfg():
    settings = get_settings()
    return {
        "tickers": tcfg.tickers(),
        "default_strategy": tcfg.default_strategy(),
        "strategies": tcfg.strategy_list(),
        "close_target_pct": tcfg.DEFAULT_CLOSE_TARGET_PCT,
        # Per-ticker {default,min} auto-close profit targets. `min` is a hard
        # floor the server clamps up to (wide-spread names like DJX cannot be
        # closed at the global 30% default) — the UI should prefill `default`
        # and refuse to submit below `min`.
        "close_targets": tcfg.close_targets_map(),
        # Per-ticker stop-loss default — DJX 50%, everyone else 30% globally.
        # See stop_loss_defaults_map()'s docstring for why the UI needs this
        # rather than just always sending a flat 30%.
        "stop_loss_defaults": tcfg.stop_loss_defaults_map(),
        # Per-ticker Counter Read noise floor — below this, both compared sides'
        # mids being under it means the reading is 0DTE-close penny noise, not a
        # real richness signal, so the UI suppresses it (see Counter Read docs).
        "richness_floors": tcfg.richness_floors_map(),
        "env": settings.tradier_env,
        "mode": settings.tradier_mode,
        "devmode": bool(getattr(settings, "devmode", False)),
        "version": TORQUE_VERSION,
    }


@router.get("/torque/clock", dependencies=_GATE)
async def torque_clock(request: Request):
    """Authoritative market-open flag from Tradier's own market calendar —
    handles holidays and early closes exactly, no hardcoded calendar and no
    external (Yahoo) dependency. Cached ~30s for the open/closed instant
    check (cheap — a local time comparison, no network call); the underlying
    calendar itself is cached for the whole month (see _tradier_calendar_days
    — about one Tradier call a day, not one per poll). `open` is None
    (unknown) only if the calendar fetch has never once succeeded, so the
    client falls back to its own weekday+time gate."""
    now = time.time()
    # serve cache while fresh — a good read for _MKT_OK_TTL, a failed one (open
    # still None) for the shorter _MKT_FAIL_TTL so a sustained outage is
    # shielded, not spammed.
    ttl = _MKT_OK_TTL if _MKT_CACHE["open"] is not None else _MKT_FAIL_TTL
    if _MKT_CACHE["t"] and now - _MKT_CACHE["t"] < ttl:
        return {"market_state": _MKT_CACHE["state"], "open": _MKT_CACHE["open"]}
    st, is_open = await _tradier_market_state(_client(request))
    _MKT_CACHE.update(t=now, state=st, open=is_open)
    return {"market_state": st, "open": is_open}


# ── analysis ───────────────────────────────────────────────────────────────
@router.get("/torque/analyze/{symbol}", dependencies=_GATE)
async def torque_analyze(symbol: str, request: Request):
    client = _client(request)
    spot, exp, contracts = await _get_chain(client, symbol)
    result = teng.analyze(spot, contracts)
    result["symbol"] = symbol.upper()
    result["expiration"] = exp
    return result


# ── build (auto-fill legs) ─────────────────────────────────────────────────
class BuildRequest(BaseModel):
    symbol: str
    strategy: str
    adjustments: dict[str, int] = Field(default_factory=dict)
    anchor_spot: float | None = None   # Lock Strikes: derive strikes from this frozen spot instead of live


@router.post("/torque/build", dependencies=_GATE)
async def torque_build(req: BuildRequest, request: Request):
    if req.strategy not in tcfg.STRATEGY_DEFS:
        raise HTTPException(400, f"unknown strategy {req.strategy!r}")
    client = _client(request)
    spot, exp, contracts = await _get_chain(client, req.symbol)
    # Lock Strikes: when the client sends a frozen anchor spot, derive the strikes
    # from THAT (so they stop re-centering as price moves). Prices still come from
    # the live chain below, so premiums keep updating for the locked strikes.
    anchor = req.anchor_spot if (req.anchor_spot and req.anchor_spot > 0) else spot
    struct = teng.build_structure(anchor, contracts, req.symbol, req.strategy, req.adjustments)
    px_map = {c["symbol"]: c for c in contracts if c.get("symbol")}
    price = teng.price_structure(struct["legs"], px_map)
    tick = float(struct["rule"].get("tick", 0.05))
    # attach steppable grid neighbors per leg
    for lg in struct["legs"]:
        lg["grid"] = _grid_neighbors(contracts, lg["side"], lg["strike"])
    missing = [lg["role"] for lg in struct["legs"] if not lg.get("symbol")]
    # Relative-richness read: also price the natural opposite structure (own
    # independently-configured strikes, not a mirror at the same strikes — see
    # COUNTER_STRATEGY) off this SAME already-fetched chain/anchor, so both
    # sides are guaranteed from one consistent snapshot instead of two calls
    # that could straddle a spot tick. Best-effort: any failure here must never
    # break the primary build the user is actually trying to place.
    counter = None
    counter_key = tcfg.COUNTER_STRATEGY.get(req.strategy)
    if counter_key:
        try:
            c_struct = teng.build_structure(anchor, contracts, req.symbol, counter_key, {})
            c_price = teng.price_structure(c_struct["legs"], px_map)
            counter = {
                "strategy": counter_key, "name": c_struct["name"], "type": c_struct["type"],
                "legs": c_struct["legs"], "price": c_price,
            }
        except Exception:
            log.warning("counter build failed for %s -> %s", req.strategy, counter_key, exc_info=True)
    return {
        "symbol": req.symbol.upper(), "expiration": exp, "spot": spot,
        "strategy": req.strategy, "name": struct["name"], "type": struct["type"],
        "legs": struct["legs"], "price": price, "tick": tick,
        "suggested_limit": teng.suggested_limit(price, tick),
        "missing_legs": missing,
        "counter": counter,
    }


# ── live price ─────────────────────────────────────────────────────────────
class PriceRequest(BaseModel):
    legs: list[dict]            # [{symbol, action, quantity, side, strike, role}]
    symbol: str | None = None   # underlying (optional; recovered from legs if absent)
    strategy: str | None = None # optional; only used to resolve `tick`


@router.post("/torque/price", dependencies=_GATE)
async def torque_price(req: PriceRequest, request: Request):
    client = _client(request)
    syms = [l.get("symbol") for l in req.legs if l.get("symbol")]
    if not syms:
        raise HTTPException(400, "no leg symbols")
    # Price straight from the (deduped, live-root) chain — the SAME source as
    # /torque/build — so the displayed net never disagrees with the legs and
    # can't drop a leg the way a separate /markets/quotes call can. The symbol
    # in each leg is the chain's canonical OCC, so it always matches.
    sym = (req.symbol or _underlying_from_legs(req.legs))
    if not sym:
        raise HTTPException(400, "cannot determine underlying for pricing")
    _, _, contracts = await _get_chain(client, sym)
    px = {c["symbol"]: c for c in contracts if c.get("symbol")}
    out = teng.price_structure(req.legs, px)

    # Cost-of-crossing, surfaced so the Send button can label what you ACTUALLY
    # pay vs fair value. `cross_cost` is not a fee — it's the amount you overpay
    # (debit) or under-collect (credit) by taking the marketable price instead of
    # the mid. It shows up as an instant unrealized loss, never as a line item.
    sp = teng.package_spread_pct(out)
    out["package_spread_pct"] = round(sp, 2) if sp is not None else None
    out["market_orders_allowed"] = tcfg.market_orders_allowed(sym)
    out["auto_close_required"] = tcfg.close_target_required(sym)
    out["untradeable"] = bool(sp is not None and sp > tcfg.MAX_AUTO_CLOSE_SPREAD_PCT)
    if out.get("complete"):
        if out["type"] == "credit":
            out["marketable_price"] = out["abs_bid"]          # you receive the bid
            out["cross_cost"] = round(abs(out["abs_mid"] - out["abs_bid"]), 4)
        else:
            out["marketable_price"] = out["abs_ask"]          # you pay the ask
            out["cross_cost"] = round(abs(out["abs_ask"] - out["abs_mid"]), 4)
    else:
        out["marketable_price"] = out["cross_cost"] = None

    # LIVE auto-close floor — the same number /torque/place will clamp to, so the
    # UI's min never disagrees with the server's.
    floor = tcfg.close_target_min(sym)
    if tcfg.close_target_spread_scaled(sym) and sp is not None:
        floor = max(floor, sp)
    out["min_close_target_pct"] = round(min(floor, 500.0), 2)

    # PRE-CALCULATED close economics. The entry fill isn't known yet, so both
    # scenarios are shown: filling passively at the mid, and crossing to the
    # marketable price. Both fold in the round-trip commission (per contract PER
    # LEG, open + close), so `breakeven_*` is the price at which the trade nets
    # exactly zero and `close_target_*` is the price that nets the floor %.
    # `tick` is a per-ticker property of the merged rule, identical across
    # strategies, so any strategy resolves it. Fall back if the name is unknown.
    try:
        tick = float(tcfg.ticker_rule(sym, req.strategy or "long_call")["tick"])
    except KeyError:
        tick = float(tcfg.ticker_rule(sym, "long_call")["tick"])
    fee = tcfg.fee_per_contract(sym)
    nlegs = len(req.legs)
    out["fee_per_contract"] = fee
    out["round_trip_fee"] = round(teng.round_trip_fee_price(fee, nlegs), 4)
    out["tick"] = tick
    if out.get("complete") and out.get("abs_mid"):
        kw = {"legs": nlegs, "fee_per_contract": fee}
        t = out["type"]
        for label, entry in (("at_mid", out["abs_mid"]), ("at_market", out["marketable_price"])):
            if entry is None:
                continue
            out[f"breakeven_{label}"] = teng.breakeven_close_price(entry, t, tick, **kw)
            out[f"close_target_{label}"] = teng.close_target_price(
                entry, t, out["min_close_target_pct"], tick, **kw)
    return out


def _underlying_from_legs(legs: list[dict]) -> str | None:
    """Recover the root underlying (NDX, SPX…) from a leg's OCC symbol."""
    for l in legs:
        s = l.get("symbol") or ""
        m = re.match(r"^([A-Z]+?)\d{6}[CP]\d{8}$", s)
        if m:
            root = m.group(1)
            # strip the weekly/PM suffix variants back to the base underlying
            for base in tcfg.TICKERS:
                if root == base or root.startswith(base):
                    return base
            return root
    return None


# ── place (entry + confirm + auto-close) ───────────────────────────────────
class PlaceRequest(BaseModel):
    symbol: str
    strategy: str
    legs: list[dict]
    order_type: str                       # "market" | "limit"
    limit_price: float | None = None
    duration: str = "day"
    quantity: int = Field(default=1, ge=1, le=50)
    auto_close: bool = False
    close_target_pct: float = Field(default=tcfg.DEFAULT_CLOSE_TARGET_PCT, ge=1, le=500)
    # None → use the ticker's configured default (30% globally, DJX 50%).
    # 0 → explicitly disable. ge=0 (not 1) so 0 actually passes validation instead
    # of being rejected before it ever reaches the "0 is falsy -> no stop" check.
    stop_loss_pct: float | None = Field(default=None, ge=0, le=99)
    spread_type: str | None = None        # "debit"|"credit" (net direction of the entry)
    confirm: bool = False
    dry_run: bool = False                 # preview-only: validate entry+close, place nothing
    account_id: str | None = None
    # Entry tag prefix — default "torque" for every normal UI-placed order.
    # Internal callers (e.g. the news-signal webhook) override this so their
    # orders are visibly distinguishable in the orders/history table and can
    # be detected client-side (for the sound alert) without special-casing
    # the execution path itself.
    tag_prefix: str = "torque"


def _single_option_payload(*, symbol, leg, otype, price, duration, preview, tag, side_override=None):
    p = {
        "class": "option", "symbol": symbol.upper(),
        "option_symbol": leg["symbol"],
        "side": side_override or leg["action"],
        "quantity": str(int(leg["quantity"])),
        "type": "market" if otype == "market" else "limit",
        "duration": duration,
    }
    if otype != "market":
        p["price"] = f"{float(price):.2f}"
    if preview:
        p["preview"] = "true"
    if tag:
        clean = re.sub(r"[^a-zA-Z0-9]", "", tag)[:30]
        if clean:
            p["tag"] = clean
    return p


def _multileg_close_type(entry_type: str) -> str:
    # closing a debit spread is a credit (you sell to close); vice-versa
    return "credit" if entry_type == "debit" else "debit"


def _is_rejected(order: dict) -> tuple[bool, str]:
    if not order:
        return True, "empty Tradier response"
    if order.get("errors"):
        errs = order["errors"].get("error") if isinstance(order["errors"], dict) else order["errors"]
        return True, f"errors: {errs}"
    st = str(order.get("status") or "").lower()
    if st == "rejected" or order.get("result") is False:
        return True, order.get("reason_description") or "rejected"
    return False, ""


async def _poll_fill(client, account_id, order_id, timeout=None, interval=None) -> dict:
    """Poll until the order reaches a terminal state or timeout. Returns the
    last order dict seen."""
    timeout = _POLL_TIMEOUT if timeout is None else timeout
    interval = _POLL_INTERVAL if interval is None else interval
    terminal = {"filled", "rejected", "canceled", "expired", "error"}
    last: dict = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            last = await client.get_order(account_id, order_id)
        except Exception as e:
            log.warning("poll get_order failed: %s", e)
            await asyncio.sleep(interval)
            continue
        st = str(last.get("status") or "").lower()
        if st in terminal:
            return last
        await asyncio.sleep(interval)
    return last


async def _submit(client, account_id, payload) -> dict:
    resp = await client.place_order(account_id, payload)
    return resp.get("order") or resp


async def _place_webull(req: PlaceRequest, client, account_id: str, legs: list[dict]) -> dict:
    """Entry-only placement through a per-user Webull connection.

    Torque's auto-close is app-managed: place → poll until FILLED → place the
    close. WebullClient has no order-status surface (no get_order), so we cannot
    confirm the entry filled. Placing the entry anyway and *hoping* would either
    (a) never place the close, or (b) place a naked close against an unfilled
    entry. Both are worse than refusing, so auto_close is rejected up front —
    the user can still place the entry and close manually.

    Webull has no OTO/OCO bracket in this SDK surface either, so the single-leg
    fast path doesn't apply.
    """
    from ..webull_client import WebullError
    from ..webull_order_builder import build_webull_option_order

    if req.auto_close:
        raise HTTPException(501, (
            "auto-close is not available for Webull connections yet — it needs order-status "
            "polling, which the Webull client does not expose. Place with auto_close=false "
            "and close manually, or connect Tradier."))

    expiration = next((e for e in (_expiration_from_occ(l.get("symbol")) for l in legs) if e), None)
    if not expiration:
        raise HTTPException(400, "could not recover the expiration from the leg symbols")

    sdef = tcfg.STRATEGY_DEFS.get(req.strategy, {})
    entry_type = (sdef.get("type") or req.spread_type or "debit").lower()
    order_type = "market" if req.order_type.lower() == "market" else entry_type

    try:
        orders = build_webull_option_order(
            strategy=req.strategy,
            legs=teng.legs_to_order_legs(legs, req.quantity),
            expiration=expiration,
            underlying=req.symbol.upper(),
            quantity=int(req.quantity),
            order_type=order_type,
            limit_price=req.limit_price,
        )
    except ValueError as e:
        raise HTTPException(400, f"could not build Webull order: {e}")

    try:
        # Always preview first — the option_strategy enum mapping is best-effort,
        # so Webull validates the payload before anything goes live.
        preview = await client.preview_option(account_id, orders)
        if req.dry_run:
            return {"mode": "dry_run", "broker": "webull", "account_id": account_id,
                    "entry_ok": True, "entry_preview": preview, "payload": orders,
                    "expiration": expiration, "auto_close": False}
        placed = await client.place_option(account_id, orders)
    except WebullError as e:
        raise HTTPException(502, str(e))

    return {"mode": "entry_only", "broker": "webull", "account_id": account_id,
            "entry": placed, "entry_preview": preview, "payload": orders,
            "expiration": expiration, "rejected": False, "reason": "",
            "auto_close": False, "close": None, "close_placed": False}


@router.post("/torque/place", dependencies=_GATE)
async def torque_place(req: PlaceRequest, request: Request,
                       user: dict = Depends(get_current_user)):
    if not req.confirm and not req.dry_run:
        raise HTTPException(400, "place requires confirm=true (or dry_run=true to preview only)")
    legs = req.legs
    if not legs or any(not l.get("symbol") for l in legs):
        raise HTTPException(400, "every leg needs a resolved symbol (call /torque/build first)")
    is_single = len(legs) == 1
    otype = req.order_type.lower()
    if otype not in ("market", "limit"):
        raise HTTPException(400, "order_type must be 'market' or 'limit'")
    if otype == "limit" and req.limit_price is None:
        raise HTTPException(400, "limit order needs limit_price")
    if otype == "market" and not tcfg.market_orders_allowed(req.symbol):
        raise HTTPException(400, (
            f"market orders are disabled for {req.symbol.upper()} — its book is too wide "
            f"(the ask can be ~2x the mid). Use a limit order so you see the fill price."))

    # Auto-close target floor. A target below the package's OWN bid/ask width
    # prices the close order inside the spread, so it can never fill and the
    # position sits open to expiry. The floor is therefore the greater of the
    # per-ticker static minimum and the LIVE package spread — static alone is not
    # enough (DJX 0DTE decays to net_mid 0.02 with a 0.16 spread = 800%).
    # Best-effort: a chain hiccup must not block an order, so fall back to static.
    # Auto-close is MANDATORY on some tickers (DJX): the exit may be re-priced but
    # never disarmed. Reject rather than silently coercing — auto_close=true places
    # a second real order, and no caller should get one they didn't ask for.
    if tcfg.close_target_required(req.symbol) and not req.auto_close:
        raise HTTPException(400, (
            f"auto-close is mandatory for {req.symbol.upper()} — its spread is too wide to "
            f"leave a position without a resting exit. Raise close_target_pct if you want a "
            f"wider target, but it cannot be disabled."))

    close_floor = tcfg.close_target_min(req.symbol)
    live_spread_pct = None
    spread_warning = None
    if tcfg.close_target_spread_scaled(req.symbol):
        try:
            _, _, _contracts = await _get_chain(_client(request), req.symbol)
            _px = teng.price_structure(legs, {c["symbol"]: c for c in _contracts})
            live_spread_pct = teng.package_spread_pct(_px)
        except Exception:
            live_spread_pct = None
        if live_spread_pct is not None and live_spread_pct > tcfg.MAX_AUTO_CLOSE_SPREAD_PCT:
            if req.auto_close:
                # Can't arm an exit that provably cannot fill. Where auto-close is
                # mandatory that also means the ENTRY is refused: no exit, no trade.
                tail = ("Widen the spread or pick a further expiration."
                        if tcfg.close_target_required(req.symbol)
                        else "Widen the spread, pick a further expiration, or place without auto_close.")
                raise HTTPException(400, (
                    f"{req.symbol.upper()} {req.strategy}: package bid/ask is "
                    f"{live_spread_pct:.0f}% of its mid — no profit target can fill. " + tail))
            # Manual close: the trade is the caller's call — WARN loudly, allow.
            spread_warning = (
                f"package bid/ask is {live_spread_pct:.0f}% of its mid — you pay roughly "
                f"half of that crossing in, and again crossing out. No auto-close is armed.")
        if live_spread_pct is not None and req.auto_close:
            close_floor = max(close_floor, live_spread_pct)
    close_floor = min(close_floor, 500.0)          # PlaceRequest caps at 500
    close_target_clamped = req.close_target_pct < close_floor
    if close_target_clamped:
        req.close_target_pct = round(close_floor, 2)

    # Credit CEILING. A credit buy-to-close at entry*(1-pct) goes non-positive at
    # pct≥~100 — the broker rejects a ≤0 limit and the auto-close silently unarms.
    # Cap pct so the buy-back stays a fillable, positive price, using the entry
    # credit (limit_price) as the fill proxy; the watcher re-derives from the real
    # fill and close_target_price() floors at one tick as a final backstop.
    _sdef = tcfg.STRATEGY_DEFS.get(req.strategy, {})
    _entry_type = (_sdef.get("type") or req.spread_type or "debit").lower()
    if req.auto_close and _entry_type == "credit" and req.limit_price:
        _tick = float(tcfg.ticker_rule(req.symbol, req.strategy).get("tick", 0.05))
        ceil = teng.max_credit_pct(abs(float(req.limit_price)), _tick, **_fee_kw(req.symbol, legs))
        if req.close_target_pct > ceil:
            req.close_target_pct = ceil
            close_target_clamped = True

    # Stop-loss: explicit value wins, else the ticker default (DJX 50%). The stop
    # needs the background watcher, so a stop alone is enough to arm one.
    stop_pct = req.stop_loss_pct if req.stop_loss_pct is not None else tcfg.stop_loss_default(req.symbol)

    # Resolve the broker AFTER input validation so a malformed request never
    # opens a per-user client. Entitled users trade their OWN connection; the
    # admin/dev path uses the house account.
    broker, client, account_id, per_user = await _resolve_torque_broker(request, user, req.account_id)
    handed_off = False    # set True once the background watcher owns the client

    if broker == "webull":
        try:
            return await _place_webull(req, client, account_id, legs)
        finally:
            if per_user:
                await client.close()

    try:
        tick = float(tcfg.ticker_rule(req.symbol, req.strategy).get("tick", 0.05))
        # Authoritative: a bull_call is a debit and a long fly is a debit
        # regardless of what a wide live bid/ask momentarily nets to — use the
        # declared type so the auto-close is built in the right direction.
        sdef = tcfg.STRATEGY_DEFS.get(req.strategy, {})
        entry_type = (sdef.get("type") or req.spread_type or "debit").lower()
        tag = f"{req.tag_prefix}{req.strategy}"

        # qty-scaled order legs
        o_legs = teng.legs_to_order_legs(legs, req.quantity)

        def _entry_payload(preview: bool) -> dict:
            if is_single:
                return _single_option_payload(
                    symbol=req.symbol,
                    leg={"symbol": legs[0]["symbol"], "action": legs[0]["action"],
                         "quantity": int(legs[0]["quantity"]) * req.quantity},
                    otype=otype, price=req.limit_price, duration=req.duration,
                    preview=preview, tag=tag)
            ml_type = "market" if otype == "market" else entry_type
            return build_tradier_order_payload(
                account_id=account_id, symbol=req.symbol, legs=o_legs,
                order_type=ml_type, limit_price=req.limit_price, duration=req.duration,
                preview=preview, tag=tag)

        def _close_payload(close_px: float, preview: bool) -> dict:
            if is_single:
                cs = "sell_to_close" if legs[0]["action"].startswith("buy") else "buy_to_close"
                return _single_option_payload(
                    symbol=req.symbol,
                    leg={"symbol": legs[0]["symbol"], "action": cs,
                         "quantity": int(legs[0]["quantity"]) * req.quantity},
                    otype="limit", price=close_px, duration="gtc", preview=preview,
                    tag=f"torqueClose{req.strategy}", side_override=cs)
            c_legs = teng.legs_to_order_legs(teng.build_close_legs(legs), req.quantity)
            return build_tradier_order_payload(
                account_id=account_id, symbol=req.symbol, legs=c_legs,
                order_type=_multileg_close_type(entry_type), limit_price=close_px,
                duration="gtc", preview=preview, tag=f"torqueClose{req.strategy}")

        # ── dry run: preview BOTH the entry and the close, execute nothing ─────
        if req.dry_run:
            entry_prev = await _submit(client, account_id, _entry_payload(True))
            e_rej, e_why = _is_rejected(entry_prev)
            proxy_fill = float(req.limit_price) if req.limit_price else 1.0
            close_px = teng.close_target_price(proxy_fill, entry_type, req.close_target_pct, tick,
                                                **_fee_kw(req.symbol, legs))
            close_prev = await _submit(client, account_id, _close_payload(close_px, True))
            c_rej, c_why = _is_rejected(close_prev)
            # A close preview can't fully validate without the position open yet —
            # Tradier replies "cannot be placed unless closing a long/short position".
            # That confirms the close is a recognized CLOSING order (right format),
            # so treat it as OK-with-caveat rather than a failure.
            unverifiable = c_rej and ("closing a long position" in c_why.lower()
                                      or "closing a short position" in c_why.lower())
            return {
                "mode": "dry_run", "account_id": account_id,
                "entry_ok": not e_rej, "entry_reason": e_why, "entry_preview": entry_prev,
                "close_ok": (not c_rej) or unverifiable,
                "close_format_valid": (not c_rej) or unverifiable,
                "close_needs_position": unverifiable,
                "close_reason": c_why, "close_preview": close_prev,
                "close_target_price": close_px,
                "close_target_pct": req.close_target_pct,
                "close_target_clamped": close_target_clamped,
                "package_spread_pct": live_spread_pct,
                "spread_warning": spread_warning,
            }

        # ── single-leg + limit + auto_close → native broker-held bracket ───────
        # No stop requested → plain 2-leg OTO (entry + profit), unchanged.
        # Stop ALSO requested → try a native 3-leg OTOCO (entry + profit + stop)
        # instead of the app-managed watcher: BOTH exits then live on Tradier's
        # own book, and Tradier's own OCO logic cancels whichever exit doesn't
        # fire — no app-side coordination code needed. Verified against a live
        # Tradier sandbox preview (2026-09-18): plain "oto" flatly rejects a
        # second leg priced below entry ("must be higher than price[0]") — it
        # only understands entry+profit, never a stop — so the stop leg has to
        # ride "otoco" with type=stop_limit specifically, and Tradier enforces a
        # $0.10 minimum gap between the two exit prices. Below that floor (cheap
        # 0DTE premium can land a 30/30 band closer than $0.10 apart) we don't
        # submit a bracket we already know gets rejected — fall through to the
        # app-managed watcher for both exits instead, same as multi-leg spreads.
        if is_single and req.auto_close and otype == "limit":
            close_px = teng.close_target_price(float(req.limit_price), entry_type, req.close_target_pct, tick,
                                                **_fee_kw(req.symbol, legs))
            close_side = "sell_to_close" if legs[0]["action"].startswith("buy") else "buy_to_close"
            qty0 = str(int(legs[0]["quantity"]) * req.quantity)
            leg0 = {
                "symbol[0]": req.symbol.upper(), "option_symbol[0]": legs[0]["symbol"],
                "side[0]": legs[0]["action"], "quantity[0]": qty0,
                "type[0]": "limit", "price[0]": f"{float(req.limit_price):.2f}",
            }
            leg1_profit = {
                "symbol[1]": req.symbol.upper(), "option_symbol[1]": legs[0]["symbol"],
                "side[1]": close_side, "quantity[1]": qty0,
                "type[1]": "limit", "price[1]": f"{close_px:.2f}",
            }
            if not stop_pct:
                oto = {"class": "oto", "duration": req.duration, **leg0, **leg1_profit,
                       "tag": re.sub(r"[^a-zA-Z0-9]", "", tag)[:30]}
                entry = await _submit(client, account_id, oto)
                rej, why = _is_rejected(entry)
                return {
                    "mode": "oto_bracket", "rejected": rej, "reason": why,
                    "entry": entry, "close_target_price": close_px,
                    "account_id": account_id, "auto_close": True,
                }
            stop_trigger, stop_limit = teng.stop_limit_leg_prices(
                float(req.limit_price), entry_type, float(stop_pct), tick, tcfg.STOP_LIMIT_BUFFER_TICKS)
            if abs(close_px - stop_trigger) >= tcfg.OCO_MIN_PRICE_GAP:
                leg2_stop = {
                    "symbol[2]": req.symbol.upper(), "option_symbol[2]": legs[0]["symbol"],
                    "side[2]": close_side, "quantity[2]": qty0,
                    "type[2]": "stop_limit", "stop[2]": f"{stop_trigger:.2f}", "price[2]": f"{stop_limit:.2f}",
                }
                otoco = {"class": "otoco", "duration": req.duration, **leg0, **leg1_profit, **leg2_stop,
                         "tag": re.sub(r"[^a-zA-Z0-9]", "", tag)[:30]}
                entry = await _submit(client, account_id, otoco)
                rej, why = _is_rejected(entry)
                return {
                    "mode": "otoco_bracket", "rejected": rej, "reason": why,
                    "entry": entry, "close_target_price": close_px,
                    "stop_loss_price": stop_trigger, "stop_loss_pct": float(stop_pct),
                    "account_id": account_id, "auto_close": True,
                }
            # gap too narrow for Tradier's OCO rule — fall through to the
            # app-managed watcher below, which owns both exits reactively.

        # ── entry payload (single option or multileg spread) ──────────────────
        if is_single:
            entry_payload = _single_option_payload(
                symbol=req.symbol, leg={"symbol": legs[0]["symbol"], "action": legs[0]["action"],
                                        "quantity": int(legs[0]["quantity"]) * req.quantity},
                otype=otype, price=req.limit_price, duration=req.duration, preview=False, tag=tag)
        else:
            ml_type = "market" if otype == "market" else entry_type
            entry_payload = build_tradier_order_payload(
                account_id=account_id, symbol=req.symbol, legs=o_legs,
                order_type=ml_type, limit_price=req.limit_price, duration=req.duration,
                preview=False, tag=tag)

        entry = await _submit(client, account_id, entry_payload)
        rej, why = _is_rejected(entry)
        order_id = entry.get("id")
        result = {
            "mode": "confirm_then_close", "entry": entry, "order_id": order_id,
            "rejected": rej, "reason": why, "account_id": account_id,
            "auto_close": req.auto_close, "close": None, "close_placed": False,
            "close_target_pct": req.close_target_pct,
            "close_target_clamped": close_target_clamped,
            "package_spread_pct": live_spread_pct,
            "spread_warning": spread_warning,
            "stop_loss_pct": stop_pct,
        }
        if rej or not order_id:
            return result
        if not req.auto_close and not stop_pct:
            return result

        # ── auto-close: watch for the fill in the BACKGROUND, then place the close.
        # We do NOT block the request — a limit entry can fill minutes later, and the
        # old synchronous 30s poll meant a slow fill silently got no close. The
        # watcher polls until the entry fills (or the day ends) and places the close
        # then, retrying if the position isn't settled yet. Progress shows in the
        # orders panel (/torque/orders). For a per-user broker, the watcher takes
        # ownership of the client (it needs it to poll for hours) and closes it.
        est_fill = float(req.limit_price) if req.limit_price else 0.0
        # Only meaningful when a profit-target close will actually be placed. A
        # stop-only watcher (auto_close=false) must not advertise a close target.
        est_close = (teng.close_target_price(est_fill, entry_type, req.close_target_pct, tick,
                                            **_fee_kw(req.symbol, legs))
                     if (est_fill and req.auto_close) else None)
        w = {
            "entry_order_id": str(order_id), "symbol": req.symbol.upper(),
            "strategy": req.strategy, "state": "watching_fill", "entry_status": "open",
            "close_order_id": None, "close_target_price": est_close,
            "close_reason": None, "done": False,
            "stop_loss_pct": stop_pct, "stop_order_id": None, "stop_exit_price": None,
            # Caller identity. _WATCHERS is a process-global store shared across
            # every user hitting this backend, so tag each watcher with the uid
            # that placed it; /torque/orders returns only the caller's own (never
            # sent to the client). dev/admin/supabase all carry a stable "id".
            "uid": user.get("id"),
        }
        _WATCHERS[str(order_id)] = w
        w["task"] = asyncio.create_task(_watch_and_close(
            client, account_id, str(order_id), w,
            is_single=is_single, legs=legs, symbol=req.symbol, strategy=req.strategy,
            entry_type=entry_type, pct=req.close_target_pct, tick=tick,
            quantity=req.quantity, close_client=per_user,
            floor_pct=close_floor, stop_pct=stop_pct, auto_close=req.auto_close))
        handed_off = True
        result["mode"] = "watching_fill"
        result["watch_id"] = str(order_id)
        result["close_target_price"] = est_close
        result["close_note"] = "auto-close will be placed when the entry fills (watching in background)"
        return result
    finally:
        # Close the per-user client unless the watcher took ownership of it.
        if per_user and not handed_off:
            await client.close()


# ── news-signal ingestion (facades-news-reactor) ────────────────────────────
class NewsSignalPayload(BaseModel):
    """See ../../docs/torque.md 'News-signal ingestion' and the sibling repo's
    docs/torque-integration-proposal.md — this mirrors that payload exactly."""
    source_event_id: str
    headline: str
    category: str | None = None
    symbol: str
    direction: str                          # "bullish" | "bearish"
    sentiment: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str | None = None
    tradier_confirmation: dict | None = None
    generated_at: str | None = None


_NEWS_DIRECTION_STRATEGY = {"bullish": "long_call", "bearish": "long_put"}
_NEWS_SIGNAL_TS_WINDOW_SEC = 300   # +/-5min — bounds how long a captured request stays replayable
_NEWS_SIGNAL_RATE: dict = {"window_start": 0.0, "count": 0}


async def require_news_signal_auth(request: Request) -> None:
    """HMAC-signed webhook auth for POST /webhook/news_signal — this endpoint
    places real trades, so it authenticates the CALLER, not just what the
    payload is allowed to do (every other check on this route constrains
    WHAT gets traded; this is the only one that checks WHO is asking).
    Fails CLOSED (401) whenever NEWS_REACTOR_WEBHOOK_SECRET is unset —
    "unconfigured" must mean "closed," never "open to anyone who can reach
    this host."

    Signature: hex HMAC-SHA256 of "{X-Signal-Timestamp}.{raw body}" using the
    shared secret, in the `X-Signal-Signature` header. The timestamp must be
    within _NEWS_SIGNAL_TS_WINDOW_SEC of now, so a captured request can't be
    replayed indefinitely — source_event_id idempotency (claim_news_signal,
    below) is the second, independent layer against replay, covering both
    the window itself and the sender's own legitimate retries."""
    secret = (get_settings().news_reactor_webhook_secret or "").strip()
    if not secret:
        raise HTTPException(401, "news-signal webhook not configured")
    ts_header = request.headers.get("X-Signal-Timestamp", "")
    sig_header = request.headers.get("X-Signal-Signature", "")
    try:
        ts = int(ts_header)
    except ValueError:
        raise HTTPException(401, "missing or invalid X-Signal-Timestamp")
    if abs(time.time() - ts) > _NEWS_SIGNAL_TS_WINDOW_SEC:
        raise HTTPException(401, "stale timestamp")
    raw = await request.body()
    expected = hmac.new(secret.encode(), f"{ts_header}.".encode() + raw, hashlib.sha256).hexdigest()
    if not sig_header or not hmac.compare_digest(sig_header, expected):
        raise HTTPException(401, "invalid signature")


def _news_signal_rate_ok(settings) -> bool:
    """Fixed-window backstop cap on signal-driven orders, independent of
    auth/entitlement/idempotency — a compromised secret or a malfunctioning
    sender still can't place unbounded orders."""
    now = time.time()
    if now - _NEWS_SIGNAL_RATE["window_start"] >= settings.news_signal_rate_window_sec:
        _NEWS_SIGNAL_RATE.update(window_start=now, count=0)
    if _NEWS_SIGNAL_RATE["count"] >= settings.news_signal_max_per_window:
        return False
    _NEWS_SIGNAL_RATE["count"] += 1
    return True


async def _place_news_signal_for_uid(uid, *, request, symbol, strategy, legs,
                                     limit_price, stop_pct, quantity) -> dict:
    """One qualified account's attempt: entitlement + broker checks, then a
    fresh PlaceRequest through the real place() path. Never raises — every
    outcome (not entitled, no broker, rejected) comes back as a dict so one
    account's failure can't stop the fan-out to the rest."""
    user = {"id": uid, "auth": "supabase"}
    # Direct function calls bypass the HTTP dependency layer entirely (no
    # FastAPI Depends() runs for a plain call), so both entitlement checks
    # normally gated via `_GATE`/`require_torque_access` are done explicitly
    # here — non-raising, since "not entitled" must be a silent drop for a
    # webhook, never a 403. A user needs "torque" (can use Torque at all) AND
    # "news-reactor" (opted into this specific signal source).
    tools = await supabase_admin.get_user_tools(uid)
    if "torque" not in tools:
        return {"uid": uid, "accepted": False, "reason": "not entitled to torque"}
    if "news-reactor" not in tools:
        return {"uid": uid, "accepted": False, "reason": "not entitled to news-reactor"}

    place_req = PlaceRequest(
        symbol=symbol, strategy=strategy, legs=legs, order_type="limit",
        limit_price=limit_price, quantity=quantity,
        auto_close=True, stop_loss_pct=stop_pct, confirm=True, tag_prefix="torqueNews",
    )
    try:
        result = await torque_place(place_req, request, user=user)
    except HTTPException as e:
        # Most commonly: this account has no active broker connection
        # (resolve_broker's own 403) — exactly the "not configured, so ignore
        # it" case this gate is meant to produce, not an error.
        return {"uid": uid, "accepted": False, "reason": e.detail}
    return {"uid": uid, "accepted": True, "place_result": result}


@router.post("/webhook/news_signal", dependencies=[Depends(require_news_signal_auth)])
async def news_signal(payload: NewsSignalPayload, request: Request):
    """Receive a directional news signal ONCE and fan it out, backend-side,
    to every qualified account — the sender posts a single time and forgets;
    Torque decides how many accounts act on it, not the reverse. Each
    qualifying account places it through Torque's own place() path — same
    guardrails (auto-close, stop-loss, the fully-hardened stop-exit ladder)
    as a human clicking Send, zero special-casing for "this came from a
    webhook".

    "Qualified" today means: uid listed in TORQUE_OPERATOR_UIDS (a webhook
    has no browser session to bind to, so there's no other way yet to know
    which accounts should react — see docs/torque.md), entitled to both
    `torque` and `news-reactor`, and has an active broker connection. Every
    account is checked and acted on independently; one account having no
    broker connected doesn't block another that does.

    Single-leg only (long_call/long_put) — the payload carries no strike or
    spread guidance, and single-leg is the only structure that gets the fully
    broker-native OTOCO bracket for both the profit target and the stop, the
    safest execution path for something that fires with no one watching it.
    The chain is fetched and the limit price/stop computed ONCE and shared
    across every qualified account, so they all act on the identical quote
    rather than staggered re-fetches drifting apart.

    Authentication (require_news_signal_auth, above) happens before any of
    this runs and fails CLOSED — everything below authenticates WHAT gets
    traded, not WHO is asking. ALWAYS returns 200/ok:true past that point
    regardless of outcome — the proposal is explicit that the sender must not
    need to know Torque's mode. Every non-execution path (flag off, no ticker
    match, no operator configured, not entitled, no broker connected, spread
    too wide, market closed/unknown, duplicate source_event_id, rate-capped)
    is a log-and-drop, never an error response.
    """
    settings = get_settings()
    if not settings.accept_news_reactor_signals:
        log.info("news_signal dropped (feature off): %s", payload.source_event_id)
        return {"ok": True, "accepted": False, "reason": "ACCEPT_NEWS_REACTOR_SIGNALS is off"}

    # Idempotency: a replay of an already-authenticated request (within the
    # signature's timestamp window) or the sender's own legitimate retry must
    # never place a second order. Checked before anything else so a replay
    # never even reaches the rate cap or does any real work.
    db = getattr(request.app.state, "db", None)
    if db is None:
        return {"ok": True, "accepted": False, "reason": "idempotency store unavailable"}
    if not db.claim_news_signal(payload.source_event_id):
        log.info("news_signal duplicate dropped: %s", payload.source_event_id)
        return {"ok": True, "accepted": False, "reason": "duplicate source_event_id"}

    if not _news_signal_rate_ok(settings):
        return {"ok": True, "accepted": False, "reason": "signal rate cap exceeded"}

    strategy = _NEWS_DIRECTION_STRATEGY.get((payload.direction or "").lower())
    if not strategy:
        return {"ok": True, "accepted": False, "reason": f"unrecognized direction {payload.direction!r}"}

    symbol = payload.symbol.upper()
    if symbol not in tcfg.tickers():
        return {"ok": True, "accepted": False, "reason": f"{symbol} is not a configured Torque ticker"}

    # Defensive belt-and-suspenders — facades-news-reactor already enforces
    # trading_hours on the sending side. Fails CLOSED here: an unattended
    # trader should never act on an uncertain market-open read, so anything
    # other than a confirmed `open is True` (including a calendar-fetch
    # failure) drops the signal rather than trading blind.
    try:
        clock = await torque_clock(request)
    except Exception:
        clock = {"open": None}
    if clock.get("open") is not True:
        return {"ok": True, "accepted": False, "reason": "market is closed or its state is unknown"}

    uids = tcfg.operator_uids()
    if not uids:
        return {"ok": True, "accepted": False, "reason": "no TORQUE_OPERATOR_UIDS configured"}

    try:
        build = await torque_build(BuildRequest(symbol=symbol, strategy=strategy), request)
    except HTTPException as e:
        return {"ok": True, "accepted": False, "reason": f"build failed: {e.detail}"}
    if build.get("missing_legs"):
        return {"ok": True, "accepted": False, "reason": f"could not resolve contract(s): {build['missing_legs']}"}

    price = build["price"]
    # Fetched fresh right here, at execution time — this payload deliberately
    # carries no price, so there is no snapshot to fall back to (by design,
    # per the proposal). Reject rather than trade against a garbage book.
    spread_pct = teng.package_spread_pct(price)
    if spread_pct is not None and spread_pct > tcfg.MAX_AUTO_CLOSE_SPREAD_PCT:
        return {"ok": True, "accepted": False,
                "reason": f"package spread too wide to trade blind ({spread_pct:.0f}% of mid)"}
    limit_price = teng.suggested_limit(price, build["tick"])
    if not limit_price:
        return {"ok": True, "accepted": False, "reason": "no live quote available at execution time"}

    stop_pct = tcfg.stop_loss_default(symbol) or tcfg.DEFAULT_STOP_LOSS_PCT
    results = [
        await _place_news_signal_for_uid(
            uid, request=request, symbol=symbol, strategy=strategy, legs=build["legs"],
            limit_price=limit_price, stop_pct=stop_pct, quantity=settings.news_signal_quantity)
        for uid in uids
    ]
    placed = [r for r in results if r["accepted"]]
    log.info("news_signal %s %s %s @ %.2f: %d/%d accounts placed",
             payload.source_event_id, symbol, strategy, limit_price, len(placed), len(results))
    return {"ok": True, "accepted": len(placed) > 0, "symbol": symbol, "strategy": strategy,
            "limit_price": limit_price, "stop_loss_pct": stop_pct, "results": results}


def _build_close_payload(*, account_id, symbol, strategy, legs, is_single,
                         entry_type, close_px, quantity, duration="gtc",
                         tag_prefix="torqueClose", market=False):
    """Closing order payload (single option or multileg), limit or market.

    `tag_prefix` distinguishes the passive profit-target close (torqueClose…)
    from a stop exit (torqueStop…) so the orders panel and the modify guard can
    tell them apart. A stop uses duration=day and a marketable price.

    `market=True` gives up price control entirely (no `close_px` needed) — the
    escalation path once a stop-limit has sat unfilled too long: a limit can
    only ever guarantee price, never a fill, on a fast enough move."""
    tag = f"{tag_prefix}{strategy}"
    if is_single:
        cs = "sell_to_close" if legs[0]["action"].startswith("buy") else "buy_to_close"
        return _single_option_payload(
            symbol=symbol, leg={"symbol": legs[0]["symbol"], "action": cs,
                                "quantity": int(legs[0]["quantity"]) * quantity},
            otype=("market" if market else "limit"), price=close_px, duration=duration,
            preview=False, tag=tag, side_override=cs)
    c_legs = teng.legs_to_order_legs(teng.build_close_legs(legs), quantity)
    return build_tradier_order_payload(
        account_id=account_id, symbol=symbol, legs=c_legs,
        order_type=("market" if market else _multileg_close_type(entry_type)),
        limit_price=(None if market else close_px),
        duration=duration, preview=False, tag=tag)


async def _package_price(client, symbol: str, legs: list[dict]) -> dict | None:
    """Live net bid/mid/ask for the open position's legs, or None."""
    try:
        _, _, contracts = await _get_chain(client, symbol)
    except Exception:
        return None
    px = teng.price_structure(legs, {c["symbol"]: c for c in contracts})
    return px if px.get("complete") else None


async def _submit_stop_exit(client, account_id, *, symbol, strategy, legs, is_single,
                            entry_type, quantity, close_px=None, market=False,
                            tag_prefix="torqueStop"):
    """Build + submit a stop-exit close (resting limit, or escalated market).
    Returns (order_id, rejected, reason). `tag_prefix` defaults to the normal
    stop tag; the final market fallback uses a distinct one so it stands out
    in the orders/history table as what it is — a guaranteed exit that gave
    up price control, not a routine bounded fill."""
    payload = _build_close_payload(
        account_id=account_id, symbol=symbol, strategy=strategy, legs=legs,
        is_single=is_single, entry_type=entry_type, close_px=close_px,
        quantity=quantity, duration="day", tag_prefix=tag_prefix, market=market)
    res = await _submit(client, account_id, payload)
    rej, why = _is_rejected(res)
    return str(res.get("id") or ""), rej, why


async def _resolve_stalled_stop(client, account_id, order_id, quantity):
    """Ground truth for a stop-exit order after its grace window expires —
    called before the ladder is allowed to touch it again.

    A timed-out poll does NOT mean the order didn't fill (or partially fill)
    in between checks — polls are STOP_INTERVAL apart, and a fast move is
    exactly when both a stop trigger and a fill are likely to land in that
    window. Trusting `cancel_order`'s own success/failure is not enough
    either: a cancel commonly throws BECAUSE the order just filled, and
    treating that as "safe to resubmit for the full quantity" sends a second
    closing order on top of a position that's already gone — which fills into
    a fresh NAKED position the moment there's nothing left to close against.
    So the cancel is best-effort; the decision is always made from a fresh
    `get_order` read afterward, never from whether the cancel itself worked.

    Returns one of:
      ("filled", 0)                — fully filled; nothing left to protect
      ("resubmit", remaining_qty)  — safe to replace, for exactly this many
      ("needs_attention", 0)       — final state unknown/unsafe; do NOT resubmit
    """
    try:
        await client.cancel_order(account_id, order_id)
    except Exception as e:
        log.warning("stop: cancel failed for %s (may have already filled): %s", order_id, e)
    try:
        o = await client.get_order(account_id, order_id)
    except Exception as e:
        log.warning("stop: could not confirm order %s's state after cancel: %s", order_id, e)
        return "needs_attention", 0
    st = str(o.get("status") or "").lower()
    if st == "filled":
        return "filled", 0    # trust the label alone — no dependency on exec_quantity being present
    if st in ("partially_filled", "canceled", "rejected", "expired"):
        # exec_quantity is read unconditionally, not gated on the status
        # string being literally "partially_filled" — a broker reporting the
        # post-cancel state of a partial fill as plain "canceled" while still
        # carrying a nonzero exec_quantity must not fall through to a full-
        # quantity resubmit; that is the exact over-close bug, just triggered
        # by a different status label instead of a failed cancel.
        exec_qty = teng._f(o.get("exec_quantity")) or 0.0
        remaining = quantity - exec_qty
        return ("filled", 0) if remaining <= 0 else ("resubmit", remaining)
    # Still "open"/"pending" (or some status we don't recognize) even after a
    # cancel attempt — that is not a state the ladder can safely act on.
    return "needs_attention", 0


async def _run_stop_exit_ladder(client, account_id, w, *, symbol, strategy, legs,
                                is_single, entry_type, quantity, tick, first_price):
    """Once the stop has fired: a fresh, live-quoted marketable limit first —
    real, bounded prices, what usually clears close to mid with price
    improvement on a multileg book. If it stalls, the SAME resting order is
    re-quoted in place via MODIFY (not cancelled and replaced) — cheaper, and
    it keeps one continuous order the orders panel/history can track — for up
    to STOP_MAX_REQUOTES chases. Only after all of those still haven't
    cleared does it freeze, alert, and guarantee the exit at a genuine
    unbounded market order: no limit, bounded or not, can ever GUARANTEE a
    fill, and a stop that never exits on a runaway move is worse than one
    that pays the full spread to actually get out.

    Every stall — a failed modify, a failed cancel, anything that isn't a
    clean confirmed status — is resolved through _resolve_stalled_stop before
    the next order goes out. Never a blind resubmit for the original
    quantity, which can over-close a partially-filled position into a naked
    one on the excess."""
    order_id, rej, why = await _submit_stop_exit(
        client, account_id, symbol=symbol, strategy=strategy, legs=legs,
        is_single=is_single, entry_type=entry_type, quantity=quantity,
        close_px=first_price)
    w["stop_exit_price"] = first_price
    w["stop_order_id"] = order_id
    w["stop_market_fallback"] = False
    if rej:
        w["state"] = "stop_rejected"
        w["stop_note"] = why
        return
    w["state"] = "stop_placed"
    remaining = quantity

    for _requote in range(tcfg.STOP_MAX_REQUOTES):
        for _tick in range(tcfg.STOP_ESCALATE_AFTER_TICKS):
            await asyncio.sleep(_STOP_INTERVAL)
            try:
                o = await client.get_order(account_id, order_id)
            except Exception:
                continue
            st = str(o.get("status") or "").lower()
            if st == "filled":
                w["state"] = "stop_filled"
                return
            if st in ("rejected", "canceled", "expired"):
                break   # dead order — resolve immediately rather than waiting out the tick budget

        px = await _package_price(client, symbol, legs)
        fresh_px = teng.stop_exit_price(px, entry_type, tick) if px else None
        modify_ok = False
        if fresh_px is not None:
            try:
                await client.modify_order(account_id, order_id, price=fresh_px)
                modify_ok = True
            except Exception as e:
                log.warning("stop: modify failed for %s (may have already filled): %s", order_id, e)
        if modify_ok:
            w["stop_exit_price"] = fresh_px
            w["state"] = "stop_requoted"
            continue   # same order_id, same tracked order — just a fresh price

        # Modify didn't happen (no live quote, or the modify call itself
        # failed) — get ground truth before touching this order again.
        outcome, remaining = await _resolve_stalled_stop(client, account_id, order_id, remaining)
        if outcome == "filled":
            w["state"] = "stop_filled"
            return
        if outcome == "needs_attention":
            w["state"] = "stop_needs_attention"
            w["stop_note"] = (f"order {order_id} left an unconfirmed final state while requoting — "
                              f"refusing to resubmit and risk closing more than is actually open; "
                              f"needs manual review")
            return
        # Confirmed dead (not filled) — the old order is gone, so this one
        # requote has to be a fresh submission rather than a modify.
        px = await _package_price(client, symbol, legs)
        fresh_px = teng.stop_exit_price(px, entry_type, tick) if px else None
        if fresh_px is None:
            break   # no live quote left to re-cross against — freeze and go to market
        order_id, rej, why = await _submit_stop_exit(
            client, account_id, symbol=symbol, strategy=strategy, legs=legs,
            is_single=is_single, entry_type=entry_type, quantity=remaining,
            close_px=fresh_px)
        w["stop_exit_price"] = fresh_px
        w["stop_order_id"] = order_id
        w["state"] = "stop_requoted"
        if rej:
            w["state"] = "stop_rejected"
            w["stop_note"] = why
            return

    # Exhausted every bounded re-cross attempt. Freeze: stop trying to chase
    # a moving book with a bounded price and guarantee the exit instead. The
    # same ground-truth check still applies first — the market fallback must
    # never risk closing an already-filled (or over-closed) position either —
    # and the fallback itself gets a distinct tag so it's visibly flagged in
    # the orders/history table as what it is, not a routine bounded fill.
    outcome, remaining = await _resolve_stalled_stop(client, account_id, order_id, remaining)
    if outcome == "filled":
        w["state"] = "stop_filled"
        return
    if outcome == "needs_attention":
        w["state"] = "stop_needs_attention"
        w["stop_note"] = (f"order {order_id} left an unconfirmed final state before the market "
                          f"fallback — refusing to submit and risk an over-close; needs manual review")
        return
    market_id, rej, why = await _submit_stop_exit(
        client, account_id, symbol=symbol, strategy=strategy, legs=legs,
        is_single=is_single, entry_type=entry_type, quantity=remaining, market=True,
        tag_prefix="torqueStopMkt")
    w["stop_order_id"] = market_id
    w["stop_market_fallback"] = True
    w["state"] = "stop_rejected" if rej else "stop_market_placed"
    w["stop_note"] = (why if rej else
                      f"no bounded fill after {tcfg.STOP_MAX_REQUOTES} re-quotes — exited at "
                      f"market to guarantee the stop; verify the fill price")


async def _monitor_stop(client, account_id, w, *, legs, symbol, strategy, is_single,
                        entry_type, entry_fill, stop_pct, tick, quantity, close_order_id):
    """Poll the package's fair value; cross out if it loses `stop_pct`% of entry.

    One deliberate refusal, DJX-driven: the trigger reads the MID, not the bid
    — a wide package is underwater by half its spread the moment it fills, and
    a bid-based stop would fire instantly. If the book is wider than
    STOP_MAX_EXIT_SPREAD_PCT when the stop trips, the first tick does NOT dump
    into it — but this refusal isn't permanent: staying blocked for
    STOP_ESCALATE_AFTER_TICKS ticks running hands off to the same bounded exit
    ladder everyone else gets (see _run_stop_exit_ladder), because a loss
    parked open indefinitely is worse than one bounded crossing.
    """
    deadline = time.time() + _WATCH_TIMEOUT
    blocked_ticks = 0
    while time.time() < deadline:
        await asyncio.sleep(_STOP_INTERVAL)
        if close_order_id:                       # target already hit → nothing to protect
            try:
                o = await client.get_order(account_id, close_order_id)
                if str(o.get("status") or "").lower() == "filled":
                    w["state"] = "closed_at_target"
                    return
            except Exception:
                pass
        px = await _package_price(client, symbol, legs)
        if not px:
            continue
        mark = px.get("abs_mid")
        w["mark"] = mark
        if not teng.stop_breached(mark, entry_fill, entry_type, stop_pct):
            continue

        sp = teng.package_spread_pct(px)
        if sp is not None and sp > tcfg.STOP_MAX_EXIT_SPREAD_PCT:
            blocked_ticks += 1
            if blocked_ticks < tcfg.STOP_ESCALATE_AFTER_TICKS:
                w["state"] = "stop_blocked_wide_market"
                w["stop_note"] = (f"stop hit (mark {mark}) but the package bid/ask is {sp:.0f}% of mid — "
                                  f"crossing would cost more than the stop saves; holding and re-checking")
                continue
            # stayed too wide too long — stop waiting for it to improve and
            # start the same bounded ladder as any other breach
        exit_px = teng.stop_exit_price(px, entry_type, tick)
        if exit_px is None:
            continue

        protect_qty = quantity
        if close_order_id:
            # The resting profit-target close might already be sitting
            # partially filled (naturally, or because the caller cancelled it
            # by hand right as the stop fired) — cancelling it blind and then
            # sizing the stop-exit for the ORIGINAL full quantity closes
            # units that TP already closed, over-closing into a fresh naked
            # position on the excess. Same ground-truth resolution the ladder
            # itself uses, applied here to the TP order before handing off.
            outcome, protect_qty = await _resolve_stalled_stop(client, account_id, close_order_id, quantity)
            _CLOSE_GUARD.pop(str(close_order_id), None)
            if outcome == "filled":
                # TP already fully closed the position (e.g. it filled in the
                # same window the stop just breached in) — nothing left to
                # protect, and a stop-exit here would open a naked position
                # from nothing.
                w["state"] = "closed_at_target"
                return
            if outcome == "needs_attention":
                w["state"] = "stop_needs_attention"
                w["stop_note"] = (f"could not confirm the profit-target close {close_order_id}'s "
                                  f"final state before handing off to the stop — refusing to guess "
                                  f"how much is actually still open; needs manual review")
                return

        await _run_stop_exit_ladder(
            client, account_id, w, symbol=symbol, strategy=strategy, legs=legs,
            is_single=is_single, entry_type=entry_type, quantity=protect_qty, tick=tick,
            first_price=exit_px)
        return


async def _watch_and_close(client, account_id, entry_id, w, *, is_single, legs,
                           symbol, strategy, entry_type, pct, tick, quantity,
                           close_client=False, floor_pct=1.0, stop_pct=None,
                           auto_close=True):
    """Background: poll the entry until it fills, then place the close (with
    retries if the freshly-filled position isn't settled yet). Updates `w`.

    close_client=True means this watcher owns a per-user broker client and must
    close it when done (the house client is shared and must never be closed)."""
    try:
        filled = await _poll_fill(client, account_id, entry_id,
                                  timeout=_WATCH_TIMEOUT, interval=_WATCH_INTERVAL)
        fst = str(filled.get("status") or "").lower()
        w["entry_status"] = fst or "unknown"
        if fst != "filled":
            w["state"] = "entry_not_filled"
            return
        # Tradier reports avg_fill_price as NEGATIVE for a filled multileg credit
        # order (confirmed live 2026-09-22: submitted price=6.6, avg_fill_price=
        # -6.6) even though every price this module computes is an unsigned
        # premium (direction lives in entry_type, never in the sign). Fed
        # unabsed into close_target_price()'s credit branch, a negative entry
        # drives the target negative, which trips the "floor at one tick" guard
        # meant for pct>=100% edge cases — silently returning the cheapest tick
        # (e.g. $0.05) instead of the real ~70%-of-credit buyback.
        entry_fill = abs(teng._f(filled.get("avg_fill_price")) or 0.0)
        w["entry_fill"] = entry_fill
        close_order_id = None

        if auto_close:
            close_px = teng.close_target_price(float(entry_fill), entry_type, pct, tick,
                                               **_fee_kw(symbol, legs))
            w["close_target_price"] = close_px
            payload = _build_close_payload(
                account_id=account_id, symbol=symbol, strategy=strategy, legs=legs,
                is_single=is_single, entry_type=entry_type, close_px=close_px,
                quantity=quantity)
            placed = False
            for attempt in range(_CLOSE_RETRIES):
                close = await _submit(client, account_id, payload)
                crej, cwhy = _is_rejected(close)
                if not crej:
                    placed = True
                    close_order_id = str(close.get("id") or "")
                    w["close_order_id"] = close_order_id
                    # Arm the modify guard: editing this order in the orders panel
                    # must re-apply the same floor /torque/place enforced.
                    if close_order_id:
                        _CLOSE_GUARD[close_order_id] = {
                            "symbol": symbol.upper(), "entry_type": entry_type,
                            "entry_fill": float(entry_fill), "tick": tick,
                            "floor_pct": float(floor_pct), **_fee_kw(symbol, legs),
                        }
                    break
                w["close_reason"] = cwhy
                # position may not be settled the instant the entry fills — retry
                if "position" in (cwhy or "").lower() and attempt < _CLOSE_RETRIES - 1:
                    await asyncio.sleep(_CLOSE_RETRY_DELAY)
                    continue
                break
            w["state"] = "close_placed" if placed else "close_rejected"
            if not placed and not stop_pct:
                return

        if stop_pct:
            w["stop_loss_pct"] = float(stop_pct)
            await _monitor_stop(
                client, account_id, w, legs=legs, symbol=symbol, strategy=strategy,
                is_single=is_single, entry_type=entry_type, entry_fill=entry_fill,
                stop_pct=float(stop_pct), tick=tick, quantity=quantity,
                close_order_id=close_order_id)
    except Exception as e:                       # never let the task die silently
        log.warning("auto-close watcher failed for %s: %s", entry_id, e)
        w["state"] = "error"
        w["close_reason"] = str(e)
    finally:
        w["done"] = True
        if close_client:
            try:
                await client.close()
            except Exception:
                pass


_WORKING_STATES = {"open", "pending", "partially_filled", "calculated", "accepted", "queued", "received"}


@router.get("/torque/orders", dependencies=_GATE)
async def torque_orders(request: Request, account_id: str | None = None,
                        user: dict = Depends(get_current_user)):
    """Feeds the bottom orders panel from the caller's own broker account:
    `orders` = live/working only, `watchers` = active auto-close watchers, and
    `history` = every Torque-tagged order today at any status (the account-wide
    Past Orders list, identical from any tab)."""
    broker, client, aid, per_user = await _resolve_torque_broker(request, user, account_id)
    await _require_tradier(broker, client, per_user, "the orders panel")
    try:
        try:
            raw = await client.get_orders(aid)
        except Exception as e:
            raise HTTPException(502, f"orders fetch failed: {e}")
        orders = []
        history = []
        for o in raw:
            st = str(o.get("status") or "").lower()
            tag = str(o.get("tag") or "")
            row = {
                "id": str(o.get("id") or ""), "symbol": o.get("symbol"),
                "class": o.get("class"), "type": o.get("type"), "side": o.get("side"),
                "status": st,
                "quantity": teng._f(o.get("quantity")), "exec_quantity": teng._f(o.get("exec_quantity")),
                "price": teng._f(o.get("price")), "avg_fill_price": teng._f(o.get("avg_fill_price")),
                "duration": o.get("duration"), "tag": tag,
                "create_date": o.get("create_date"),
            }
            if st in _WORKING_STATES:
                orders.append({**row, "working": True})
            # Past Orders: EVERY Torque-tagged order on this account today, any status
            # (pending → filled/canceled/rejected/expired). Account-scoped, so it's the
            # same list from every tab, and the status is the broker's own — no per-tab
            # tracking to drift. Torque tags all its orders "torque…" / "torqueClose…".
            if tag.lower().startswith("torque"):
                history.append(row)
        orders.sort(key=lambda x: x.get("create_date") or "", reverse=True)
        history.sort(key=lambda x: x.get("create_date") or "", reverse=True)
        history = history[:100]
        # Show watchers that are still PENDING a close (entry not yet confirmed
        # closed) AND belong to THIS caller — _WATCHERS is process-global and
        # shared across users, so scope by uid or another user's in-flight
        # auto-close would leak into this response. Once the close order is live
        # it shows as a working order above, so drop watchers that already placed
        # their close. `stop_needs_attention` is a deliberate exception: it means
        # the stop-exit ladder refused to resubmit rather than risk an over-close,
        # so nothing is on the book to show as a working order — the watcher
        # itself is the only place this ever becomes visible, and it must stay
        # visible until a human clears it. `task`/`uid` are internal — never
        # returned to the client.
        my_uid = user.get("id")
        watchers = [{k: v for k, v in w.items() if k not in ("task", "uid")}
                    for w in _WATCHERS.values()
                    if w.get("state") in ("watching_fill", "stop_needs_attention")
                    and w.get("uid") == my_uid]
        return {"account_id": aid, "orders": orders, "watchers": watchers, "history": history}
    finally:
        if per_user:
            await client.close()


@router.post("/torque/cancel/{order_id}", dependencies=_GATE)
async def torque_cancel(order_id: str, request: Request, account_id: str | None = None,
                        user: dict = Depends(get_current_user)):
    """Cancel a working order (entry or close) from the orders panel."""
    broker, client, aid, per_user = await _resolve_torque_broker(request, user, account_id)
    await _require_tradier(broker, client, per_user, "cancelling an order")
    try:
        try:
            resp = await client.cancel_order(aid, order_id)
        except Exception as e:
            raise HTTPException(502, f"cancel failed: {e}")
        return {"order_id": order_id, "result": resp}
    finally:
        if per_user:
            await client.close()


class ModifyRequest(BaseModel):
    price: float
    duration: str | None = None
    account_id: str | None = None


def _enforce_close_floor_on_modify(order_id: str, new_price: float) -> None:
    """Reject an edit that pushes a profit-target close below the ticker's floor.

    debit  close = SELL to close → a LOWER price is a smaller profit → floor is a
                   MINIMUM price.
    credit close = BUY to close  → a HIGHER price keeps less of the credit → the
                   floor is a MAXIMUM price.
    Unknown orders (not placed by this process, or a native OTO close) pass
    through — we have no entry fill to measure against.
    """
    g = _CLOSE_GUARD.get(order_id)
    if not g or not tcfg.close_target_spread_scaled(g["symbol"]):
        return
    floor_px = teng.close_target_price(g["entry_fill"], g["entry_type"],
                                       g["floor_pct"], g["tick"],
                                       legs=g.get("legs", 1),
                                       fee_per_contract=g.get("fee_per_contract", 0.0))
    too_low = g["entry_type"] != "credit" and new_price < floor_px
    too_high = g["entry_type"] == "credit" and new_price > floor_px
    if too_low or too_high:
        raise HTTPException(400, (
            f"{g['symbol']}: close target must stay at or beyond {floor_px} "
            f"(≥{g['floor_pct']:.0f}% of the {g['entry_type']} entry fill "
            f"{g['entry_fill']}). A closer target prices the order inside the "
            f"package bid/ask and can never fill."))


@router.post("/torque/modify/{order_id}", dependencies=_GATE)
async def torque_modify(order_id: str, req: ModifyRequest, request: Request,
                        user: dict = Depends(get_current_user)):
    """Change a working order's limit price (and optionally duration).

    If this is a Torque profit-target close on a floor-enforced ticker (DJX), the
    same floor /torque/place applied is re-applied here — otherwise the orders
    panel would be a trivial bypass: place at the clamped 60% target, then edit
    the close down to 5% and get an order that can never fill."""
    broker, client, aid, per_user = await _resolve_torque_broker(request, user, req.account_id)
    await _require_tradier(broker, client, per_user, "modifying an order")
    _enforce_close_floor_on_modify(str(order_id), float(req.price))
    try:
        try:
            resp = await client.modify_order(aid, order_id, price=req.price, duration=req.duration)
        except Exception as e:
            raise HTTPException(502, f"modify failed: {e}")
        return {"order_id": order_id, "result": resp}
    finally:
        if per_user:
            await client.close()


@router.get("/torque/order/{order_id}", dependencies=_GATE)
async def torque_order(order_id: str, request: Request, account_id: str | None = None,
                       user: dict = Depends(get_current_user)):
    broker, client, aid, per_user = await _resolve_torque_broker(request, user, account_id)
    await _require_tradier(broker, client, per_user, "order status")
    try:
        try:
            return await client.get_order(aid, order_id)
        except Exception as e:
            raise HTTPException(502, f"get_order failed: {e}")
    finally:
        if per_user:
            await client.close()
