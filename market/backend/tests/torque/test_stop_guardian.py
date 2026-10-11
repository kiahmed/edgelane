"""Torque stop guardian: stop-loss at the POSITION level, whoever placed it.

Rules under test (as specified):
  * Reflex positions are ALWAYS protected at the ticker default (or 30%),
    whatever the page's stop-loss setting says;
  * every other position in a Torque ticker — Torque's own, Tradier's site,
    the tp CLI — follows the page's saved per-ticker setting (no row = on);
  * a broker-held stop is left to work, but if the price is past the stop and
    it hasn't filled within the takeover window, the guardian takes over;
  * a position already past its stop when first seen is alerted, never
    auto-closed (it re-arms once back above the stop);
  * multi-leg positions are grouped only when today's order history confirms
    them; otherwise flagged and left alone;
  * positions Torque's own watcher manages are never touched.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import torque_guardian as g
from app.routes import torque as troute

from .conftest import FakeRequest, FakeTradier

SPY_C = "SPY261012C00700000"
SPY_P = "SPY261012P00690000"
AKEY = "live:ACC"


def pos(sym, qty, cost):
    return {"symbol": sym, "quantity": qty, "cost_basis": cost}


class Book(FakeTradier):
    """Positions, orders and quotes the test controls; cancels are recorded and
    can make positions disappear (a stop leg filling as we cancel)."""
    def __init__(self, positions, quotes, orders=None):
        super().__init__(positions=positions, orders=orders or [])
        self.q = dict(quotes)
        self.cancels = []
        self.after_cancel_positions = None

    async def quotes(self, symbols, greeks=False):
        syms = symbols if isinstance(symbols, (list, tuple)) else str(symbols).split(",")
        return [{"symbol": s, "bid": self.q[s][0], "ask": self.q[s][1]} for s in syms if s in self.q]

    async def cancel_order(self, account_id, order_id):
        self.cancels.append(str(order_id))
        if self.after_cancel_positions is not None:
            self._positions = list(self.after_cancel_positions)
        return {"order": {"id": order_id, "status": "ok"}}


@pytest.fixture
def db(monkeypatch):
    """In-memory stand-ins for the guardian's Supabase reads/writes."""
    st = type("DB", (), {})()
    st.rows, st.settings, st.reflex, st.watchers, st.ladders = {}, [], [], [], []

    async def _get_rows(akey):
        return [dict(r) for r in st.rows.values()]

    async def _upsert(row):
        st.rows[row["unit_key"]] = {**st.rows.get(row["unit_key"], {}), **row}
        return True

    async def _delete(akey, ukey):
        st.rows.pop(ukey, None)
        return True

    async def _settings(akey):
        return list(st.settings)

    async def _reflex(uid):
        return list(st.reflex)

    async def _select(table, **kw):
        return list(st.watchers) if table == "torque_watchers" else []
    for name, fn in (("get_guarded_positions", _get_rows), ("upsert_guarded_position", _upsert),
                     ("delete_guarded_position", _delete), ("get_sl_settings", _settings),
                     ("get_open_reflex_orders", _reflex), ("select_many", _select)):
        monkeypatch.setattr(g.supabase_admin, name, fn)

    async def _ladder(client, account_id, w, **kw):
        st.ladders.append(kw)
        w["state"] = "stop_filled"
    monkeypatch.setattr(troute, "_run_stop_exit_ladder", _ladder)
    for d in (g._ACTIVE_EXITS, g._BREACH_SINCE, g._WIDE_TICKS, g._SETTINGS_CACHE):
        d.clear()
    yield st
    for d in (g._ACTIVE_EXITS, g._BREACH_SINCE, g._WIDE_TICKS, g._SETTINGS_CACHE):
        d.clear()


@pytest.fixture
def cfg(monkeypatch):
    s = troute.get_settings().model_copy(update={"guardian_takeover_sec": 10.0})
    return s


async def _pass(book, cfg, *, uids=("u1",), now=None, watcher_syms=frozenset()):
    acct = {"key": AKEY, "client": book, "account_id": "ACC", "uids": list(uids), "owned": False}
    n = await g.guard_account(acct, settings_obj=cfg, watcher_syms=set(watcher_syms),
                              now=time.time() if now is None else now)
    for t in list(g._ACTIVE_EXITS.values()):
        await t
    return n


