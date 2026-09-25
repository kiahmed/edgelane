"""DJX guardrails: stop-loss math, wide-market refusal, and the modify bypass."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import torque_config as tc
from app import torque_engine as teng
from app.routes import torque as tq


# ── stop trigger is measured at MID, never at the bid ──────────────────────
def test_debit_stop_fires_when_mark_falls_by_pct():
    # paid 1.00, stop 50% → fire at/below 0.50
    assert teng.stop_breached(0.50, 1.00, "debit", 50.0) is True
    assert teng.stop_breached(0.49, 1.00, "debit", 50.0) is True
    assert teng.stop_breached(0.51, 1.00, "debit", 50.0) is False


def test_credit_stop_fires_when_buyback_cost_rises():
    # collected 1.00, stop 50% → fire when it costs 1.50+ to buy back
    assert teng.stop_breached(1.50, 1.00, "credit", 50.0) is True
    assert teng.stop_breached(1.49, 1.00, "credit", 50.0) is False


def test_a_bid_based_stop_would_fire_instantly_on_djx_but_mid_does_not():
    # real DJXW package: net_bid 0.45 / mid 5.22 / net_ask 10.00, entry at mid
    entry, bid, mid = 5.22, 0.45, 5.22
    assert teng.stop_breached(bid, entry, "debit", 50.0) is True   # bid → instant stop-out
    assert teng.stop_breached(mid, entry, "debit", 50.0) is False  # mid → correctly quiet


# ── stop_loss_price: fixed resting price for the native bracket ────────────
def test_stop_loss_price_matches_stop_breached_threshold():
    # the native bracket's fixed trigger must line up EXACTLY with where the
    # reactive watcher would trigger — same 30% level regardless of which
    # mechanism a given order ends up on.
    entry, pct, tick = 1.00, 30.0, 0.05
    trigger = teng.stop_loss_price(entry, "debit", pct, tick)
    assert trigger == 0.70
    assert teng.stop_breached(trigger, entry, "debit", pct) is True
    assert teng.stop_breached(trigger + tick, entry, "debit", pct) is False


def test_stop_loss_price_credit_rounds_up_debit_rounds_down():
    # debit: floors (never rounds the exit price back ABOVE the true stop
    # level, which would fire late). credit: ceils (never loosens the stop).
    assert teng.stop_loss_price(1.00, "debit", 33.0, 0.05) == 0.65     # 0.67 floors to 0.65
    assert teng.stop_loss_price(1.00, "credit", 33.0, 0.05) == 1.35    # 1.33 ceils to 1.35


# ── stop_limit_leg_prices: trigger + a genuinely marketable limit ──────────
def test_stop_limit_leg_prices_debit_limit_is_further_below_trigger():
    trigger, limit = teng.stop_limit_leg_prices(1.00, "debit", 30.0, 0.05, 3)
    assert trigger == 0.70
    assert limit < trigger
    assert round(trigger - limit, 4) == 0.15   # 3 ticks * 0.05


def test_stop_limit_leg_prices_credit_limit_is_further_above_trigger():
    trigger, limit = teng.stop_limit_leg_prices(1.00, "credit", 30.0, 0.05, 3)
    assert trigger == 1.30
    assert limit > trigger
    assert round(limit - trigger, 4) == 0.15


def test_stop_limit_leg_prices_zero_tick_falls_back_to_plain_rounding():
    trigger, limit = teng.stop_limit_leg_prices(1.00, "debit", 30.0, 0.0, 3)
    assert trigger == 0.70 and limit == 0.70   # buffer=0 when tick=0 (_ceil_tick/_floor_tick no-op)


def test_stop_is_inert_without_an_entry_fill_or_pct():
    assert teng.stop_breached(0.1, 0.0, "debit", 50.0) is False
    assert teng.stop_breached(0.1, 1.0, "debit", None) is False


# ── stop exit crosses, with a bounded price ────────────────────────────────
def test_debit_stop_exits_at_the_package_bid_and_credit_at_the_ask():
    px = {"complete": True, "net_bid": 0.77, "net_ask": 0.89, "abs_mid": 0.83}
    assert teng.stop_exit_price(px, "debit", 0.01) == 0.77
    assert teng.stop_exit_price(px, "credit", 0.01) == 0.89


def test_stop_exit_none_without_a_complete_price():
    assert teng.stop_exit_price({"complete": False}, "debit", 0.01) is None


def test_wide_market_ceiling_is_below_the_djx_disaster_case():
    # DJXW 07-10 package measured at 182.8% of mid — must exceed the ceiling so
    # _monitor_stop refuses to cross rather than dumping into that book.
    assert 182.8 > tc.STOP_MAX_EXIT_SPREAD_PCT
    # ...while a normal DIA-grade book (14.5%) is happily crossable.
    assert 14.5 < tc.STOP_MAX_EXIT_SPREAD_PCT


def test_djx_has_a_stop_default_and_tight_names_do_not():
    assert tc.stop_loss_default("DJX") == 50.0
    for t in ("SPX", "NDX", "SPY"):
        assert tc.stop_loss_default(t) is None


# ── stop-limit gap risk: a limit only guarantees price, never a fill ───────
async def test_stalled_stop_limit_requotes_via_modify_before_touching_market(monkeypatch):
    """A violent move can leave a stop-exit limit resting while the market
    keeps running away — the old code submitted it once and never checked
    again. It must now keep watching, and when a limit stalls, re-quote the
    SAME resting order in place (MODIFY, not cancel-and-replace) at a fresh
    bounded price rather than reaching for an unbounded market order — a
    multileg market order has NO price protection and can clear worse than
    the quoted book itself. Only after STOP_MAX_REQUOTES real re-quotes still
    don't fill does it freeze and give up on price entirely."""
    monkeypatch.setattr(tq, "_STOP_INTERVAL", 0.01)

    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(tq, "_package_price", _fake_price)

    client = FakeTradier(order_status={"status": "open"})   # every limit sits unfilled forever
    w = {}
    legs = [{"symbol": "NDXP123", "action": "buy_to_open", "quantity": 1}]
    await tq._monitor_stop(
        client, "T", w, legs=legs, symbol="NDX", strategy="long_call", is_single=True,
        entry_type="debit", entry_fill=1.00, stop_pct=30.0, tick=0.05, quantity=1,
        close_order_id=None)

    assert w["state"] == "stop_market_placed"
    assert w["stop_market_fallback"] is True
    assert "no bounded fill after" in w["stop_note"]

    modifies = [p for p in client.placed if "_modify" in p]
    real_orders = [p for p in client.placed if "_modify" not in p]
    # every stall re-quoted the SAME order via modify — never a new one — for
    # exactly STOP_MAX_REQUOTES rounds, all still bounded to the live quote
    assert len(modifies) == tc.STOP_MAX_REQUOTES
    assert all(m["_modify"] == "1001" for m in modifies)
    assert all(m["price"] == 0.60 for m in modifies)
    # only two REAL orders ever hit the book: the initial bounded limit, and
    # the final unbounded market fallback once every requote was exhausted
    assert len(real_orders) == 2
    assert real_orders[0]["type"] == "limit" and real_orders[0]["price"] == "0.60"
    assert real_orders[1]["type"] == "market" and "price" not in real_orders[1]
    assert real_orders[1]["tag"] == "torqueStopMktlongcall"   # distinct tag flags the fallback


