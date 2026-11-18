"""Extended-hours exits (stop/target for entries placed outside 09:30-16:00)
and the one-trade-per-ticker-per-direction rule."""
import pytest
from fastapi import HTTPException

from app import torque_config as tcfg
from app import torque_guardian as g
from app.routes import torque as troute
from app.routes.torque import PlaceRequest, torque_place
from tests.torque.conftest import FakeRequest, FakeTradier

DEV = {"id": "dev-local", "email": "dev@local", "auth": "dev"}
SPY_C = "SPY261012C00700000"
SPY_C2 = "SPY261012C00710000"
SPY_P = "SPY261012P00690000"


# ── direction ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("legs,expected", [
    ([(SPY_C, "buy")], "bull"), ([(SPY_C, "sell")], "bear"),
    ([(SPY_P, "buy")], "bear"), ([(SPY_P, "sell")], "bull"),
    ([(SPY_C, "buy"), (SPY_C2, "sell")], "bull"),          # bull call
    ([(SPY_C, "sell"), (SPY_C2, "buy")], "bear"),          # bear call
    ([("SPY261012P00700000", "buy"), ("SPY261012P00690000", "sell")], "bear"),   # bear put
    ([("SPY261012P00700000", "sell"), ("SPY261012P00690000", "buy")], "bull"),   # bull put
    ([(SPY_C, "buy"), (SPY_P, "buy")], None),              # strangle / condor-ish: neutral
])
def test_structure_dir(legs, expected):
    assert g.structure_dir([{"symbol": s, "action": a + "_to_open"} for s, a in legs]) == expected


def test_order_entry_dir_from_tag_and_legs():
    working = {"status": "open", "class": "option", "option_symbol": SPY_P, "side": "buy_to_open",
               "tag": "torqueNewslong_put"}
    assert g.order_entry_dir(working) == "bear"
    assert g.order_entry_dir({**working, "tag": ""}) == "bear"
    assert g.order_entry_dir({**working, "status": "filled"}) is None
    assert g.order_entry_dir({**working, "side": "sell_to_close"}) is None   # an exit, not an entry


class _Acct(FakeTradier):
    def __init__(self, positions=(), orders=(), fail=False):
        super().__init__()
        self._pos, self._ord, self._fail = list(positions), list(orders), fail

    async def get_positions(self, account_id):
        if self._fail:
            raise RuntimeError("broker down")
        return self._pos

    async def get_orders(self, account_id):
        return self._ord


async def test_same_direction_open_position_blocks():
    acct = _Acct(positions=[{"symbol": SPY_C, "quantity": 1, "cost_basis": 200}])
    assert "bullish" in await troute._same_direction_block(acct, "A", "SPY", "long_call")
    assert "bullish" in await troute._same_direction_block(acct, "A", "SPY", "bull_put")
    assert await troute._same_direction_block(acct, "A", "SPY", "long_put") is None     # other direction ok
    assert await troute._same_direction_block(acct, "A", "QQQ", "long_call") is None    # other ticker ok


async def test_same_direction_working_entry_blocks():
    order = {"id": 9, "status": "pending", "class": "option", "option_symbol": SPY_P,
             "side": "buy_to_open", "tag": "torquelong_put"}
    acct = _Acct(orders=[order])
    assert "bearish" in await troute._same_direction_block(acct, "A", "SPY", "bear_put")
    assert await troute._same_direction_block(acct, "A", "SPY", "long_call") is None


async def test_neutral_strategy_and_read_failure():
    acct = _Acct(positions=[{"symbol": SPY_C, "quantity": 1, "cost_basis": 200}])
    assert await troute._same_direction_block(acct, "A", "SPY", "iron_condor") is None
    assert "couldn't verify" in await troute._same_direction_block(_Acct(fail=True), "A", "SPY", "long_call")


async def test_place_refuses_a_second_same_direction_entry(monkeypatch):
    acct = _Acct(positions=[{"symbol": "NDXP260710C22100000", "quantity": 1, "cost_basis": 500}])
    async def _resolve(request, user, account_id=None):
        return "tradier", acct, "T", False
    monkeypatch.setattr(troute, "_resolve_torque_broker", _resolve)
    req = PlaceRequest(symbol="NDX", strategy="long_call", order_type="limit", limit_price=70.0,
                       quantity=1, confirm=True, auto_close=True, close_target_pct=30, account_id="T",
                       legs=[{"side": "call", "strike": 22000.0, "action": "buy_to_open",
                              "quantity": 1, "symbol": "NDXP260710C22000000"}])
    with pytest.raises(HTTPException) as ei:
        await torque_place(req, FakeRequest(FakeTradier()), user=DEV)
    assert ei.value.status_code == 409 and "one trade per ticker per direction" in ei.value.detail
    assert acct.placed == []