# ── grouping ─────────────────────────────────────────────────────────────────
def test_a_lone_option_is_a_single_unit_with_its_cost_basis_entry():
    units, flagged = g.build_units([pos(SPY_C, 2, 500.0)], [])
    assert flagged == [] and len(units) == 1
    u = units[0]
    assert u["single"] and u["qty"] == 2 and u["entry"] == 2.5 and u["entry_type"] == "debit"
    assert u["legs"] == [{"symbol": SPY_C, "action": "buy_to_open", "quantity": 1}]


def test_a_short_option_is_a_credit_unit():
    u = g.build_units([pos(SPY_P, -1, -180.0)], [])[0][0]
    assert u["entry_type"] == "credit" and u["entry"] == 1.8
    assert u["legs"][0]["action"] == "sell_to_open"


def test_two_legs_confirmed_by_todays_multileg_order_form_one_spread():
    order = {"class": "multileg", "status": "filled", "leg": [
        {"option_symbol": SPY_C, "side": "buy_to_open", "quantity": 2},
        {"option_symbol": SPY_P, "side": "sell_to_open", "quantity": 2}]}
    units, flagged = g.build_units([pos(SPY_C, 2, 600.0), pos(SPY_P, -2, -200.0)], [order])
    assert flagged == [] and len(units) == 1
    u = units[0]
    assert not u["single"] and u["qty"] == 2 and u["entry"] == 2.0 and u["entry_type"] == "debit"


def test_unconfirmed_multi_leg_positions_are_flagged_and_left_alone():
    units, flagged = g.build_units([pos(SPY_C, 1, 300.0), pos(SPY_P, -1, -100.0)], [])
    assert units == [] and {f["key"] for f in flagged} == {SPY_C, SPY_P}


def test_a_leg_confirmed_by_its_own_opening_order_is_guarded_singly():
    order = {"class": "option", "status": "filled", "option_symbol": SPY_C, "side": "buy_to_open",
             "quantity": 1}
    units, flagged = g.build_units([pos(SPY_C, 1, 300.0), pos(SPY_P, -1, -100.0)], [order])
    assert [u["key"] for u in units] == [SPY_C] and [f["key"] for f in flagged] == [SPY_P]


def test_adding_to_a_prior_day_spread_leg_never_makes_it_a_single():
    # Review blocker: bull put spread from an earlier day (long 1 P_low, short
    # 1 P_high), then today 1 more P_low bought as a single. Treating all 2
    # P_low as a single would sell both and leave the short P_high naked.
    p_low, p_high = "SPY261012P00680000", "SPY261012P00690000"
    today = {"class": "option", "status": "filled", "option_symbol": p_low, "side": "buy_to_open",
             "quantity": 1, "exec_quantity": 1}
    units, flagged = g.build_units([pos(p_low, 2, 400.0), pos(p_high, -1, -300.0)], [today])
    assert units == [] and {f["key"] for f in flagged} == {p_low, p_high}


def test_single_confirmation_checks_side_too():
    sold = {"class": "option", "status": "filled", "option_symbol": SPY_C, "side": "sell_to_open",
            "quantity": 1}
    units, _ = g.build_units([pos(SPY_C, 1, 300.0), pos(SPY_P, -1, -100.0)], [sold])
    assert units == []


def test_a_lone_position_on_its_ticker_is_still_guarded_without_history():
    units, flagged = g.build_units([pos(SPY_C, 3, 900.0)], [])
    assert [u["key"] for u in units] == [SPY_C] and flagged == []