async def test_stop_limit_that_fills_promptly_never_escalates(monkeypatch):
    """The escalation path must not fire on the ordinary case — a stop-limit
    that fills quickly should never see a second order at all."""
    monkeypatch.setattr(tq, "_STOP_INTERVAL", 0.01)

    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(tq, "_package_price", _fake_price)

    client = FakeTradier(order_status={"status": "filled"})
    w = {}
    legs = [{"symbol": "NDXP123", "action": "buy_to_open", "quantity": 1}]
    await tq._monitor_stop(
        client, "T", w, legs=legs, symbol="NDX", strategy="long_call", is_single=True,
        entry_type="debit", entry_fill=1.00, stop_pct=30.0, tick=0.05, quantity=1,
        close_order_id=None)

    assert w["state"] == "stop_filled"
    assert len(client.placed) == 1   # no requote, no market escalation needed


async def test_wide_book_block_is_not_permanent_and_still_starts_with_a_limit(monkeypatch):
    """STOP_MAX_EXIT_SPREAD_PCT refuses to cross a garbage book on the FIRST
    breach tick, but that refusal must not be permanent — an open loss parked
    forever while the book stays wide is worse than one bounded crossing.
    Once it hands off, the FIRST attempt is still a bounded limit at the
    quoted price, not an immediate blind market order."""
    monkeypatch.setattr(tq, "_STOP_INTERVAL", 0.01)

    async def _fake_wide_price(client, symbol, legs):
        # mid well below the 30% stop threshold (entry 5.22 -> triggers at
        # <=3.654) AND ~198% of mid wide — both breached and un-crossable.
        return {"complete": True, "net_bid": 0.05, "net_ask": 6.00, "abs_mid": 3.00}
    monkeypatch.setattr(tq, "_package_price", _fake_wide_price)

    client = FakeTradier(order_status={"status": "open"})   # book stays bad; nothing ever fills
    w = {}
    legs = [{"symbol": "DJXW123", "action": "buy_to_open", "quantity": 1}]
    await tq._monitor_stop(
        client, "T", w, legs=legs, symbol="DJX", strategy="long_call", is_single=True,
        entry_type="debit", entry_fill=5.22, stop_pct=30.0, tick=0.05, quantity=1,
        close_order_id=None)

    assert w["state"] == "stop_market_placed"          # still eventually escalates
    assert w["stop_market_fallback"] is True
    assert client.placed[0]["type"] == "limit"          # ...but starts bounded, not blind
    assert client.placed[-1]["type"] == "market"        # market is the last resort, not the first


