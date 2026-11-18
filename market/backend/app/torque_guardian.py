"""Torque stop guardian — stop-loss protection at the POSITION level.

Torque's other protections are tied to orders Torque itself placed: a broker-
held bracket (single legs) or an app watcher (spreads, market entries). The
guardian closes the gaps those leave by looking at what the account actually
HOLDS, whoever placed it — Torque, Reflex, Tradier's own site, the tp CLI:

  * every ~GUARDIAN_INTERVAL_SEC, for every Torque account, read the open
    option positions, group them into units (one single option, or a spread
    the day's order history confirms was one order — anything it can't
    confirm is left alone and flagged), and decide each unit's stop:
      - a Reflex (news-signal) position: ALWAYS, at the ticker default (or 30%);
      - anything else: the Torque page's saved stop-loss setting for that
        ticker (torque_sl_settings; no row = on at the ticker default);
  * leave a unit alone while Torque's own watcher manages it;
  * when a broker-held stop is resting for it, stand back — but if the price
    is past the stop and that stop hasn't filled within GUARDIAN_TAKEOVER_SEC,
    take over: cancel the broker's orders on it and run the normal exit ladder;
  * when nothing protects it, run the exit ladder on breach;
  * a position already past its stop the first time the guardian sees it is
    ALERTED, never auto-closed (it re-arms once the price is back above the
    stop).

State per unit is persisted (torque_guarded_positions) so a restart keeps it.
Only Torque-configured tickers are guarded (their safety rules come from
torque_tickers_setup). Runs in one backend container (GUARDIAN_ENABLED).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time
import uuid
from math import gcd
from typing import Any

from . import supabase_admin
from . import torque_config as tcfg
from . import torque_engine as teng
from .logsetup import txn

log = logging.getLogger("edgelane.market.torque_guardian")

_OCC = re.compile(r"^([A-Z]+?)(\d{6})([CP])(\d{8})$")


def account_key(client: Any, account_id: str | None) -> str:
    """"<live|sandbox>:<broker account id>" — identifies a broker account
    across users and containers (the house account and a user's own
    connection to the same account share it)."""
    env = "sandbox" if "sandbox" in str(getattr(client, "base_url", "")) else "live"
    return f"{env}:{account_id or ''}"


def ticker_of(option_symbol: str) -> str | None:
    """The configured Torque ticker an OCC option symbol belongs to (NDXP →
    NDX, SPXW → SPX, DJXW → DJX), or None if it's not a Torque ticker."""
    m = _OCC.match(str(option_symbol or "").upper())
    if not m:
        return None
    root = m.group(1)
    known = sorted(tcfg.known_tickers(), key=len, reverse=True)
    for t in known:
        if root == t:
            return t
    for t in known:
        if root.startswith(t):
            return t
    return None


# ── trade direction (one open trade per ticker per direction) ──────────────
def structure_dir(legs: list[dict]) -> str | None:
    """'bull' | 'bear' | None for a structure given as legs
    [{symbol: OCC, action: buy_to_open|sell_to_open|...}]. Singles and
    verticals by their payoff; same-type butterflies follow Torque's own
    convention (call fly bull, put fly bear); anything mixing calls and puts
    (condor, iron fly, strangle) has no single direction → None."""
    parts = []
    for lg in legs or []:
        m = _OCC.match(str(lg.get("symbol") or lg.get("option_symbol") or "").upper())
        if not m:
            return None
        act = str(lg.get("action") or lg.get("side") or "").lower()
        parts.append((m.group(3), int(m.group(4)) / 1000.0, act.startswith("buy")))
    if not parts:
        return None
    if len(parts) == 1:
        cp, _k, long_ = parts[0]
        return "bull" if (cp == "C") == long_ else "bear"
    types = {cp for cp, _k, _l in parts}
    if len(types) != 1:
        return None
    cp = types.pop()
    longs = [k for _c, k, l in parts if l]
    shorts = [k for _c, k, l in parts if not l]
    if len(parts) == 2 and len(longs) == 1 and len(shorts) == 1:
        if cp == "C":
            return "bull" if longs[0] < shorts[0] else "bear"
        return "bear" if longs[0] > shorts[0] else "bull"
    if len(parts) == 3:
        return "bull" if cp == "C" else "bear"
    if not shorts:
        return "bull" if cp == "C" else "bear"
    if not longs:
        return "bear" if cp == "C" else "bull"
    return None


def order_entry_dir(order: dict) -> str | None:
    """Direction of a WORKING opening order: from the strategy in its Torque
    tag when present, else from its *_to_open legs. None if it isn't a
    working entry or has no single direction."""
    if str(order.get("status") or "").lower() not in _WORKING:
        return None
    legs = [{"symbol": lg.get("option_symbol"), "action": lg.get("side")} for lg in _legs_of(order)
            if str(lg.get("side") or "").lower().endswith("to_open") and lg.get("option_symbol")]
    if not legs and order.get("option_symbol") and str(order.get("side") or "").lower().endswith("to_open"):
        legs = [{"symbol": order.get("option_symbol"), "action": order.get("side")}]
    if not legs:
        return None
    tag = str(order.get("tag") or "").lower().replace("_", "").replace("-", "")
    if tag.startswith("torque"):
        for key in sorted(tcfg.STRATEGY_DEFS, key=len, reverse=True):
            if key.replace("_", "") in tag:
                return tcfg.STRATEGY_DEFS[key].get("dir")
    return structure_dir(legs)


def open_directions(positions: list[dict], orders: list[dict], ticker: str) -> set[str]:
    """Directions ('bull'/'bear') the account already has on `ticker`: open
    option positions (grouped into structures like the guardian does) plus
    working opening orders."""
    t = ticker.upper()
    dirs: set[str] = set()
    units, flagged = build_units(positions, orders)
    for u in units:
        if u["ticker"] == t and (d := structure_dir(u["legs"])):
            dirs.add(d)
    qty = {str(p.get("symbol") or "").upper(): _qty(p) for p in positions}
    for f in flagged:
        if f.get("ticker") != t:
            continue
        for sym in f.get("symbols") or []:
            q = qty.get(str(sym).upper()) or 0.0
            if q and (d := structure_dir([{"symbol": sym, "action": "buy" if q > 0 else "sell"}])):
                dirs.add(d)
    for o in orders:
        if any(ticker_of(sym) == t for sym in _order_symbols(o)) and (d := order_entry_dir(o)):
            dirs.add(d)
    return dirs