# ── extended-hours exits ─────────────────────────────────────────────────────
async def test_ext_exit_pcts_row_then_global_then_regular(monkeypatch):
    async def _glob():
        return 10.0, 15.0
    monkeypatch.setattr(troute, "_ext_globals", _glob)
    tcfg._SETUP["SPY"] = {**tcfg._SETUP["SPY"], "ext_stop_loss_pct": 8, "ext_close_target_pct": None}
    assert await troute.ext_exit_pcts("SPY") == (8.0, 15.0)
    async def _none():
        return None, None
    monkeypatch.setattr(troute, "_ext_globals", _none)
    tcfg._SETUP["QQQ"] = {**tcfg._SETUP["QQQ"], "stop_loss_default": None, "close_target_default": None}
    assert await troute.ext_exit_pcts("QQQ") == (tcfg.DEFAULT_STOP_LOSS_PCT, tcfg.DEFAULT_CLOSE_TARGET_PCT)


async def test_ext_globals_db_then_config(monkeypatch):
    troute._EXT_CACHE.update(t=0.0, vals=None)
    async def _cfg(key):
        return {"torque_ext_stop_loss_pct": "12", "torque_ext_close_target_pct": "bad"}[key]
    monkeypatch.setattr(troute.supabase_admin, "get_app_config", _cfg)
    sl, tp = await troute._ext_globals()
    troute._EXT_CACHE.update(t=0.0, vals=None)
    assert sl == 12.0 and tp == 15.0          # malformed DB value -> config seed (15)


async def test_place_in_extended_session_arms_the_extended_exits(monkeypatch):
    async def _ext(client):
        return True
    monkeypatch.setattr(troute, "_in_extended_session", _ext)
    async def _hours():
        return ("07:30", "16:15"), frozenset({"NDX"})
    monkeypatch.setattr(troute, "torque_hours", _hours)
    async def _pcts(sym):
        return 10.0, 15.0
    monkeypatch.setattr(troute, "ext_exit_pcts", _pcts)
    req = PlaceRequest(symbol="NDX", strategy="long_call", order_type="limit", limit_price=70.0,
                       quantity=1, confirm=True, auto_close=False, close_target_pct=50,
                       stop_loss_pct=0, account_id="T",
                       legs=[{"side": "call", "strike": 22000.0, "action": "buy_to_open",
                              "quantity": 1, "symbol": "NDXP260710C22000000"}])
    try:
        await torque_place(req, FakeRequest(FakeTradier()), user=DEV)
    except Exception:
        pass                                   # only the arming decision matters here
    assert req.auto_close is True and req.stop_loss_pct == 10.0 and req.close_target_pct >= 15.0


# ── guardian: extended stop for an extended-hours entry ─────────────────────
def _unit():
    return g.build_units([{"symbol": SPY_C, "quantity": 1, "cost_basis": 200}], [])[0][0]


def test_decide_uses_the_extended_stop_regardless_of_the_page():
    px = {"complete": True, "abs_mid": 1.85, "net_bid": 1.84, "net_ask": 1.86}   # -7.5%
    kw = dict(row=None, watcher_syms=set(), px=px, has_stop=False, now=0.0, akey="A", takeover_sec=10.0)
    # Torque position, page stop switched OFF: extended entry still guarded at 5%.
    act, _n, pct = g.decide(_unit(), source="torque", settings={"SPY": (False, 30.0)}, ext_stop=5.0, **kw)
    assert pct == 5.0 and act != "sl_off"
    # Same position, regular entry: the page's off wins.
    act, _n, _p = g.decide(_unit(), source="torque", settings={"SPY": (False, 30.0)}, **kw)
    assert act == "sl_off"
    # Outside position: still needs stop-loss switched on, then the extended %.
    act, _n, _p = g.decide(_unit(), source="outside", settings={}, ext_stop=5.0, **kw)
    assert act == "sl_off"
    act, _n, pct = g.decide(_unit(), source="outside", settings={"SPY": (True, 30.0)}, ext_stop=5.0, **kw)
    assert pct == 5.0


# ── session-based exits: re-pricing Torque's resting exits ──────────────────
class _Mod:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def modify_order(self, account_id, order_id, price=None, order_type=None, duration=None, stop=None):
        if self.fail:
            raise RuntimeError("broker says no")
        self.calls.append((str(order_id), price, stop))
        return {"order": {"id": order_id, "status": "ok"}}


def _spy_unit():
    return g.build_units([{"symbol": SPY_C, "quantity": 1, "cost_basis": 200}], [])[0][0]   # entry 2.00


def _bracket(unit, sl, tp, tag="torqueNewslongcall"):
    t = g.exit_targets(unit, sl, tp)
    return {"id": 1, "class": "otoco", "status": "open", "tag": tag, "leg": [
        {"id": 11, "type": "limit", "side": "buy_to_open", "status": "filled", "option_symbol": SPY_C, "price": 2.0},
        {"id": 12, "type": "limit", "side": "sell_to_close", "status": "open", "option_symbol": SPY_C,
         "price": t["limit"]["price"]},
        {"id": 13, "type": "stop_limit", "side": "sell_to_close", "status": "open", "option_symbol": SPY_C,
         "stop_price": t["stop_limit"]["stop"], "price": t["stop_limit"]["price"]}]}