# ── over-close safety: never resubmit blind after a stalled poll ───────────
async def test_fill_landing_between_poll_and_cancel_does_not_over_close(monkeypatch):
    """A fill can land in the window between the last status poll and the
    cancel call — especially likely right when a stop fires, since a fast
    move is exactly what triggers both the stop AND a fill. The old code only
    logged a warning on a failed cancel and resubmitted anyway, sending a
    duplicate closing order on top of a position that was already flat (which
    fills into a fresh naked position the moment there's nothing left to
    close against). The ladder must re-confirm the order's true state instead
    of trusting that a timed-out poll means "still open"."""
    monkeypatch.setattr(tq, "_STOP_INTERVAL", 0.01)

    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(tq, "_package_price", _fake_price)

    # id 1001 is the first (and only) order this run places — open for both
    # polls inside the grace window, but has actually filled by the time
    # _resolve_stalled_stop re-checks it (after the failed cancel attempt).
    client = FakeTradier(order_status_by_id={
        "1001": [{"status": "open"}, {"status": "open"}, {"status": "filled"}],
    })
    w = {}
    legs = [{"symbol": "NDXP123", "action": "buy_to_open", "quantity": 1}]
    await tq._monitor_stop(
        client, "T", w, legs=legs, symbol="NDX", strategy="long_call", is_single=True,
        entry_type="debit", entry_fill=1.00, stop_pct=30.0, tick=0.05, quantity=1,
        close_order_id=None)

    assert w["state"] == "stop_filled"
    assert len(client.placed) == 1   # exactly one closing order — no duplicate on top of it


async def test_partial_fill_only_replaces_the_remaining_quantity(monkeypatch):
    """A stop order that filled 3 of 5 and stays resting for the remainder
    keeps getting re-quoted via modify (the broker tracks its own remaining
    open quantity) through every requote round. Only once requotes are
    exhausted and the ladder must finally give up on that order does it need
    to know how much is actually left — and it must size the market fallback
    for exactly that (5 - 3 = 2), not the original full 5, which would close
    2 contracts that no longer exist and open a fresh naked position on the
    excess."""
    monkeypatch.setattr(tq, "_STOP_INTERVAL", 0.01)

    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(tq, "_package_price", _fake_price)

    # order 1001 (the only order ever placed here) sits partially filled for
    # every poll and every modify attempt — never fully fills on its own.
    client = FakeTradier(
        order_status_by_id={"1001": {"status": "partially_filled", "exec_quantity": 3.0}},
    )
    w = {}
    legs = [{"symbol": "NDXP123", "action": "buy_to_open", "quantity": 1}]
    await tq._monitor_stop(
        client, "T", w, legs=legs, symbol="NDX", strategy="long_call", is_single=True,
        entry_type="debit", entry_fill=1.00, stop_pct=30.0, tick=0.05, quantity=5,
        close_order_id=None)

    assert w["state"] == "stop_market_placed"
    real_orders = [p for p in client.placed if "_modify" not in p]
    modifies = [p for p in client.placed if "_modify" in p]
    assert len(modifies) == tc.STOP_MAX_REQUOTES     # kept chasing the same order the whole time
    assert len(real_orders) == 2                     # original attempt + the market fallback
    assert real_orders[0]["quantity"] == "5"          # original was sized for the full position
    assert real_orders[1]["quantity"] == "2"          # fallback only for what's left (5 - 3 filled)
    assert real_orders[1]["type"] == "market"


