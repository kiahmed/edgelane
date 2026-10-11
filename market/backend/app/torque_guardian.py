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


# ── per-account inputs ──────────────────────────────────────────────────────
async def _sl_settings(key: str) -> dict[str, tuple[bool, float]] | None:
    """ticker -> (enabled, pct) for the account; last good copy on a failed
    read; None if it has never loaded (non-Reflex units are then skipped)."""
    rows = await supabase_admin.get_sl_settings(key)
    if rows is not None:
        _SETTINGS_CACHE[key] = {
            str(r.get("ticker") or "").upper(): (
                bool(r.get("enabled")),
                float(r["pct"]) if r.get("pct") is not None else default_stop_pct(str(r.get("ticker") or "")))
            for r in rows}
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
           akey: str, takeover_sec: float) -> tuple[str, str | None, float | None]:
    """(action, note, stop_pct) for one unit. Actions:
    'watcher_managed' | 'sl_off' | 'no_settings' | 'no_quote' | 'past_stop_alert'
    | 'watching' | 'broker_stop' | 'wide_book' | 'exit'.

    Stop-loss by source: Reflex → always on at the ticker default; Torque →
    the page's saved per-ticker setting, on at the default when none is
    saved; outside (Tradier's site, tp CLI, …) → only when stop-loss has been
    switched ON for that ticker on the Torque page (no saved setting = off)."""
    ukey = (akey, unit["key"])
    if set(unit["symbols"]) & watcher_syms:
        return "watcher_managed", "managed by Torque's own watcher", None
    t = unit["ticker"]
    if source == "reflex":
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
                     pct: float, owned: bool) -> None:
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
async def guard_account(acct: dict, *, settings_obj, watcher_syms: set[str], now: float) -> int:
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
    for u in units:
        ukey = (akey, u["key"])
        if ukey in _ACTIVE_EXITS:
            continue
        row = rows.get(u["key"])
        source = unit_source(u, row, reflex, torque_syms)
        px = teng.price_structure(u["legs"], quotes) if quotes else None
        _, has_stop = closing_orders(orders, set(u["symbols"]))
        action, note, pct = decide(u, row=row, source=source, settings=settings,
                                   watcher_syms=watcher_syms, px=px, has_stop=has_stop, now=now,
                                   akey=akey, takeover_sec=takeover)
        if action == "past_stop_alert" and (row or {}).get("state") != "past_stop_alert":
            txn("stop guardian alert", account=akey, unit=u["key"], note=note)
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
                _exit_unit(akey, exit_client, aid, u, orders, pct, owned))
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
    if not (s.devmode or s.tradier_env == "sandbox"):
        try:
            _st, is_open = await troute._tradier_market_state(house)
        except Exception:
            is_open = None
        if is_open is not True:
            STATUS.update(skipped_closed=True, last_run=time.time())
            return
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
                total += await guard_account(acct, settings_obj=s, watcher_syms=watcher_syms, now=now)
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