# ── who opened it: Reflex / Torque / outside ────────────────────────────────
def test_torque_tagged_fills_mark_their_contracts_as_torque():
    orders = [
        {"class": "option", "status": "filled", "tag": "torqueLongCall", "option_symbol": SPY_C,
         "side": "buy_to_open"},
        # a bracket: parent still "open", the entry leg filled
        {"class": "oto", "status": "open", "tag": "torqueLongPut", "leg": [
            {"option_symbol": SPY_P, "side": "buy_to_open", "status": "filled"},
            {"option_symbol": SPY_P, "side": "sell_to_close", "status": "open"}]},
        # Torque's exits and other people's orders don't count
        {"class": "option", "status": "filled", "tag": "torqueClose12345", "option_symbol": "X1",
         "side": "sell_to_close"},
        {"class": "option", "status": "filled", "option_symbol": "X2", "side": "buy_to_open"},
        {"class": "option", "status": "rejected", "tag": "torqueLongCall", "option_symbol": "X3",
         "side": "buy_to_open"},
    ]
    assert g.torque_opened_symbols(orders) == {SPY_C, SPY_P}


def test_source_prefers_the_strongest_and_keeps_a_saved_one(unit):
    assert g.unit_source(unit, None, set(), set()) == "outside"
    assert g.unit_source(unit, None, set(), {SPY_C}) == "torque"
    assert g.unit_source(unit, None, {SPY_C}, {SPY_C}) == "reflex"
    # today's orders have rolled off, but the saved source sticks
    assert g.unit_source(unit, {"source": "torque"}, set(), set()) == "torque"
    assert g.unit_source(unit, {"source": "outside"}, set(), {SPY_C}) == "torque"


def test_outside_position_is_not_guarded_until_stop_loss_is_switched_on(unit):
    act, note, _ = _decide(unit, mark=1.0, row={"state": "watching"}, source="outside", settings={})
    assert act == "sl_off" and "outside Torque" in note
    assert _decide(unit, mark=2.0, row={"state": "watching"}, source="outside",
                   settings={"SPY": (True, 30.0)})[0] == "exit"
    assert _decide(unit, mark=1.0, row={"state": "watching"}, source="outside",
                   settings={"SPY": (False, 30.0)})[0] == "sl_off"


def test_torque_position_with_no_saved_setting_is_on_at_default(unit):
    assert _decide(unit, mark=2.0, row={"state": "watching"}, source="torque", settings={})[0] == "exit"


def test_positions_outside_torque_tickers_are_ignored():
    units, flagged = g.build_units([pos("AAPL261012C00200000", 1, 300.0), pos("SPY", 100, 70000)], [])
    assert units == [] and flagged == []


def test_root_variants_map_to_their_torque_ticker():
    assert g.ticker_of("SPXW261012C07500000") == "SPX"
    assert g.ticker_of("NDXP261012P25000000") == "NDX"
    assert g.ticker_of("DJXW261012C00500000") == "DJX"
    assert g.ticker_of("AAPL261012C00200000") is None


def test_held_multiple_never_guesses_a_size():
    u = g.build_units([pos(SPY_C, 2, 600.0)], [])[0][0]
    assert g.held_multiple(u, {SPY_C: pos(SPY_C, 2, 600.0)}) == 2
    assert g.held_multiple(u, {}) is None
    assert g.held_multiple(u, {SPY_C: pos(SPY_C, -2, -600.0)}) is None      # flipped side


# ── the stop decision ────────────────────────────────────────────────────────
def _decide(unit, *, mark=2.0, row=None, reflex=False, source="torque", settings=None, has_stop=False, now=1000.0,
            watcher=frozenset(), spread=0.04):
    px = {"complete": True, "abs_mid": mark, "net_bid": mark - spread / 2, "net_ask": mark + spread / 2}
    return g.decide(unit, row=row, source="reflex" if reflex else source, settings={} if settings is None else settings,
                    watcher_syms=set(watcher), px=px, has_stop=has_stop, now=now, akey=AKEY,
                    takeover_sec=10.0)


@pytest.fixture
def unit():
    for d in (g._BREACH_SINCE, g._WIDE_TICKS):
        d.clear()
    return g.build_units([pos(SPY_C, 1, 300.0)], [])[0][0]          # entry 3.00, 30% stop → 2.10


def test_torque_position_uses_the_saved_setting_default_on(unit):
    assert _decide(unit, mark=2.5, row={"state": "watching"})[0] == "watching"
    assert _decide(unit, mark=2.0, row={"state": "watching"})[0] == "exit"


def test_setting_turned_off_means_no_stop_for_non_reflex(unit):
    act, note, _ = _decide(unit, mark=1.0, row={"state": "watching"}, settings={"SPY": (False, 30.0)})
    assert act == "sl_off" and "turned off" in note