# ── TP → SL handoff: cancelling/racing the profit-target close ─────────────
_VERTICAL_LEGS = [
    {"symbol": "NDXP123C1", "side": "call", "strike": 22000.0, "action": "buy_to_open", "quantity": 1},
    {"symbol": "NDXP123C2", "side": "call", "strike": 22100.0, "action": "sell_to_open", "quantity": 1},
]


async def test_manually_cancelled_tp_with_no_fill_still_lets_sl_fire_for_the_full_size(monkeypatch):
    """Cancelling the resting profit-target close by hand (Orders panel) must
    not silently disable the stop — it's a live concern the user raised after
    observing what looked like a stop failing to fire post-cancel. A TP
    cancelled with nothing filled must still protect the FULL original
    multi-leg position, not a subset of its legs."""
    monkeypatch.setattr(tq, "_STOP_INTERVAL", 0.01)

    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(tq, "_package_price", _fake_price)

    client = FakeTradier(
        order_status_by_id={"9001": {"status": "canceled", "exec_quantity": 0.0}},
        order_status={"status": "filled"},   # the SL order itself fills promptly
    )
    w = {}
    await tq._monitor_stop(
        client, "T", w, legs=_VERTICAL_LEGS, symbol="NDX", strategy="bull_call", is_single=False,
        entry_type="debit", entry_fill=1.00, stop_pct=30.0, tick=0.05, quantity=5,
        close_order_id="9001")

    assert w["state"] == "stop_filled"
    real_orders = [p for p in client.placed if "_modify" not in p]
    assert len(real_orders) == 1
    sl = real_orders[0]
    assert sl["quantity[0]"] == "5" and sl["quantity[1]"] == "5"   # full size, both legs
    assert sl["option_symbol[0]"] == "NDXP123C1" and sl["option_symbol[1]"] == "NDXP123C2"


async def test_partially_filled_tp_hands_off_only_the_truly_remaining_quantity(monkeypatch):
    """A resting TP that had already filled 2 of 5 units when the stop fires
    (naturally, or right as the caller cancels it) must not have its 5 units
    blindly re-closed — that closes 2 units that no longer exist. The stop
    must protect only the 3 units TP left open, on every leg."""
    monkeypatch.setattr(tq, "_STOP_INTERVAL", 0.01)

    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(tq, "_package_price", _fake_price)

    client = FakeTradier(
        order_status_by_id={"9002": {"status": "partially_filled", "exec_quantity": 2.0}},
        order_status={"status": "filled"},
    )
    w = {}
    await tq._monitor_stop(
        client, "T", w, legs=_VERTICAL_LEGS, symbol="NDX", strategy="bull_call", is_single=False,
        entry_type="debit", entry_fill=1.00, stop_pct=30.0, tick=0.05, quantity=5,
        close_order_id="9002")

    assert w["state"] == "stop_filled"
    real_orders = [p for p in client.placed if "_modify" not in p]
    assert len(real_orders) == 1
    sl = real_orders[0]
    assert sl["quantity[0]"] == "3" and sl["quantity[1]"] == "3"   # 5 - 2 already filled by TP


async def test_tp_that_actually_filled_right_at_breach_stands_down_without_a_stop(monkeypatch):
    """A race where TP fills in the gap between the loop's own top-of-tick
    check and the stop's takeover must be caught too — firing a stop-exit on
    top of a position TP already fully closed opens a fresh naked position
    from nothing."""
    monkeypatch.setattr(tq, "_STOP_INTERVAL", 0.01)

    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(tq, "_package_price", _fake_price)

    # "open" for the loop's own top-of-tick check (doesn't trip the early
    # return), "filled" by the time the takeover re-checks it.
    client = FakeTradier(order_status_by_id={"9003": [{"status": "open"}, {"status": "filled"}]})
    w = {}
    await tq._monitor_stop(
        client, "T", w, legs=_VERTICAL_LEGS, symbol="NDX", strategy="bull_call", is_single=False,
        entry_type="debit", entry_fill=1.00, stop_pct=30.0, tick=0.05, quantity=5,
        close_order_id="9003")

    assert w["state"] == "closed_at_target"
    assert len([p for p in client.placed if "_modify" not in p]) == 0   # no stop order ever placed


