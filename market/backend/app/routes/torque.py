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
import logging
import re
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .. import auth
from ..auth import get_current_user
from ..config import get_settings
from ..broker_resolver import resolve_broker
from ..entitlements import ensure_tool
from ..order_builder import build_tradier_order_payload
from .. import torque_config as tcfg
from .. import torque_engine as teng

log = logging.getLogger("edgelane.market.torque")
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
    client + configured house account. Torque is Tradier-only, so a user whose
    active connection is Webull gets a clear 400.

    Returns (client, account_id, per_user). When per_user is True the caller owns
    the client and MUST close it (or hand it to the background watcher).
    """
    broker, client, account_override, per_user = await resolve_broker(request, user)
    if broker != "tradier":
        if per_user:
            await client.close()
        raise HTTPException(400, "Torque currently supports Tradier broker connections only.")
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
    return client, account_id, per_user


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

# tiny TTL cache so analyze + build hitting within one tick share a chain fetch
_CHAIN_CACHE: dict[str, tuple[float, float, str, list[dict]]] = {}
_CHAIN_TTL = 2.0

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
    try:
        q = await client.stock_quote(sym)
        spot = teng._f(q.get("last") or q.get("close") or q.get("prevclose"))
        exps = await client.option_expirations(sym)
        exp = teng.choose_expiration(exps)
        if not exp:
            raise HTTPException(404, f"no listed option expirations for {sym}")
        raw = await client.options_chain(sym, exp)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Tradier chain fetch failed for {sym}: {e}")
    contracts = teng.normalize_chain(raw)
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
        "env": settings.tradier_env,
        "mode": settings.tradier_mode,
        "devmode": bool(getattr(settings, "devmode", False)),
    }


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


@router.post("/torque/build", dependencies=_GATE)
async def torque_build(req: BuildRequest, request: Request):
    if req.strategy not in tcfg.STRATEGY_DEFS:
        raise HTTPException(400, f"unknown strategy {req.strategy!r}")
    client = _client(request)
    spot, exp, contracts = await _get_chain(client, req.symbol)
    struct = teng.build_structure(spot, contracts, req.symbol, req.strategy, req.adjustments)
    px_map = {c["symbol"]: c for c in contracts if c.get("symbol")}
    price = teng.price_structure(struct["legs"], px_map)
    tick = float(struct["rule"].get("tick", 0.05))
    # attach steppable grid neighbors per leg
    for lg in struct["legs"]:
        lg["grid"] = _grid_neighbors(contracts, lg["side"], lg["strike"])
    missing = [lg["role"] for lg in struct["legs"] if not lg.get("symbol")]
    return {
        "symbol": req.symbol.upper(), "expiration": exp, "spot": spot,
        "strategy": req.strategy, "name": struct["name"], "type": struct["type"],
        "legs": struct["legs"], "price": price, "tick": tick,
        "suggested_limit": teng.suggested_limit(price, tick),
        "missing_legs": missing,
    }


# ── live price ─────────────────────────────────────────────────────────────
class PriceRequest(BaseModel):
    legs: list[dict]            # [{symbol, action, quantity, side, strike, role}]
    symbol: str | None = None   # underlying (optional; recovered from legs if absent)


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
    return teng.price_structure(req.legs, px)


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
    spread_type: str | None = None        # "debit"|"credit" (net direction of the entry)
    confirm: bool = False
    dry_run: bool = False                 # preview-only: validate entry+close, place nothing
    account_id: str | None = None


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

    # Resolve the broker AFTER input validation so a malformed request never
    # opens a per-user client. Entitled users trade their OWN connection; the
    # admin/dev path uses the house account.
    client, account_id, per_user = await _resolve_torque_broker(request, user, req.account_id)
    handed_off = False    # set True once the background watcher owns the client
    try:
        tick = float(tcfg.ticker_rule(req.symbol, req.strategy).get("tick", 0.05))
        # Authoritative: a bull_call is a debit and a long fly is a debit
        # regardless of what a wide live bid/ask momentarily nets to — use the
        # declared type so the auto-close is built in the right direction.
        sdef = tcfg.STRATEGY_DEFS.get(req.strategy, {})
        entry_type = (sdef.get("type") or req.spread_type or "debit").lower()
        tag = f"torque{req.strategy}"

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
            close_px = teng.close_target_price(proxy_fill, entry_type, req.close_target_pct, tick)
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
            }

        # ── single-leg + limit + auto_close → native OTO bracket ──────────────
        if is_single and req.auto_close and otype == "limit":
            close_px = teng.close_target_price(float(req.limit_price), entry_type, req.close_target_pct, tick)
            close_side = "sell_to_close" if legs[0]["action"].startswith("buy") else "buy_to_close"
            oto = {
                "class": "oto", "duration": req.duration,
                "symbol[0]": req.symbol.upper(), "option_symbol[0]": legs[0]["symbol"],
                "side[0]": legs[0]["action"], "quantity[0]": str(int(legs[0]["quantity"]) * req.quantity),
                "type[0]": "limit", "price[0]": f"{float(req.limit_price):.2f}",
                "symbol[1]": req.symbol.upper(), "option_symbol[1]": legs[0]["symbol"],
                "side[1]": close_side, "quantity[1]": str(int(legs[0]["quantity"]) * req.quantity),
                "type[1]": "limit", "price[1]": f"{close_px:.2f}",
                "tag": re.sub(r"[^a-zA-Z0-9]", "", tag)[:30],
            }
            entry = await _submit(client, account_id, oto)
            rej, why = _is_rejected(entry)
            return {
                "mode": "oto_bracket", "rejected": rej, "reason": why,
                "entry": entry, "close_target_price": close_px,
                "account_id": account_id, "auto_close": True,
            }

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
        }
        if rej or not order_id:
            return result
        if not req.auto_close:
            return result

        # ── auto-close: watch for the fill in the BACKGROUND, then place the close.
        # We do NOT block the request — a limit entry can fill minutes later, and the
        # old synchronous 30s poll meant a slow fill silently got no close. The
        # watcher polls until the entry fills (or the day ends) and places the close
        # then, retrying if the position isn't settled yet. Progress shows in the
        # orders panel (/torque/orders). For a per-user broker, the watcher takes
        # ownership of the client (it needs it to poll for hours) and closes it.
        est_fill = float(req.limit_price) if req.limit_price else 0.0
        est_close = teng.close_target_price(est_fill, entry_type, req.close_target_pct, tick) if est_fill else None
        w = {
            "entry_order_id": str(order_id), "symbol": req.symbol.upper(),
            "strategy": req.strategy, "state": "watching_fill", "entry_status": "open",
            "close_order_id": None, "close_target_price": est_close,
            "close_reason": None, "done": False,
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
            quantity=req.quantity, close_client=per_user))
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


def _build_close_payload(*, account_id, symbol, strategy, legs, is_single,
                         entry_type, close_px, quantity):
    """Closing order payload (single option or multileg), GTC limit."""
    if is_single:
        cs = "sell_to_close" if legs[0]["action"].startswith("buy") else "buy_to_close"
        return _single_option_payload(
            symbol=symbol, leg={"symbol": legs[0]["symbol"], "action": cs,
                                "quantity": int(legs[0]["quantity"]) * quantity},
            otype="limit", price=close_px, duration="gtc", preview=False,
            tag=f"torqueClose{strategy}", side_override=cs)
    c_legs = teng.legs_to_order_legs(teng.build_close_legs(legs), quantity)
    return build_tradier_order_payload(
        account_id=account_id, symbol=symbol, legs=c_legs,
        order_type=_multileg_close_type(entry_type), limit_price=close_px,
        duration="gtc", preview=False, tag=f"torqueClose{strategy}")


async def _watch_and_close(client, account_id, entry_id, w, *, is_single, legs,
                           symbol, strategy, entry_type, pct, tick, quantity,
                           close_client=False):
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
        entry_fill = teng._f(filled.get("avg_fill_price")) or 0.0
        close_px = teng.close_target_price(float(entry_fill), entry_type, pct, tick)
        w["close_target_price"] = close_px
        payload = _build_close_payload(
            account_id=account_id, symbol=symbol, strategy=strategy, legs=legs,
            is_single=is_single, entry_type=entry_type, close_px=close_px,
            quantity=quantity)
        for attempt in range(_CLOSE_RETRIES):
            close = await _submit(client, account_id, payload)
            crej, cwhy = _is_rejected(close)
            if not crej:
                w["state"] = "close_placed"
                w["close_order_id"] = str(close.get("id") or "")
                return
            w["close_reason"] = cwhy
            # position may not be settled the instant the entry fills — retry
            if "position" in (cwhy or "").lower() and attempt < _CLOSE_RETRIES - 1:
                await asyncio.sleep(_CLOSE_RETRY_DELAY)
                continue
            break
        w["state"] = "close_rejected"
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
    """ONLY the live/working orders (no filled/closed history) plus active
    auto-close watchers still waiting for their entry to fill — feeds the
    bottom orders panel. Orders come from the caller's own broker account."""
    client, aid, per_user = await _resolve_torque_broker(request, user, account_id)
    try:
        try:
            raw = await client.get_orders(aid)
        except Exception as e:
            raise HTTPException(502, f"orders fetch failed: {e}")
        orders = []
        for o in raw:
            st = str(o.get("status") or "").lower()
            if st not in _WORKING_STATES:
                continue                          # skip filled/canceled/expired history
            orders.append({
                "id": str(o.get("id") or ""), "symbol": o.get("symbol"),
                "class": o.get("class"), "type": o.get("type"), "side": o.get("side"),
                "status": st, "working": True,
                "quantity": teng._f(o.get("quantity")), "exec_quantity": teng._f(o.get("exec_quantity")),
                "price": teng._f(o.get("price")), "avg_fill_price": teng._f(o.get("avg_fill_price")),
                "duration": o.get("duration"), "tag": o.get("tag"),
                "create_date": o.get("create_date"),
            })
        orders.sort(key=lambda x: x.get("create_date") or "", reverse=True)
        # Show watchers that are still PENDING a close (entry not yet confirmed
        # closed) AND belong to THIS caller — _WATCHERS is process-global and
        # shared across users, so scope by uid or another user's in-flight
        # auto-close would leak into this response. Once the close order is live
        # it shows as a working order above, so drop watchers that already placed
        # their close. `task`/`uid` are internal — never returned to the client.
        my_uid = user.get("id")
        watchers = [{k: v for k, v in w.items() if k not in ("task", "uid")}
                    for w in _WATCHERS.values()
                    if w.get("state") in ("watching_fill",) and w.get("uid") == my_uid]
        return {"account_id": aid, "orders": orders, "watchers": watchers}
    finally:
        if per_user:
            await client.close()


@router.post("/torque/cancel/{order_id}", dependencies=_GATE)
async def torque_cancel(order_id: str, request: Request, account_id: str | None = None,
                        user: dict = Depends(get_current_user)):
    """Cancel a working order (entry or close) from the orders panel."""
    client, aid, per_user = await _resolve_torque_broker(request, user, account_id)
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


@router.post("/torque/modify/{order_id}", dependencies=_GATE)
async def torque_modify(order_id: str, req: ModifyRequest, request: Request,
                        user: dict = Depends(get_current_user)):
    """Change a working order's limit price (and optionally duration)."""
    client, aid, per_user = await _resolve_torque_broker(request, user, req.account_id)
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
    client, aid, per_user = await _resolve_torque_broker(request, user, account_id)
    try:
        try:
            return await client.get_order(aid, order_id)
        except Exception as e:
            raise HTTPException(502, f"get_order failed: {e}")
    finally:
        if per_user:
            await client.close()