def test_reflex_is_always_protected_even_with_the_setting_off(unit):
    act, _, pct = _decide(unit, mark=2.0, row={"state": "watching"}, reflex=True,
                          settings={"SPY": (False, 50.0)})
    assert act == "exit" and pct == 30.0


def test_saved_pct_is_honoured(unit):
    assert _decide(unit, mark=2.0, row={"state": "watching"}, settings={"SPY": (True, 40.0)})[0] == "watching"
    assert _decide(unit, mark=1.75, row={"state": "watching"}, settings={"SPY": (True, 40.0)})[0] == "exit"


def test_already_past_its_stop_when_first_seen_is_alert_only(unit):
    act, note, _ = _decide(unit, mark=1.0, row=None)
    assert act == "past_stop_alert" and "not closed" in note
    assert _decide(unit, mark=1.0, row={"state": "past_stop_alert", "note": note})[0] == "past_stop_alert"
    assert _decide(unit, mark=2.6, row={"state": "past_stop_alert"})[0] == "watching"     # re-armed


def test_a_live_broker_stop_gets_the_takeover_window_first(unit):
    assert _decide(unit, mark=2.0, row={"state": "watching"}, has_stop=True, now=1000.0)[0] == "broker_stop"
    assert _decide(unit, mark=2.0, row={"state": "watching"}, has_stop=True, now=1005.0)[0] == "broker_stop"
    assert _decide(unit, mark=2.0, row={"state": "watching"}, has_stop=True, now=1011.0)[0] == "exit"


def test_recovering_clears_the_takeover_timer(unit):
    _decide(unit, mark=2.0, row={"state": "watching"}, has_stop=True, now=1000.0)
    _decide(unit, mark=2.5, row={"state": "watching"}, has_stop=True, now=1005.0)
    assert _decide(unit, mark=2.0, row={"state": "watching"}, has_stop=True, now=1011.0)[0] == "broker_stop"


def test_wide_book_waits_one_check_then_exits(unit):
    assert _decide(unit, mark=2.0, row={"state": "watching"}, spread=1.6)[0] == "wide_book"
    assert _decide(unit, mark=2.0, row={"state": "watching"}, spread=1.6)[0] == "exit"


def test_watcher_managed_positions_are_never_touched(unit):
    assert _decide(unit, mark=1.0, row={"state": "watching"}, watcher={SPY_C})[0] == "watcher_managed"


def test_without_settings_loaded_only_reflex_is_acted_on(unit):
    act = g.decide(unit, row={"state": "watching"}, source="torque", settings=None, watcher_syms=set(),
                   px={"complete": True, "abs_mid": 1.0, "net_bid": 0.98, "net_ask": 1.02},
                   has_stop=False, now=0.0, akey=AKEY, takeover_sec=10.0)[0]
    assert act == "no_settings"


# ── one full pass: exits, cancels, persistence ───────────────────────────────
async def test_outside_position_past_its_stop_is_closed_after_cancelling_its_orders(db, cfg):
    db.rows[SPY_C] = {"unit_key": SPY_C, "state": "watching", "source": "torque"}
    tp = {"id": 77, "class": "option", "status": "open", "option_symbol": SPY_C,
          "side": "sell_to_close", "type": "limit"}
    book = Book([pos(SPY_C, 1, 300.0)], {SPY_C: (1.98, 2.02)}, orders=[tp])
    await _pass(book, cfg)
    assert book.cancels == ["77"]                   # resting profit target cleared first
    (lad,) = db.ladders
    assert lad["quantity"] == 1 and lad["is_single"] and lad["entry_type"] == "debit"
    assert db.rows[SPY_C]["state"] == "exited"