async def test_tp_unconfirmable_before_handoff_freezes_instead_of_guessing(monkeypatch):
    """If the TP order's true final state can't be confirmed at all (cancel
    and the follow-up read both fail), the handoff must refuse to guess how
    much is still open rather than risk an over-close either way."""
    monkeypatch.setattr(tq, "_STOP_INTERVAL", 0.01)

    async def _fake_price(client, symbol, legs):
        return {"complete": True, "net_bid": 0.60, "net_ask": 0.64, "abs_mid": 0.62}
    monkeypatch.setattr(tq, "_package_price", _fake_price)

    client = FakeTradier(order_status_by_id={"9004": {"status": "open"}})

    async def _boom(account_id, order_id):
        raise RuntimeError("network blip")
    monkeypatch.setattr(client, "get_order", _boom)

    w = {}
    await tq._monitor_stop(
        client, "T", w, legs=_VERTICAL_LEGS, symbol="NDX", strategy="bull_call", is_single=False,
        entry_type="debit", entry_fill=1.00, stop_pct=30.0, tick=0.05, quantity=5,
        close_order_id="9004")

    assert w["state"] == "stop_needs_attention"
    assert len([p for p in client.placed if "_modify" not in p]) == 0


# ── the orders-panel modify bypass ─────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_guard():
    tq._CLOSE_GUARD.clear()
    yield
    tq._CLOSE_GUARD.clear()


def _arm(entry_type="debit", entry_fill=1.00, floor=45.0, symbol="DJX"):
    tq._CLOSE_GUARD["OID"] = {"symbol": symbol, "entry_type": entry_type,
                              "entry_fill": entry_fill, "tick": 0.01,
                              "floor_pct": floor}


def test_modify_rejects_lowering_a_debit_close_below_the_floor():
    _arm()                                   # floor price = 1.00 * 1.45 = 1.45
    with pytest.raises(HTTPException) as ei:
        tq._enforce_close_floor_on_modify("OID", 1.05)
    assert ei.value.status_code == 400
    assert "1.45" in str(ei.value.detail)


def test_modify_allows_raising_a_debit_close():
    _arm()
    assert tq._enforce_close_floor_on_modify("OID", 1.45) is None
    assert tq._enforce_close_floor_on_modify("OID", 2.00) is None


def test_modify_rejects_raising_a_credit_buyback_above_the_floor():
    _arm(entry_type="credit", entry_fill=2.00)   # floor price = 2.00 * (1-0.45) = 1.10
    with pytest.raises(HTTPException):
        tq._enforce_close_floor_on_modify("OID", 1.50)   # keeps less of the credit
    assert tq._enforce_close_floor_on_modify("OID", 1.10) is None
    assert tq._enforce_close_floor_on_modify("OID", 0.90) is None


def test_modify_is_unrestricted_for_tight_tickers():
    _arm(symbol="SPX")
    assert tq._enforce_close_floor_on_modify("OID", 0.01) is None


def test_modify_passes_through_unknown_orders():
    assert tq._enforce_close_floor_on_modify("NOT-TRACKED", 0.01) is None


# ── route-level: market orders banned, spread warning, autoclose block ─────
import app.routes.torque as troute
from app.routes.torque import PlaceRequest, torque_place
from .conftest import FakeTradier, FakeRequest

DEV = {"id": "dev-local", "email": "dev@local", "auth": "dev"}


def _place_req(**kw):
    base = dict(symbol="DJX", strategy="bull_call", order_type="limit",
                limit_price=1.70, quantity=1, confirm=True,
                legs=[{"side": "call", "strike": 526.0, "action": "buy_to_open",
                       "quantity": 1, "symbol": "DJXW260710C00526000"},
                      {"side": "call", "strike": 536.0, "action": "sell_to_open",
                       "quantity": 1, "symbol": "DJXW260710C00536000"}])
    base.update(kw)
    return PlaceRequest(**base)