def default_stop_pct(ticker: str) -> float:
    return float(tcfg.stop_loss_default(ticker) or tcfg.DEFAULT_STOP_LOSS_PCT)


# ── runtime state (per container; durable parts live in the DB) ────────────
_WORKING = {"open", "pending", "partially_filled", "calculated", "accepted", "queued", "received"}
_STOP_TYPES = {"stop", "stop_limit"}
_ACTIVE_EXITS: dict[tuple[str, str], asyncio.Task] = {}
_BREACH_SINCE: dict[tuple[str, str], float] = {}     # breach first seen while a broker stop rests
_WIDE_TICKS: dict[tuple[str, str], int] = {}
_SETTINGS_CACHE: dict[str, dict[str, tuple[bool, float]]] = {}   # account_key -> ticker -> (on, pct)
STATUS: dict[str, Any] = {"last_run": None, "accounts": 0, "units": 0, "skipped_closed": False,
                          "lease": None}
# This process's identity for the single-runner lease (migration 0025).
LEASE_HOLDER = re.sub(r"[^A-Za-z0-9_-]", "-", f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
_REFUSED_LIVE: set[str] = set()      # live accounts already logged as refused in DEVMODE


# ── order / position helpers ────────────────────────────────────────────────
def _legs_of(order: dict) -> list[dict]:
    legs = order.get("leg") or order.get("legs")
    if isinstance(legs, dict):
        legs = [legs]
    return legs if isinstance(legs, list) else []


def _order_symbols(order: dict) -> set[str]:
    syms = {str(order["option_symbol"]).upper()} if order.get("option_symbol") else set()
    syms |= {str(lg["option_symbol"]).upper() for lg in _legs_of(order) if lg.get("option_symbol")}
    return syms


_FILLED = ("filled", "partially_filled")
_TORQUE_EXIT_TAGS = ("torqueclose", "torquestop", "torquecancel")


def torque_opened_symbols(orders: list[dict]) -> set[str]:
    """Contracts a Torque-tagged entry order opened today (filled, or a
    bracket whose entry leg filled). Torque tags every entry `torque<…>`;
    its exits are `torqueClose/Stop/Cancel…` and don't count. Tradier lists
    only today's orders — the guardian persists the source it saw, so a
    position keeps it on later days."""
    out: set[str] = set()
    for o in orders or []:
        tag = str(o.get("tag") or "").lower()
        if not tag.startswith("torque") or tag.startswith(_TORQUE_EXIT_TAGS):
            continue
        legs = _legs_of(o)
        if str(o.get("status") or "").lower() not in _FILLED \
                and not any(str(lg.get("status") or "").lower() in _FILLED for lg in legs):
            continue
        if o.get("option_symbol") and str(o.get("side") or "").lower().endswith("to_open"):
            out.add(str(o["option_symbol"]).upper())
        for lg in legs:
            if lg.get("option_symbol") and str(lg.get("side") or "").lower().endswith("to_open"):
                out.add(str(lg["option_symbol"]).upper())
    return out


_SOURCE_RANK = {"outside": 0, "torque": 1, "reflex": 2}


def unit_source(unit: dict, row: dict | None, reflex_syms: set[str], torque_syms: set[str]) -> str:
    """Who opened this unit: 'reflex' (a Reflex signal), 'torque' (a Torque
    order) or 'outside' (Tradier's site, the tp CLI, …). The strongest seen
    so far sticks — a saved 'torque'/'reflex' isn't lost once today's orders
    roll off."""
    syms = set(unit["symbols"])
    seen = "reflex" if syms & reflex_syms else "torque" if syms & torque_syms else "outside"
    saved = (row or {}).get("source")
    return saved if _SOURCE_RANK.get(saved, -1) > _SOURCE_RANK[seen] else seen


def closing_orders(orders: list[dict], symbols: set[str]) -> tuple[list[dict], bool]:
    """Working orders that would close any of `symbols`, and whether one of
    them is a live STOP (a stop/stop-limit order, or a bracket whose stop leg
    is still working)."""
    out, has_stop = [], False
    for o in orders or []:
        if str(o.get("status") or "").lower() not in _WORKING or not (_order_symbols(o) & symbols):
            continue
        legs = _legs_of(o)
        if str(o.get("class") or "").lower() in ("oto", "otoco", "oco"):
            out.append(o)
            has_stop |= any(str(lg.get("type") or "").lower() in _STOP_TYPES
                            and str(lg.get("status") or "").lower() in _WORKING for lg in legs)
            continue
        sides = [str(o.get("side") or "")] + [str(lg.get("side") or "") for lg in legs]
        if any("to_close" in sd for sd in sides):
            out.append(o)
            has_stop |= str(o.get("type") or "").lower() in _STOP_TYPES
    return out, has_stop


def _qty(p: dict) -> float:
    return teng._f(p.get("quantity")) or 0.0


def build_units(positions: list[dict], orders_today: list[dict]) -> tuple[list[dict], list[dict]]:
    """Group open option positions into guardable units.

    One open option on a Torque ticker = a single-leg unit. Several on the
    same ticker are grouped only as far as TODAY's order history confirms
    (Tradier lists only the current session's orders): a filled multi-leg
    order whose legs match open positions in the same proportion is one
    spread unit; a remaining leg with a filled single-option opening order is
    a single unit. Anything else is returned as `flagged` and left alone —
    stopping one leg of an unconfirmed spread could leave the other naked.
    Positions on tickers Torque doesn't configure are ignored."""
    by_ticker: dict[str, dict[str, dict]] = {}
    for p in positions or []:
        sym = str(p.get("symbol") or "").upper()
        t = ticker_of(sym)
        if t and _qty(p):
            by_ticker.setdefault(t, {})[sym] = p
    units: list[dict] = []
    flagged: list[dict] = []
    filled = [o for o in orders_today or [] if str(o.get("status") or "").lower() == "filled"]
    for t, pos in by_ticker.items():
        remaining = dict(pos)
        if len(remaining) > 1:
            for o in filled:
                if str(o.get("class") or "").lower() != "multileg":
                    continue
                legs = [(str(lg.get("option_symbol") or "").upper(), str(lg.get("side") or "").lower(),
                         teng._f(lg.get("quantity")) or 0.0) for lg in _legs_of(o)]
                if not legs or any(s not in remaining or q <= 0 for s, _sd, q in legs):
                    continue
                ks = set()
                for s, sd, q in legs:
                    pq = _qty(remaining[s])
                    if (pq > 0) != sd.startswith("buy"):
                        ks = {None}
                        break
                    ks.add(abs(pq) / q)
                if len(ks) != 1 or None in ks:
                    continue
                k = ks.pop()
                if k != int(k) or k <= 0:
                    continue
                g = 0
                for _s, _sd, q in legs:
                    g = gcd(g, int(q))
                units.append(_unit(t, [remaining.pop(s) for s, _sd, _q in legs], k=int(k) * g))
        if len(remaining) == 1 and len(pos) == 1:
            units.append(_unit(t, list(remaining.values())))
            continue
        for s in list(remaining):
            if _single_confirmed(s, _qty(remaining[s]), filled):
                units.append(_unit(t, [remaining.pop(s)]))
        for s, p in remaining.items():
            flagged.append({"ticker": t, "key": s, "symbols": [s],
                            "note": "part of a multi-leg position the order history can't confirm — not guarded"})
    return units, flagged


def _single_confirmed(sym: str, held: float, filled: list[dict]) -> bool:
    """A leg sharing its ticker with other open legs is a standalone single
    only when today's filled single-option opening orders on that contract
    account for the WHOLE held quantity, on the held side. Anything less (some
    of it came from an earlier day, or from a spread) and selling it could
    strip the cover off a short leg — leave it flagged."""
    opened = 0.0
    for o in filled:
        if str(o.get("class") or "").lower() != "option" \
                or str(o.get("option_symbol") or "").upper() != sym:
            continue
        side = str(o.get("side") or "").lower()
        if not side.endswith("to_open"):
            continue
        q = teng._f(o.get("exec_quantity")) or teng._f(o.get("quantity")) or 0.0
        opened += q if side.startswith("buy") else -q
    return bool(held) and opened == held


def _unit(ticker: str, plist: list[dict], k: int | None = None) -> dict:
    syms = [str(p["symbol"]).upper() for p in plist]
    qtys = [int(abs(_qty(p))) for p in plist]
    if k is None:
        g = 0
        for q in qtys:
            g = gcd(g, q)
        k = g or 1
    legs = [{"symbol": s, "action": ("buy_to_open" if _qty(p) > 0 else "sell_to_open"),
             "quantity": int(abs(_qty(p)) // k)} for s, p in zip(syms, plist)]
    cost = sum(teng._f(p.get("cost_basis")) or 0.0 for p in plist)
    entry = abs(cost) / (k * 100.0)
    return {"ticker": ticker, "key": "|".join(sorted(syms)), "symbols": syms, "legs": legs,
            "qty": k, "single": len(syms) == 1, "entry": round(entry, 4),
            "entry_type": "debit" if cost > 0 else "credit"}


def held_multiple(unit: dict, held: dict[str, dict]) -> int | None:
    """How many of `unit` the account still holds — every leg present, on the
    same side, in the unit's exact proportion — or None if it no longer
    matches (closed, partly closed, or changed): never guess a size."""
    ks = set()
    for lg in unit["legs"]:
        p = held.get(lg["symbol"])
        q = _qty(p) if p else 0.0
        if not q or (q > 0) != lg["action"].startswith("buy") or abs(q) % lg["quantity"]:
            return None
        ks.add(int(abs(q) // lg["quantity"]))
    return ks.pop() if len(ks) == 1 else None


def _quote_map(quotes: list[dict]) -> dict[str, dict]:
    return {str(q.get("symbol") or "").upper(): q for q in quotes or [] if q.get("symbol")}


# ── accounts ────────────────────────────────────────────────────────────────
async def _accounts(app, *, sandbox_only: bool = False) -> list[dict]:
    """Every broker account Torque trades on: the house account (admin
    page) plus each user entitled to Torque with an active Tradier
    connection. De-duplicated by account key; `owned` = we must close it."""
    from .broker_resolver import resolve_client_for_uid
    out: dict[str, dict] = {}
    house = getattr(app.state, "tradier", None)
    hid = getattr(app.state, "tradier_account_id", "") or ""
    if house is not None and hid and not getattr(app.state, "using_mock", False):
        out[account_key(house, hid)] = {"client": house, "account_id": hid, "uids": [], "owned": False}
    for uid in await supabase_admin.get_users_with_tool("torque") or []:
        try:
            resolved = await resolve_client_for_uid(uid)
        except Exception as e:
            log.warning("guardian: broker for %s unavailable: %s", uid, e)
            continue
        if not resolved:
            continue
        broker, client, aid = resolved
        if broker != "tradier":
            await _close(client)
            continue
        try:
            aid = aid or await client.resolve_account_id()
        except Exception:
            aid = None
        key = account_key(client, aid)
        if not aid or key in out:
            if key in out:
                out[key]["uids"].append(uid)
            await _close(client)
            continue
        out[key] = {"client": client, "account_id": aid, "uids": [uid], "owned": True}
    if sandbox_only:
        # A DEVMODE backend must never act on a real-money account: users'
        # own Tradier connections are production whatever DEVMODE says.
        for k in [k for k in out if not k.startswith("sandbox:")]:
            if k not in _REFUSED_LIVE:
                _REFUSED_LIVE.add(k)
                txn("stop guardian refused live account", account=k, reason="DEVMODE guards sandbox accounts only")
            if out[k]["owned"]:
                await _close(out[k]["client"])
            del out[k]
    return [{"key": k, **v} for k, v in out.items()]


async def _close(client) -> None:
    try:
        await client.close()
    except Exception:
        pass


# ── session-based exits (extended-hours tickers) ───────────────────────────
_TP_CACHE: dict[str, dict[str, tuple[bool, float | None]]] = {}
_REPRICE_FAIL: dict[tuple[str, str], float] = {}
_TP_FAIL: dict[tuple[str, str], float] = {}
_GAP_LOGGED: set[tuple[str, str]] = set()
_REPRICE_RETRY_SEC = 60.0


def tp_reached_at(exec_px: float | None, target_px: float, entry_type: str) -> bool:
    """Is the executable exit price at or beyond the take-profit price?
    debit → can sell for >= target; credit → can buy back for <= target."""
    if exec_px is None or target_px is None:
        return False
    return abs(exec_px) <= abs(target_px) if entry_type == "credit" else abs(exec_px) >= abs(target_px)


def _working_stop_legs(closes: list[dict]) -> list[tuple[dict, str]]:
    """(parent order, order tag) for every working stop / stop_limit close
    among `closes` — the parent is what gets cancelled (an OCO's legs go
    together)."""
    out = []
    for o in closes:
        tag = str(o.get("tag") or "").lower()
        legs = _legs_of(o) if str(o.get("class") or "").lower() in ("oto", "otoco", "oco") else [o]
        for lg in legs:
            if str(lg.get("type") or "").lower() in _STOP_TYPES and "to_close" in str(lg.get("side") or "") \
                    and str(lg.get("status") or "").lower() in _WORKING:
                out.append((o, tag))
    return out


_CANCEL_CONFIRM_TRIES = 3


async def _replace_stop_with_take_profit(client, account_id: str, akey: str, unit: dict,
                                         order: dict, price: float) -> tuple[bool, str]:
    """Cancel the resting stop order, confirm it is cancelled, then place the
    take-profit limit. Never places while the stop might still be live; if the
    stop filled instead, there's nothing left to close."""
    oid = order.get("id")
    try:
        await client.cancel_order(account_id, oid)
    except Exception as e:
        log.warning("guardian: cancel of stop %s failed: %s", oid, e)
    status = ""
    for i in range(_CANCEL_CONFIRM_TRIES):
        try:
            status = str((await client.get_order(account_id, oid)).get("status") or "").lower()
        except Exception:
            status = ""
        if status in ("canceled", "filled", "rejected", "expired"):
            break
        await asyncio.sleep(0.5 * (i + 1))
    txn("guardian take-profit: stop cancel", account=akey, unit=unit["key"], order_id=oid, status=status)
    if status == "filled":
        return False, "the stop filled first — position closed by the stop"
    if status not in ("canceled", "rejected", "expired"):
        return False, f"couldn't confirm the stop {oid} was cancelled ({status or 'unknown'}) — not placing a second close"
    # Size the take-profit to what is still held — the stop may have partly
    # filled before its cancel landed.
    try:
        held = {str(p.get("symbol") or "").upper(): p for p in await client.get_positions(account_id)}
    except Exception as e:
        return False, f"couldn't re-read the position after cancelling the stop: {str(e)[:120]}"
    if not set(unit["symbols"]) & set(held):
        return False, "the position closed while the stop was being cancelled"
    k = held_multiple(unit, held)
    if not k:
        return False, "the position changed while the stop was being cancelled — review"
    return await _place_take_profit(client, account_id, akey, {**unit, "qty": k}, price)


async def _place_take_profit(client, account_id: str, akey: str, unit: dict,
                             price: float) -> tuple[bool, str]:
    """A GTC limit close for `unit` at its take-profit price."""
    from .routes import torque as troute
    try:
        payload = troute._build_close_payload(
            account_id=account_id, symbol=unit["ticker"], strategy="guard", legs=unit["legs"],
            is_single=unit["single"], entry_type=unit["entry_type"], close_px=price,
            quantity=unit["qty"], entry_order_id=None, duration="gtc",
            tag_prefix="torqueCloseGuard", market=False)
        res = await troute._submit(client, account_id, payload)
        rej, why = troute._is_rejected(res)
    except Exception as e:
        rej, why = True, str(e)[:200]
    txn("guardian take-profit close", account=akey, unit=unit["key"], price=price,
        rejected=rej, reason=why or None)
    return (not rej), why


def regular_exit_pcts(unit: dict, source: str, settings: dict | None,
                      tp_saved: dict) -> tuple[float, float]:
    """(stop %, target %) a Torque / Reflex position uses in the regular
    session: Reflex → the ticker's defaults; Torque-placed → the account's
    saved setting, else the ticker's defaults."""
    t = unit["ticker"]
    sl = default_stop_pct(t)
    tp = float(tcfg.close_target_default(t))
    if source == "torque":
        sl = float((settings or {}).get(t, (True, sl))[1])
        saved = tp_saved.get(t)
        if saved and saved[1] is not None:
            tp = float(saved[1])
    return sl, max(tp, float(tcfg.close_target_min(t)))


def _is_limit_close(o: dict) -> bool:
    legs = _legs_of(o) if str(o.get("class") or "").lower() in ("oto", "otoco", "oco") else [o]
    return any(str(lg.get("type") or "").lower() == "limit" and "to_close" in str(lg.get("side") or "")
               and str(lg.get("status") or "").lower() in _WORKING for lg in legs)


def _profit_side(price: float | None, unit: dict) -> bool:
    """A close limit priced on the profit side of entry (a take-profit), as
    opposed to a stop leg that has triggered into a plain limit."""
    if price is None or not unit.get("entry"):
        return False
    e = abs(float(unit["entry"]))
    return abs(price) < e if unit["entry_type"] == "credit" else abs(price) > e


def exit_targets(unit: dict, sl_pct: float, tp_pct: float) -> dict[str, dict[str, float]]:
    """Prices Torque's resting exits should carry for `unit` at these %:
    {'limit': {price}, 'stop_limit': {stop, price}, 'stop': {stop}}."""
    from .routes import torque as troute
    t, et = unit["ticker"], unit["entry_type"]
    tick = tcfg.structure_tick(t, single=unit["single"])
    tp_px = teng.close_target_price(unit["entry"], et, float(tp_pct), tick,
                                    **troute._fee_kw(t, unit["legs"]))
    trig, lim = teng.stop_limit_leg_prices(unit["entry"], et, float(sl_pct), tick,
                                           tcfg.STOP_LIMIT_BUFFER_TICKS)
    return {"limit": {"price": tp_px}, "stop_limit": {"stop": trig, "price": lim},
            "stop": {"stop": trig}, "_tick": {"price": tick}}


async def reprice_exits(client, account_id: str, akey: str, unit: dict, orders: list[dict], *,
                        now: float, want: tuple[float, float],
                        only_from: tuple[float, float] | None = None) -> int:
    """Move Torque's own resting exits on `unit` (orders tagged torque*: the
    bracket's profit limit / stop-limit legs, a watcher's close) to the prices
    for `want` = (stop %, target %). With `only_from`, a leg is moved only if it
    currently sits at the `only_from` prices — so returning to regular values
    at the open never touches an exit Torque didn't move (floors, clamps and
    hand-set prices stay). A failed modify is retried after
    _REPRICE_RETRY_SEC; the guardian's own SL / TP enforcement still applies.
    Returns the number of legs modified."""
    new = exit_targets(unit, *want)
    tick = new.pop("_tick")["price"]
    old = exit_targets(unit, *only_from) if only_from else None
    if old:
        old.pop("_tick", None)
    tol = tick / 2.0
    n = 0
    gap = abs(new["limit"]["price"] - new["stop_limit"]["stop"])
    for o in closing_orders(orders, set(unit["symbols"]))[0]:
        if not str(o.get("tag") or "").lower().startswith("torque"):
            continue
        cls = str(o.get("class") or "").lower()
        if cls in ("otoco", "oco") and gap < tcfg.OCO_MIN_PRICE_GAP:
            # Tradier refuses an OCO whose two prices are under $0.10 apart.
            gkey = (akey, str(o.get("id")))
            if gkey not in _GAP_LOGGED:
                _GAP_LOGGED.add(gkey)
                txn("exit reprice skipped", account=akey, unit=unit["key"], order_id=o.get("id"),
                    reason=f"target and stop would be {gap:.2f} apart — under the broker's "
                           f"{tcfg.OCO_MIN_PRICE_GAP:.2f} OCO minimum", stop_pct=want[0], target_pct=want[1])
            continue
        legs = _legs_of(o) if cls in ("oto", "otoco", "oco") else [o]
        for lg in legs:
            typ = str(lg.get("type") or "").lower()
            if typ not in new or "to_close" not in str(lg.get("side") or "") \
                    or str(lg.get("status") or "").lower() not in _WORKING or not lg.get("id"):
                continue
            cur = {"price": teng._f(lg.get("price")), "stop": teng._f(lg.get("stop_price") or lg.get("stop"))}
            if typ == "limit" and not _profit_side(cur["price"], unit):
                continue        # a triggered stop leg now working as a limit — never move it up
            target = new[typ]
            if all(cur.get(k) is not None and abs(cur[k] - v) < tol for k, v in target.items()):
                continue                                    # already there
            if old and not all(cur.get(k) is not None and abs(cur[k] - v) < tol
                               for k, v in old[typ].items()):
                continue                                    # not one Torque moved — leave it
            key = (akey, str(lg["id"]))
            if key in _REPRICE_FAIL and now - _REPRICE_FAIL[key] < _REPRICE_RETRY_SEC:
                continue
            try:
                await client.modify_order(account_id, lg["id"], price=target.get("price"),
                                          stop=target.get("stop"))
                _REPRICE_FAIL.pop(key, None)
                n += 1
                txn("exit repriced", account=akey, unit=unit["key"], order_id=lg["id"], type=typ,
                    **{k: v for k, v in target.items()}, stop_pct=want[0], target_pct=want[1])
            except Exception as e:
                _REPRICE_FAIL[key] = now
                log.warning("guardian: reprice of %s on %s failed: %s", lg.get("id"), unit["key"], e)
    return n


# ── per-account inputs ──────────────────────────────────────────────────────
def sl_saved_row(row: dict) -> bool:
    """Whether a torque_sl_settings row holds a real stop-loss save. False only
    for a TP-only row (sl_saved false AND take-profit data present). A row with
    no TP data can only have come from a stop-loss save — this also covers a
    row saved by a backend that predates the sl_saved column."""
    return row.get("sl_saved", True) is not False or row.get("tp_enabled") is None


async def _sl_settings(key: str) -> dict[str, tuple[bool, float]] | None:
    """ticker -> (enabled, pct) for the account; last good copy on a failed
    read; None if it has never loaded (non-Reflex units are then skipped)."""
    rows = await supabase_admin.get_sl_settings(key)
    if rows is not None:
        _TP_CACHE[key] = {str(r.get("ticker") or "").upper(): (
            bool(r.get("tp_enabled")), float(r["tp_pct"]) if r.get("tp_pct") is not None else None)
            for r in rows if r.get("tp_enabled") is not None}
        _SETTINGS_CACHE[key] = {
            str(r.get("ticker") or "").upper(): (
                bool(r.get("enabled")),
                float(r["pct"]) if r.get("pct") is not None else default_stop_pct(str(r.get("ticker") or "")))
            for r in rows if sl_saved_row(r)}   # a TP-only row: SL never saved
    return _SETTINGS_CACHE.get(key)


async def _reflex_symbols(uids: list[str]) -> set[str]:
    out: set[str] = set()
    for uid in uids:
        for r in await supabase_admin.get_open_reflex_orders(uid) or []:
            if str(r.get("status") or "").lower() in ("rejected", "canceled", "expired"):
                continue
            for lg in r.get("legs") or []:
                if lg.get("symbol"):
                    out.add(str(lg["symbol"]).upper())
    return out


async def _watcher_symbols() -> set[str]:
    """Symbols Torque's own app watchers are managing (this container in
    memory, plus every container's persisted watchers) — the guardian never
    touches those."""
    from .routes import torque as troute
    out: set[str] = set()
    sources = [w for w in troute._WATCHERS.values() if not w.get("done")]
    rows = await supabase_admin.select_many("torque_watchers", filters={"done": "eq.false"})
    sources += list(rows or [])
    for w in sources:
        for lg in w.get("legs") or []:
            if lg.get("symbol"):
                out.add(str(lg["symbol"]).upper())
    return out


# ── the decision for one unit ───────────────────────────────────────────────
def decide(unit: dict, *, row: dict | None, source: str, settings: dict | None,
           watcher_syms: set[str], px: dict | None, has_stop: bool, now: float,
           akey: str, takeover_sec: float,
           ext_stop: float | None = None) -> tuple[str, str | None, float | None]:
    """(action, note, stop_pct) for one unit. Actions:
    'watcher_managed' | 'sl_off' | 'no_settings' | 'no_quote' | 'past_stop_alert'
    | 'watching' | 'broker_stop' | 'wide_book' | 'exit'.

    Stop-loss by source: Reflex → always on at the ticker default; Torque →
    the page's saved per-ticker setting, on at the default when none is
    saved; outside (Tradier's site, tp CLI, …) → only when stop-loss has been
    switched ON for that ticker on the Torque page (no saved setting = off).
    In the extended session (`ext_stop` set, extended-hours tickers only) the
    extended stop applies: always on for Reflex and Torque-placed positions
    (not tied to the page), and the % for an outside position whose stop-loss
    is switched on."""
    ukey = (akey, unit["key"])
    if set(unit["symbols"]) & watcher_syms:
        return "watcher_managed", "managed by Torque's own watcher", None
    t = unit["ticker"]
    if ext_stop is not None and source in ("reflex", "torque"):
        enabled, pct = True, float(ext_stop)
    elif source == "reflex":
        enabled, pct = True, default_stop_pct(t)
    elif settings is None:
        return "no_settings", "stop-loss settings not loaded yet", None
    elif source == "torque":
        enabled, pct = settings.get(t, (True, default_stop_pct(t)))
    else:
        if t not in settings:
            return "sl_off", (f"stop-loss off — opened outside Torque; switch stop-loss on for {t} "
                              f"on the Torque page to guard it"), None
        enabled, pct = settings[t]
        if ext_stop is not None:
            pct = float(ext_stop)
    if not enabled:
        return "sl_off", f"stop-loss turned off for {t}", None
    if not px or not px.get("complete"):
        return "no_quote", "no live quote", pct
    breached = teng.stop_breached(px.get("abs_mid"), unit["entry"], unit["entry_type"], pct)
    prior = (row or {}).get("state")
    if row is None and breached:
        return "past_stop_alert", (f"already past its {pct:g}% stop when first seen "
                                   f"(entry {unit['entry']}, mark {px.get('abs_mid')}) — alert only, not closed"), pct
    if prior == "past_stop_alert" and breached:
        return "past_stop_alert", (row or {}).get("note"), pct
    if not breached:
        _BREACH_SINCE.pop(ukey, None)
        _WIDE_TICKS.pop(ukey, None)
        return ("broker_stop" if has_stop else "watching"), None, pct
    if has_stop:
        since = _BREACH_SINCE.setdefault(ukey, now)
        if now - since < takeover_sec:
            return "broker_stop", "past the stop — waiting for the broker's stop to fill", pct
    sp = teng.package_spread_pct(px)
    if sp is not None and sp > tcfg.STOP_MAX_EXIT_SPREAD_PCT:
        _WIDE_TICKS[ukey] = _WIDE_TICKS.get(ukey, 0) + 1
        if _WIDE_TICKS[ukey] < tcfg.STOP_ESCALATE_AFTER_TICKS:
            return "wide_book", f"past the stop but the book is {sp:.0f}% wide — re-checking", pct
    return "exit", (f"stop {pct:g}% breached (entry {unit['entry']}, mark {px.get('abs_mid')})"
                    + (" — broker stop didn't fill, taking over" if has_stop else "")), pct


# ── exit ────────────────────────────────────────────────────────────────────
async def _exit_unit(akey: str, client, account_id: str, unit: dict, orders: list[dict],
                     pct: float, owned: bool, extended: bool = False) -> None:
    """Close a unit whose stop is breached. First cancel every working order
    that would close it (a bracket whose stop didn't fill, a resting profit
    target, a stale exit) so the exit isn't rejected as over-closing, then
    re-read the position — it may have just closed on its own — and run the
    normal exit ladder for exactly what is still held."""
    from .routes import torque as troute
    ukey = (akey, unit["key"])
    syms = set(unit["symbols"])
    state, note = "exiting", None
    try:
        if extended:
            # Extended session: the only exit the broker accepts is a limit, so
            # without a live quote there is nothing to send — check BEFORE
            # cancelling anything, so the position never loses its exits.
            pre = await troute._package_price(client, unit["ticker"], unit["legs"])
            tick0 = tcfg.structure_tick(unit["ticker"], single=unit["single"])
            if not pre or teng.stop_exit_price(pre, unit["entry_type"], tick0) is None:
                state = "needs_attention"
                note = ("stop breached in the extended session with no live quote — market orders "
                        "aren't accepted until 09:30; existing exits left in place")
                return
        for o in closing_orders(orders, syms)[0]:
            try:
                await client.cancel_order(account_id, o.get("id"))
                txn("order cancel requested", account=account_id, order_id=o.get("id"), via="stop guardian")
            except Exception as e:
                log.warning("guardian: cancel of %s failed (may have filled): %s", o.get("id"), e)
        held = {str(p.get("symbol") or "").upper(): p for p in await client.get_positions(account_id)}
        if not syms & set(held):
            state, note = "exited", "closed by its own orders before the guardian had to act"
            return
        k = held_multiple(unit, held)
        if not k:
            state, note = "needs_attention", "position changed while the guardian was exiting — review"
            return
        current = {"qty": k}
        tick = tcfg.structure_tick(unit["ticker"], single=unit["single"])
        px = await troute._package_price(client, unit["ticker"], unit["legs"])
        first = teng.stop_exit_price(px, unit["entry_type"], tick) if px else None
        w = {"entry_order_id": None, "symbol": unit["ticker"], "strategy": "guard",
             "per_user": False, "uid": None, "state": "stop_triggered"}
        txn("stop guardian exiting", account=akey, unit=unit["key"], qty=current["qty"], stop_pct=pct,
            first_price=first)
        if first is None and not tcfg.market_orders_allowed(unit["ticker"]):
            state = "needs_attention"
            note = (f"stop breached but no live quote, and market orders are off for {unit['ticker']} "
                    f"— close it manually")
            return
        if first is None and extended:
            state = "needs_attention"
            note = "stop breached in the extended session and the quote vanished — close it manually"
            return
        if first is None:
            mid, rej, why = await troute._submit_stop_exit(
                client, account_id, symbol=unit["ticker"], strategy="guard", legs=unit["legs"],
                is_single=unit["single"], entry_type=unit["entry_type"], quantity=current["qty"],
                entry_order_id=None, market=True, tag_prefix="torqueStopMkt")
            state = "needs_attention" if rej else "exited"
            note = why if rej else "no live quote — exited at market"
            return
        await troute._run_stop_exit_ladder(
            client, account_id, w, symbol=unit["ticker"], strategy="guard", legs=unit["legs"],
            is_single=unit["single"], entry_type=unit["entry_type"], quantity=current["qty"],
            tick=tick, first_price=first)
        st = w.get("state")
        state = "exited" if st in ("stop_filled", "stop_market_placed") else "needs_attention"
        note = w.get("stop_note") or st
    except Exception as e:
        state, note = "needs_attention", f"guardian exit failed: {e}"
        log.warning("guardian: exit for %s/%s failed: %s", akey, unit["key"], e)
    finally:
        await _persist(akey, unit, state=state, note=note, pct=pct, source=None)
        _ACTIVE_EXITS.pop(ukey, None)
        _BREACH_SINCE.pop(ukey, None)
        _WIDE_TICKS.pop(ukey, None)
        if owned:
            await _close(client)


async def _persist(akey: str, unit: dict, *, state: str, note: str | None, pct: float | None,
                   source: str | None, mark: float | None = None) -> None:
    row = {"account_key": akey, "unit_key": unit["key"], "ticker": unit["ticker"],
           "symbols": unit["symbols"], "state": state, "note": note, "stop_pct": pct,
           "entry_price": unit.get("entry"), "last_mark": mark}
    if source:
        row["source"] = source
    try:
        if not source:
            existing = {r["unit_key"]: r for r in (await supabase_admin.get_guarded_positions(akey) or [])}
            row["source"] = (existing.get(unit["key"]) or {}).get("source") or "outside"
        await supabase_admin.upsert_guarded_position(row)
    except Exception as e:
        log.warning("guardian: could not save state for %s/%s: %s", akey, unit["key"], e)


# ── one pass over one account ───────────────────────────────────────────────
async def guard_account(acct: dict, *, settings_obj, watcher_syms: set[str], now: float,
                        session: str = "regular", ext_tickers: frozenset = frozenset()) -> int:
    akey, client, aid = acct["key"], acct["client"], acct["account_id"]
    positions = await client.get_positions(aid)
    rows = {r["unit_key"]: r for r in (await supabase_admin.get_guarded_positions(akey) or [])}
    orders = await client.get_orders(aid) if positions else []
    units, flagged = build_units(positions, orders)
    live_keys = {u["key"] for u in units} | {f["key"] for f in flagged}
    for gone in set(rows) - live_keys:                 # positions closed since the last pass
        if (akey, gone) not in _ACTIVE_EXITS:
            await supabase_admin.delete_guarded_position(akey, gone)
    if not units and not flagged:
        return 0
    quotes = _quote_map(await client.quotes(sorted({s for u in units for s in u["symbols"]}))) if units else {}
    settings = await _sl_settings(akey)
    reflex = await _reflex_symbols(acct["uids"])
    torque_syms = torque_opened_symbols(orders)
    takeover = float(settings_obj.guardian_takeover_sec)
    for f in flagged:
        if (rows.get(f["key"]) or {}).get("state") != "unconfirmed":
            await supabase_admin.upsert_guarded_position({
                "account_key": akey, "unit_key": f["key"], "ticker": f["ticker"], "symbols": f["symbols"],
                "source": "outside", "state": "unconfirmed", "note": f["note"]})
    from .routes import torque as troute
    tp_saved = _TP_CACHE.get(akey) or {}
    for u in units:
        ukey = (akey, u["key"])
        if ukey in _ACTIVE_EXITS:
            continue
        row = rows.get(u["key"])
        source = unit_source(u, row, reflex, torque_syms)
        px = teng.price_structure(u["legs"], quotes) if quotes else None
        _, has_stop = closing_orders(orders, set(u["symbols"]))
        # Exit values follow the CLOCK for the broker's extended-hours tickers:
        # the extended stop / target inside the extended session, the regular
        # ones in the regular session — whenever the position was opened.
        ext_vals = None
        if u["ticker"] in ext_tickers:
            ext_vals = await troute.ext_exit_pcts(u["ticker"])
            if source != "outside":
                reg = regular_exit_pcts(u, source, settings, tp_saved)
                await reprice_exits(acct["client"], aid, akey, u, orders, now=now,
                                    want=(ext_vals if session == "extended" else reg),
                                    only_from=(None if session == "extended" else ext_vals))
        ext_stop = ext_vals[0] if (ext_vals and session == "extended") else None
        action, note, pct = decide(u, row=row, source=source, settings=settings,
                                   watcher_syms=watcher_syms, px=px, has_stop=has_stop, now=now,
                                   akey=akey, takeover_sec=takeover, ext_stop=ext_stop)
        # Extended-session take-profit: judged on the EXECUTABLE side (what the
        # position could actually be closed for), never the mid and never on a
        # wide book. A resting limit close is left to fill at its own price;
        # with none resting, a GTC limit is placed AT the target price — never
        # the stop ladder, which would cross the spread.
        if session == "extended" and ext_vals and action in ("watching", "broker_stop"):
            tp_on = source != "outside" or bool((tp_saved.get(u["ticker"]) or (False, None))[0])
            if tp_on and px and px.get("complete"):
                tgt = exit_targets(u, *ext_vals)["limit"]["price"]
                tick = tcfg.structure_tick(u["ticker"], single=u["single"])
                exec_px = teng.stop_exit_price(px, u["entry_type"], tick)
                closes = closing_orders(orders, set(u["symbols"]))[0]
                if tp_reached_at(exec_px, tgt, u["entry_type"]) and not any(_is_limit_close(o) for o in closes) \
                        and not (ukey in _TP_FAIL and now - _TP_FAIL[ukey] < _REPRICE_RETRY_SEC):
                    head = f"take-profit {ext_vals[1]:g}% reached in the extended session — "
                    stops = _working_stop_legs(closes)
                    if stops:
                        # Never add a second close next to a resting stop (Tradier
                        # reserves the quantity, or the stop is orphaned). The
                        # position is past its target, so: cancel that stop, confirm
                        # the cancel, THEN place the take-profit limit. (A modify
                        # that changes the order type is silently ignored by the
                        # broker — verified in sandbox — so it isn't used.)
                        order, tag = stops[0]
                        if len({str(o.get("id")) for o, _t in stops}) != 1 or not tag.startswith("torque"):
                            ok, why = False, "a stop order Torque didn't place is resting — not touched"
                        else:
                            ok, why = await _replace_stop_with_take_profit(acct["client"], aid, akey, u, order, tgt)
                        msg = f"resting stop cancelled, limit close placed at {tgt}" if ok else f"not closed: {why}"
                    else:
                        ok, why = await _place_take_profit(acct["client"], aid, akey, u, tgt)
                        msg = f"limit close placed at {tgt}" if ok else f"limit close refused: {why}"
                    note = head + msg
                    if ok:
                        _TP_FAIL.pop(ukey, None)
                    else:
                        _TP_FAIL[ukey] = now
                        action = "needs_attention"
        if action == "past_stop_alert" and (row or {}).get("state") != "past_stop_alert":
            txn("stop guardian alert", account=akey, unit=u["key"], note=note)
        if action == "exit" and session == "extended" and u["ticker"] not in ext_tickers:
            # Its options don't trade until 09:30: an exit would be refused,
            # and the quotes it breached on are stale. Keep every existing
            # broker order (bracket stop/target) and re-check at the open.
            action, note = "outside_session", (
                f"stop {pct:g}% breached on an extended-session quote, but {u['ticker']} options "
                f"don't trade until 09:30 — exits left in place, re-checked at the open")
        if action == "exit":
            exit_client, owned = acct["client"], False
            if acct["owned"]:
                from .broker_resolver import resolve_client_for_uid
                r = await resolve_client_for_uid(acct["uids"][0])
                if not r:
                    continue
                exit_client, owned = r[1], True
            await _persist(akey, u, state="exiting", note=note, pct=pct,
                           source=source, mark=(px or {}).get("abs_mid"))
            _ACTIVE_EXITS[ukey] = asyncio.create_task(
                _exit_unit(akey, exit_client, aid, u, orders, pct, owned,
                           extended=(session == "extended")))
            continue
        state = action
        if row is None or row.get("state") != state or row.get("note") != note \
                or row.get("source") != source:
            await _persist(akey, u, state=state, note=note, pct=pct,
                           source=source, mark=(px or {}).get("abs_mid"))
    return len(units)


# ── the loop ────────────────────────────────────────────────────────────────
async def run_once(app) -> None:
    from .config import get_settings
    from .routes import torque as troute
    s = get_settings()
    house = getattr(app.state, "tradier", None)
    session = "regular"
    if not (s.devmode or s.tradier_env == "sandbox"):
        # Torque's trading window (regular or extended session). Every guarded
        # position is watched in both; in the extended session an exit is only
        # sent where the broker can fill it (see guard_account / _exit_unit).
        try:
            _st, _open, session = await troute._torque_session(house)
        except Exception:
            session = None
        if session not in ("regular", "extended"):
            STATUS.update(skipped_closed=True, last_run=time.time())
            return
    ext_tickers = (await troute.torque_hours())[1]
    if not tcfg.ticker_setup_loaded():
        return
    sandbox_only = bool(s.devmode or s.tradier_env == "sandbox")
    lease = "guardian:sandbox" if sandbox_only else "guardian:live"
    ttl = max(30.0, 3 * float(s.guardian_interval_sec))
    if not await supabase_admin.acquire_guardian_lease(lease, LEASE_HOLDER, ttl):
        if STATUS.get("lease") != "standby":
            txn("stop guardian standing by", lease=lease, holder=LEASE_HOLDER,
                reason="another backend holds the guardian lease (or Supabase is unreachable)")
        STATUS.update(lease="standby", last_run=time.time())
        return
    if STATUS.get("lease") != "held":
        txn("stop guardian lease taken", lease=lease, holder=LEASE_HOLDER)
    STATUS["lease"] = "held"
    accounts = await _accounts(app, sandbox_only=sandbox_only)
    watcher_syms = await _watcher_symbols()
    total = 0
    now = time.time()
    try:
        for acct in accounts:
            try:
                total += await guard_account(acct, settings_obj=s, watcher_syms=watcher_syms, now=now,
                                              session=session, ext_tickers=ext_tickers)
            except Exception as e:
                log.warning("guardian: pass over %s failed: %s", acct["key"], e)
    finally:
        for acct in accounts:
            if acct["owned"]:
                await _close(acct["client"])
    STATUS.update(last_run=time.time(), accounts=len(accounts), units=total, skipped_closed=False)


async def guardian_loop(app) -> None:
    from .config import get_settings
    interval = max(3.0, float(get_settings().guardian_interval_sec))
    while True:
        await asyncio.sleep(interval)
        try:
            await run_once(app)
        except Exception as e:                     # never let the loop die
            log.warning("guardian: pass failed: %s", e)