async def test_bracket_whose_stop_didnt_fill_is_taken_over(db, cfg):
    db.rows[SPY_C] = {"unit_key": SPY_C, "state": "broker_stop", "source": "torque"}
    bracket = {"id": 55, "class": "otoco", "status": "open", "leg": [
        {"option_symbol": SPY_C, "side": "buy_to_open", "type": "limit", "status": "filled", "exec_quantity": 1},
        {"option_symbol": SPY_C, "side": "sell_to_close", "type": "limit", "status": "open"},
        {"option_symbol": SPY_C, "side": "sell_to_close", "type": "stop_limit", "status": "open"}]}
    book = Book([pos(SPY_C, 1, 300.0)], {SPY_C: (1.98, 2.02)}, orders=[bracket])
    t0 = time.time()
    await _pass(book, cfg, now=t0)
    assert db.ladders == [] and book.cancels == []          # inside the takeover window
    await _pass(book, cfg, now=t0 + 11)
    assert book.cancels == ["55"] and len(db.ladders) == 1


async def test_position_that_closed_on_its_own_during_cancel_is_not_exited(db, cfg):
    db.rows[SPY_C] = {"unit_key": SPY_C, "state": "watching", "source": "torque"}
    tp = {"id": 77, "class": "option", "status": "open", "option_symbol": SPY_C,
          "side": "sell_to_close", "type": "limit"}
    book = Book([pos(SPY_C, 1, 300.0)], {SPY_C: (1.98, 2.02)}, orders=[tp])
    book.after_cancel_positions = []                 # the TP filled as we cancelled it
    await _pass(book, cfg)
    assert db.ladders == [] and db.rows[SPY_C]["state"] == "exited"


async def test_setting_off_means_the_position_is_left_alone(db, cfg):
    db.rows[SPY_C] = {"unit_key": SPY_C, "state": "watching", "source": "torque"}
    db.settings = [{"ticker": "SPY", "enabled": False, "pct": 30}]
    book = Book([pos(SPY_C, 1, 300.0)], {SPY_C: (0.98, 1.02)})
    await _pass(book, cfg)
    assert db.ladders == [] and db.rows[SPY_C]["state"] == "sl_off"


async def test_reflex_position_is_exited_even_with_the_setting_off(db, cfg):
    db.rows[SPY_C] = {"unit_key": SPY_C, "state": "watching", "source": "torque"}
    db.settings = [{"ticker": "SPY", "enabled": False, "pct": 30}]
    db.reflex = [{"entry_order_id": "9", "status": "filled", "legs": [{"symbol": SPY_C}]}]
    book = Book([pos(SPY_C, 1, 300.0)], {SPY_C: (1.98, 2.02)})
    await _pass(book, cfg)
    assert len(db.ladders) == 1 and db.rows[SPY_C]["source"] == "reflex"


async def test_first_sight_past_stop_is_alerted_and_persisted_not_closed(db, cfg):
    db.settings = [{"ticker": "SPY", "enabled": True, "pct": 30}]
    book = Book([pos(SPY_C, 1, 300.0)], {SPY_C: (0.98, 1.02)})
    await _pass(book, cfg)
    assert db.ladders == [] and db.rows[SPY_C]["state"] == "past_stop_alert"
    await _pass(book, cfg)                           # still past → still alert only
    assert db.ladders == []


async def test_closed_positions_drop_their_guard_rows(db, cfg):
    db.rows[SPY_C] = {"unit_key": SPY_C, "state": "watching", "source": "torque"}
    await _pass(Book([], {}), cfg)
    assert db.rows == {}


async def test_watcher_managed_position_is_never_exited(db, cfg):
    db.rows[SPY_C] = {"unit_key": SPY_C, "state": "watching", "source": "torque"}
    book = Book([pos(SPY_C, 1, 300.0)], {SPY_C: (0.98, 1.02)})
    await _pass(book, cfg, watcher_syms={SPY_C})
    assert db.ladders == [] and db.rows[SPY_C]["state"] == "watcher_managed"


async def test_run_once_skips_while_the_market_is_closed(monkeypatch):
    s = troute.get_settings().model_copy(update={"devmode": False, "tradier_env": "production"})
    monkeypatch.setattr("app.config.get_settings", lambda: s)

    async def _closed(client):
        return "closed", False
    monkeypatch.setattr(troute, "_tradier_market_state", _closed)
    called = []

    async def _accts(app, **kw):
        called.append(1)
        return []
    monkeypatch.setattr(g, "_accounts", _accts)
    app = type("A", (), {"state": type("S", (), {"tradier": FakeTradier()})()})()
    await g.run_once(app)
    assert called == [] and g.STATUS["skipped_closed"] is True