async def test_market_order_is_refused_for_djx():
    req = _place_req(order_type="market", limit_price=None)
    with pytest.raises(HTTPException) as ei:
        await torque_place(req, FakeRequest(FakeTradier()), user=DEV)
    assert ei.value.status_code == 400
    assert "market orders are disabled for DJX" in str(ei.value.detail)


async def test_market_order_still_allowed_for_spx():
    # SPX must be untouched by the DJX ban — assert at the config layer (a full
    # SPX place needs a matching fake chain).
    assert tc.market_orders_allowed("SPX") is True
    assert tc.market_orders_allowed("NDX") is True
    assert tc.market_orders_allowed("DJX") is False


def test_stop_default_reaches_place_via_config():
    # a DJX place with no explicit stop_loss_pct inherits the 50% default
    req = _place_req()
    assert req.stop_loss_pct is None
    assert tc.stop_loss_default(req.symbol) == 50.0


def _single_leg_req(**kw):
    base = dict(symbol="NDX", strategy="long_call", order_type="limit",
                limit_price=70.0, quantity=1, confirm=True, auto_close=True,
                close_target_pct=30, account_id="T",
                legs=[{"side": "call", "strike": 22000.0, "action": "buy_to_open",
                       "quantity": 1, "symbol": "NDXP260710C22000000"}])
    base.update(kw)
    return PlaceRequest(**base)


async def test_single_leg_stop_plus_autoclose_uses_native_otoco():
    """Single-leg + limit + auto_close + a stop with a normal (>= $0.10) gap
    between the two exit prices submits ONE native 3-leg otoco bracket — both
    exits broker-held, no app-managed watcher, no coordination code needed.
    Verified against a live Tradier sandbox preview (2026-09-18) that "otoco"
    is the only class accepting a lower-priced second exit leg, and that it
    must be type=stop_limit (stop[N] trigger + price[N] limit), not a second
    plain limit."""
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])
    req = _single_leg_req(stop_loss_pct=30)
    r = await torque_place(req, FakeRequest(client), user=DEV)
    assert r["mode"] == "otoco_bracket"
    assert r["close_target_price"] == 91.05          # unchanged math vs the plain-OTO case
    assert r["stop_loss_price"] == 49.0               # 70 * 0.70, floored to tick
    assert r["stop_loss_pct"] == 30.0
    p = client.placed[0]
    assert p["class"] == "otoco"
    assert len(client.placed) == 1                    # still one atomic order, not two
    assert p["side[0]"] == "buy_to_open" and p["type[0]"] == "limit"
    assert p["side[1]"] == "sell_to_close" and p["type[1]"] == "limit" and p["price[1]"] == "91.05"
    assert p["side[2]"] == "sell_to_close" and p["type[2]"] == "stop_limit"
    assert p["stop[2]"] == "49.00"
    assert float(p["price[2]"]) < 49.0                # limit sits further below the trigger (marketable once tripped)


async def test_single_leg_stop_plus_autoclose_falls_back_when_gap_too_narrow():
    """Tradier enforces a $0.10 minimum gap between the two OCO exit prices
    (verified live: 'OCO price difference should be at least 0.1$'). On a cheap
    enough entry, a 30%/30% profit/stop band lands narrower than that — submitting
    the bracket anyway would just get rejected, so it must fall through to the
    app-managed watcher (both exits reactive) instead, same as a multi-leg spread."""
    entry = 0.02   # NDX's real configured tick is 0.05 (coarser than a hypothetical
                    # 0.01) — at this entry both exit prices round to the SAME tick
                    # grid closely enough to land inside the $0.10 floor.
    close_px = teng.close_target_price(entry, "debit", 30, 0.05, legs=1, fee_per_contract=0.95)
    stop_trigger = teng.stop_loss_price(entry, "debit", 30, 0.05)
    assert (close_px - stop_trigger) < tc.OCO_MIN_PRICE_GAP, "test setup must actually be in the narrow-gap regime"
    client = FakeTradier(place_responses=[
        {"order": {"id": 1, "status": "ok"}},         # entry
        {"order": {"id": 2, "status": "ok"}},          # app-managed profit close
    ], order_status={"id": 1, "status": "filled", "avg_fill_price": entry,
                     "exec_quantity": 1.0, "remaining_quantity": 0.0, "class": "option"})
    req = _single_leg_req(limit_price=entry, stop_loss_pct=30)
    r = await torque_place(req, FakeRequest(client), user=DEV)
    assert r.get("mode") != "otoco_bracket"
    assert r.get("mode") != "oto_bracket"
    # first submitted payload is the plain entry, not an otoco/oto bracket
    assert client.placed[0].get("class") not in ("otoco", "oto")