async def test_reprice_moves_a_regular_bracket_to_extended_values():
    u = _spy_unit()
    c = _Mod()
    n = await g.reprice_exits(c, "A", "acct", u, [_bracket(u, 30, 30)], now=0.0, want=(10, 15))
    ext = g.exit_targets(u, 10, 15)
    assert n == 2
    assert ("12", ext["limit"]["price"], None) in c.calls
    assert ("13", ext["stop_limit"]["price"], ext["stop_limit"]["stop"]) in c.calls
    # Already there → nothing to do.
    c2 = _Mod()
    assert await g.reprice_exits(c2, "A", "acct", u, [_bracket(u, 10, 15)], now=0.0, want=(10, 15)) == 0


async def test_back_to_regular_only_moves_what_sits_at_extended_values():
    u = _spy_unit()
    c = _Mod()
    n = await g.reprice_exits(c, "A", "acct", u, [_bracket(u, 10, 15)], now=0.0,
                              want=(30, 30), only_from=(10, 15))
    assert n == 2
    # A bracket Torque never moved (e.g. a floor-raised 45% target) is left alone.
    c2 = _Mod()
    n = await g.reprice_exits(c2, "A", "acct", u, [_bracket(u, 30, 45)], now=0.0,
                              want=(30, 30), only_from=(10, 15))
    assert n == 0 and c2.calls == []


async def test_reprice_never_touches_non_torque_orders_and_backs_off_on_failure():
    u = _spy_unit()
    c = _Mod()
    assert await g.reprice_exits(c, "A", "acct", u, [_bracket(u, 30, 30, tag="mine")], now=0.0,
                                 want=(10, 15)) == 0
    g._REPRICE_FAIL.clear()
    bad = _Mod(fail=True)
    assert await g.reprice_exits(bad, "A", "acct2", u, [_bracket(u, 30, 30)], now=100.0, want=(10, 15)) == 0
    ok = _Mod()
    assert await g.reprice_exits(ok, "A", "acct2", u, [_bracket(u, 30, 30)], now=120.0, want=(10, 15)) == 0
    assert await g.reprice_exits(ok, "A", "acct2", u, [_bracket(u, 30, 30)], now=200.0, want=(10, 15)) == 2
    g._REPRICE_FAIL.clear()


@pytest.mark.parametrize("exec_px,tgt,et,hit", [
    (2.30, 2.30, "debit", True), (2.29, 2.30, "debit", False),
    (0.85, 0.85, "credit", True), (0.86, 0.85, "credit", False), (None, 2.30, "debit", False)])
def test_tp_reached_at(exec_px, tgt, et, hit):
    assert g.tp_reached_at(exec_px, tgt, et) is hit


async def test_reprice_never_moves_a_triggered_stop_leg_up():
    """A stop_limit that triggered shows as a plain limit BELOW entry — it must
    not be mistaken for the take-profit and repriced to the target."""
    u = _spy_unit()
    o = _bracket(u, 30, 30)
    o["leg"][2] = {**o["leg"][2], "type": "limit", "stop_price": None, "price": 1.35}
    c = _Mod()
    await g.reprice_exits(c, "A", "acct", u, [o], now=0.0, want=(10, 15))
    assert all(call[0] != "13" for call in c.calls)


def test_regular_exit_pcts_by_source():
    u = _spy_unit()
    tcfg._SETUP["SPY"] = {**tcfg._SETUP["SPY"], "stop_loss_default": None, "close_target_default": 30}
    assert g.regular_exit_pcts(u, "reflex", {"SPY": (True, 20.0)}, {"SPY": (True, 40.0)}) == (30.0, 30.0)
    assert g.regular_exit_pcts(u, "torque", {"SPY": (True, 20.0)}, {"SPY": (True, 40.0)}) == (20.0, 40.0)
    assert g.regular_exit_pcts(u, "torque", {}, {}) == (30.0, 30.0)


async def test_watcher_stop_follows_the_session(monkeypatch):
    async def _ext(client):
        return True
    monkeypatch.setattr(troute, "_in_extended_session", _ext)
    async def _hours():
        return ("07:30", "16:15"), frozenset({"SPY"})
    monkeypatch.setattr(troute, "torque_hours", _hours)
    async def _pcts(sym):
        return 10.0, 15.0
    monkeypatch.setattr(troute, "ext_exit_pcts", _pcts)
    assert await troute._session_stop_pct(None, "SPY", 30.0) == 10.0
    assert await troute._session_stop_pct(None, "NDX", 30.0) == 30.0     # not an extended ticker


async def test_reprice_skips_an_oco_whose_prices_would_be_under_the_broker_gap(monkeypatch):
    """Entry 0.30: 15% target ~0.35 vs 10% stop ~0.27 — under Tradier's $0.10
    OCO minimum, so the modify is skipped up front (and logged once)."""
    g._GAP_LOGGED.clear()
    u = g.build_units([{"symbol": SPY_C, "quantity": 1, "cost_basis": 30}], [])[0][0]
    c = _Mod()
    assert await g.reprice_exits(c, "A", "acct", u, [_bracket(u, 30, 30)], now=0.0, want=(10, 15)) == 0
    assert c.calls == [] and len(g._GAP_LOGGED) == 1
    g._GAP_LOGGED.clear()