async def test_outside_position_with_no_saved_setting_is_left_alone(db, cfg):
    book = Book([pos(SPY_C, 1, 300.0)], {SPY_C: (1.98, 2.02)})         # past a 30% stop
    db.rows[SPY_C] = {"unit_key": SPY_C, "state": "watching", "source": "outside"}
    await _pass(book, cfg)
    assert db.ladders == [] and db.rows[SPY_C]["state"] == "sl_off"
    assert db.rows[SPY_C]["source"] == "outside"


async def test_torque_tagged_position_is_guarded_at_the_default_with_no_setting(db, cfg):
    order = {"id": 7, "class": "option", "status": "filled", "tag": "torqueLongCall",
             "option_symbol": SPY_C, "side": "buy_to_open", "quantity": 1}
    db.rows[SPY_C] = {"unit_key": SPY_C, "state": "watching", "source": "outside"}
    book = Book([pos(SPY_C, 1, 300.0)], {SPY_C: (1.98, 2.02)}, orders=[order])
    await _pass(book, cfg)
    assert len(db.ladders) == 1 and db.rows[SPY_C]["source"] == "torque"


async def test_run_once_stands_by_without_the_lease(monkeypatch):
    s = troute.get_settings().model_copy(update={"devmode": True})
    monkeypatch.setattr("app.config.get_settings", lambda: s)
    monkeypatch.setattr(g.tcfg, "ticker_setup_loaded", lambda: True)
    leases = []

    async def _lease(name, holder, ttl):
        leases.append(name)
        return False
    monkeypatch.setattr(g.supabase_admin, "acquire_guardian_lease", _lease)
    called = []

    async def _accts(app, **kw):
        called.append(1)
        return []
    monkeypatch.setattr(g, "_accounts", _accts)
    app = type("A", (), {"state": type("S", (), {"tradier": FakeTradier()})()})()
    await g.run_once(app)
    assert leases == ["guardian:sandbox"] and called == [] and g.STATUS["lease"] == "standby"


async def test_devmode_guards_sandbox_accounts_only(monkeypatch):
    class C(FakeTradier):
        def __init__(self, url):
            super().__init__()
            self.base_url = url
            self.closed = False

        async def close(self):
            self.closed = True
    live, sbx = C("https://api.tradier.com/v1"), C("https://sandbox.tradier.com/v1")
    clients = {"u-live": (live, "L1"), "u-sbx": (sbx, "S1")}

    async def _users(tool):
        return list(clients)

    async def _resolve(uid):
        c, aid = clients[uid]
        return "tradier", c, aid
    monkeypatch.setattr(g.supabase_admin, "get_users_with_tool", _users)
    monkeypatch.setattr("app.broker_resolver.resolve_client_for_uid", _resolve)
    app = type("A", (), {"state": type("S", (), {"tradier": None})()})()
    got = await g._accounts(app, sandbox_only=True)
    assert [a["key"] for a in got] == ["sandbox:S1"] and live.closed
    assert {a["key"] for a in await g._accounts(app)} == {"sandbox:S1", "live:L1"}


# ── saved stop-loss setting (the page's toggle) ──────────────────────────────
DEV = {"id": "dev-local", "email": "dev@local", "auth": "dev"}


async def test_unsaved_setting_defaults_on_at_the_ticker_default(monkeypatch):
    r = await troute.torque_sl_setting_get("DJX", FakeRequest(FakeTradier()), account_id="T", user=DEV)
    assert r == {"symbol": "DJX", "enabled": True, "pct": 50.0, "saved": False}


async def test_setting_is_saved_and_read_back_per_account_and_ticker(monkeypatch):
    saved = {}

    async def _upsert(row):
        saved[(row["account_key"], row["ticker"])] = row
        return True

    async def _get(akey):
        return [r for (k, _t), r in saved.items() if k == akey]
    monkeypatch.setattr(troute.supabase_admin, "upsert_sl_setting", _upsert)
    monkeypatch.setattr(troute.supabase_admin, "get_sl_settings", _get)
    req = troute.SLSettingRequest(symbol="spy", enabled=False, pct=25, account_id="T")
    await troute.torque_sl_setting_set(req, FakeRequest(FakeTradier()), user=DEV)
    r = await troute.torque_sl_setting_get("SPY", FakeRequest(FakeTradier()), account_id="T", user=DEV)
    assert r == {"symbol": "SPY", "enabled": False, "pct": 25.0, "saved": True}