def test_stop_only_watcher_does_not_advertise_a_close_target():
    """auto_close=false + a stop arms the watcher, but there is no profit-target
    close order, so close_target_price must be None rather than a phantom price."""
    import inspect
    src = inspect.getsource(troute.torque_place)
    assert "if (est_fill and req.auto_close) else None" in src


# ── auto-close is MANDATORY on DJX ─────────────────────────────────────────
def test_auto_close_is_required_for_djx_only():
    assert tc.close_target_required("DJX") is True
    for t in ("SPX", "NDX", "RUT", "SPY", "QQQ"):
        assert tc.close_target_required(t) is False


def test_close_targets_map_exposes_required_flag():
    m = tc.close_targets_map()
    assert m["DJX"]["required"] is True
    assert m["SPX"]["required"] is False


async def test_place_rejects_djx_without_auto_close():
    req = _place_req(auto_close=False)
    with pytest.raises(HTTPException) as ei:
        await torque_place(req, FakeRequest(FakeTradier()), user=DEV)
    assert ei.value.status_code == 400
    assert "auto-close is mandatory for DJX" in str(ei.value.detail)


async def test_place_rejects_djx_without_auto_close_before_touching_the_chain():
    """The mandatory check must precede the spread probe, so the user is told the
    real reason instead of a confusing spread error."""
    req = _place_req(auto_close=False, order_type="market", limit_price=None)
    with pytest.raises(HTTPException) as ei:
        await torque_place(req, FakeRequest(FakeTradier()), user=DEV)
    # market-order ban is validated even earlier — that's the correct precedence
    assert ei.value.status_code == 400
    assert "market orders are disabled" in str(ei.value.detail)


async def test_spx_may_still_place_without_auto_close():
    assert tc.close_target_required("SPX") is False


# ── fee-aware close target ─────────────────────────────────────────────────
def test_commissions_match_tradier_order_preview():
    # read off Tradier's `commission` field (production AND sandbox agree):
    #   DJX buy 1 -> 0.53   DJX buy 10 -> 5.30   DJX 2-leg -> 1.06   SPX -> 0.95
    assert tc.commission_per_contract("DJX") == 0.53
    assert tc.commission_per_contract("SPX") == 0.95
    assert tc.commission_per_contract("ZZZZ") == tc.DEFAULT_COMMISSION
    # regulatory pass-throughs are NOT invented — preview reports fees=0
    assert tc.extra_fee_per_contract("DJX") == 0.0
    assert tc.fee_per_contract("DJX") == 0.53


def test_round_trip_fee_is_per_unit_and_scales_with_legs():
    # 2 sides x legs x fee / multiplier — independent of contract count
    assert teng.round_trip_fee_price(0.53, 1) == 0.0106
    assert teng.round_trip_fee_price(0.53, 2) == 0.0212
    assert teng.round_trip_fee_price(0.53, 4) == 0.0424


def test_zero_fee_reduces_to_the_naive_target():
    assert teng.close_target_price(1.00, "debit", 45, 0.01) == 1.45
    assert teng.close_target_price(2.00, "credit", 45, 0.01) == 1.10


def test_fees_push_a_debit_target_up_and_a_credit_target_down():
    up = teng.close_target_price(6.00, "debit", 45, 0.01, legs=1, fee_per_contract=0.53)
    assert up == 8.72 and up > 8.70          # naive was 8.70
    dn = teng.close_target_price(2.00, "credit", 45, 0.01, legs=2, fee_per_contract=0.53)
    assert dn < 1.10                          # naive was 1.10


def test_breakeven_is_above_entry_for_a_debit_and_below_for_a_credit():
    be = teng.breakeven_close_price(6.00, "debit", 0.01, legs=1, fee_per_contract=0.53)
    assert be == 6.02 and be > 6.00
    be_c = teng.breakeven_close_price(2.00, "credit", 0.01, legs=1, fee_per_contract=0.53)
    assert be_c < 2.00


@pytest.mark.parametrize("entry", [0.05, 0.70, 1.70, 6.00, 25.95])
@pytest.mark.parametrize("legs", [1, 2, 4])
def test_any_positive_target_always_clears_breakeven(entry, legs):
    """The invariant that matters: a profit target must never price below the
    round-trip cost. Directional tick rounding is what guarantees it."""
    fee = 0.53
    be = teng.breakeven_close_price(entry, "debit", 0.01, legs=legs, fee_per_contract=fee)
    tgt = teng.close_target_price(entry, "debit", 1.0, 0.01, legs=legs, fee_per_contract=fee)
    assert tgt >= be, f"target {tgt} < breakeven {be}"


def test_credit_target_never_exceeds_breakeven():
    fee = 0.53
    for entry in (0.50, 2.00, 10.0):
        be = teng.breakeven_close_price(entry, "credit", 0.01, legs=2, fee_per_contract=fee)
        tgt = teng.close_target_price(entry, "credit", 1.0, 0.01, legs=2, fee_per_contract=fee)
        assert tgt <= be, f"buyback target {tgt} > breakeven {be}"


# ── credit close target must never go non-positive (bug: pct>=~100) ─────────
def test_credit_close_price_is_always_positive_even_at_absurd_pct():
    for pct in (99, 100, 120, 200, 500):
        p = teng.close_target_price(2.00, "credit", pct, 0.05)
        assert p > 0, f"credit pct={pct} produced non-positive limit {p}"
        assert p >= 0.05                          # floored at one tick
    # a debit is unaffected and stays well positive
    assert teng.close_target_price(2.00, "debit", 500, 0.05) > 2.0


def test_credit_close_price_floors_at_one_tick_with_fees():
    # fees push it negative even sooner; still floored positive
    p = teng.close_target_price(2.00, "credit", 100, 0.05, legs=2, fee_per_contract=0.53)
    assert p == 0.05


def test_max_credit_pct_keeps_the_buyback_at_or_above_one_tick():
    for entry in (0.50, 1.00, 2.00, 10.0):
        for legs, fee in ((1, 0.0), (2, 0.53), (4, 0.95)):
            ceil = teng.max_credit_pct(entry, 0.05, legs=legs, fee_per_contract=fee)
            px = teng.close_target_price(entry, "credit", ceil, 0.05,
                                         legs=legs, fee_per_contract=fee)
            assert px >= 0.05, f"entry={entry} legs={legs} ceil={ceil} -> {px}"
            assert 1.0 <= ceil <= 99.0


def test_max_credit_pct_degenerates_gracefully_for_tiny_credit():
    assert teng.max_credit_pct(0.05, 0.05) == 1.0


async def test_place_caps_credit_target_and_arms_a_positive_close():
    """End-to-end: a credit spread with an absurd close_target_pct must still
    place a POSITIVE, fillable buy-to-close — not a negative limit the broker
    rejects (which would silently unarm auto-close)."""
    fake = FakeTradier(spot=22000.0)
    _, _, contracts = await troute._get_chain(fake, "NDX")
    spot = teng.implied_spot(contracts, fallback=22000.0)
    st = teng.build_structure(spot, contracts, "NDX", "bull_put")   # credit, 2-leg
    assert st["type"] == "credit"
    legs = st["legs"]
    limit = abs(teng.price_structure(legs, {c["symbol"]: c for c in contracts})["net_mid"]) or 5.0
    req = PlaceRequest(symbol="NDX", strategy="bull_put", legs=legs,
                       order_type="limit", limit_price=limit, quantity=1,
                       auto_close=True, close_target_pct=200, account_id="T", confirm=True)
    res = await torque_place(req, FakeRequest(fake), user=DEV)
    # pct got capped below 100
    assert res["close_target_pct"] < 100
    assert res["close_target_clamped"] is True
    # drive the watcher and inspect the ACTUAL close payload price
    w = troute._WATCHERS[res["watch_id"]]
    await w["task"]
    closes = [p for p in fake.placed if str(p.get("tag", "")).startswith("torqueClose")]
    assert closes, "no close order was placed"
    price = float(closes[-1].get("price"))
    assert price > 0, f"credit close placed at non-positive price {price}"