async def test_setting_for_an_unconfigured_ticker_is_refused():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        await troute.torque_sl_setting_set(troute.SLSettingRequest(symbol="ZZZZ", enabled=True, pct=30),
                                           FakeRequest(FakeTradier()), user=DEV)
    assert e.value.status_code == 400


# ── bracket status labels ────────────────────────────────────────────────────
def _br(entry="filled", tp="open", sl="open", exec_=1):
    return {"class": "otoco", "status": "open", "leg": [
        {"option_symbol": SPY_C, "type": "limit", "status": entry, "exec_quantity": exec_ if entry == "filled" else 0},
        {"option_symbol": SPY_C, "type": "limit", "status": tp},
        {"option_symbol": SPY_C, "type": "stop_limit", "status": sl}]}


@pytest.mark.parametrize("order,by_sym,settings,label", [
    (_br(entry="pending"), {}, {}, "pending"),
    (_br(), {}, {}, "filled · TP + SL live"),
    (_br(sl="canceled"), {}, {}, "filled · TP live · SL by guardian"),
    (_br(tp="canceled"), {}, {}, "filled · SL live"),
    (_br(tp="canceled", sl="expired"), {}, {}, "filled · SL by guardian"),
    (_br(tp="canceled", sl="canceled"), {}, {"SPY": False}, "filled · no SL (turned off)"),
    (_br(tp="canceled", sl="canceled"), {SPY_C: {"state": "past_stop_alert"}}, {}, "filled · past stop — alert only"),
    (_br(tp="filled", sl="canceled"), {}, {}, "closed · TP"),
    (_br(tp="canceled", sl="filled"), {}, {}, "closed · SL"),
])
def test_bracket_label_reflects_what_is_actually_live(order, by_sym, settings, label):
    assert troute._bracket_label(order, by_sym, settings) == label


def test_non_brackets_have_no_label_and_signal_codes_read_the_entry_leg():
    assert troute._bracket_label({"class": "option", "status": "open"}, {}, {}) is None
    assert troute._signal_status_code(_br()) == "filled"
    assert troute._signal_status_code(_br(entry="pending")) == "pending"
    assert troute._signal_status_code(_br(tp="filled")) == "closed_tp"
    assert troute._signal_status_code({"class": "option", "status": "canceled"}) == "canceled"


async def test_orders_panel_returns_bracket_labels_and_the_guardian_view(monkeypatch):
    async def _rows(akey):
        return [{"unit_key": SPY_C, "ticker": "SPY", "symbols": [SPY_C], "source": "reflex",
                 "state": "watching", "note": None, "stop_pct": 30}]
    monkeypatch.setattr(troute.supabase_admin, "get_guarded_positions", _rows)
    client = FakeTradier(orders=[{**_br(sl="canceled"), "id": 5}])
    r = await troute.torque_orders(FakeRequest(client), account_id="T", user=DEV)
    assert r["orders"][0]["bracket_status"] == "filled · TP live · SL by guardian"
    assert r["guardian"] == [{"unit_key": SPY_C, "ticker": "SPY", "symbols": [SPY_C], "source": "reflex",
                              "state": "watching", "note": None, "stop_pct": 30}]


async def test_guardian_no_quote_exit_never_goes_to_market_where_market_orders_are_off(db, monkeypatch):
    monkeypatch.setattr(g.tcfg, "market_orders_allowed", lambda t: False)

    async def _price(client, symbol, legs):
        return None
    monkeypatch.setattr(troute, "_package_price", _price)
    book = Book([pos(SPY_C, 1, 300.0)], {})
    u = g.build_units(book._positions, [])[0][0]
    await g._exit_unit(AKEY, book, "ACC", u, [], 30.0, False)
    assert book.placed == [] and db.rows[SPY_C]["state"] == "needs_attention"
    assert "market orders are off" in db.rows[SPY_C]["note"]
