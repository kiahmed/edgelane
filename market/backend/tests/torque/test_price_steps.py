"""Typed prices snap to each ticker's legal step (torque_tickers_setup).

Penny-pilot names (XLF/XLE/SMH) step $0.01 below $3 and $0.05 at/above; an
off-increment limit is rejected by the broker, so /torque/place and
/torque/modify snap it — an already-legal price passes through unchanged.
"""
from __future__ import annotations

from app.routes import torque as troute
from app.routes.torque import ModifyRequest, PlaceRequest, torque_cfg, torque_modify, torque_place

from .conftest import FakeRequest, FakeTradier

DEV = {"id": "dev-local", "email": "dev@local", "auth": "dev"}


def _single(symbol, occ):
    return [{"symbol": occ, "action": "buy_to_open", "quantity": 1, "side": "call",
             "strike": 100.0, "role": "long"}]


async def _place(symbol, occ, price):
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])
    req = PlaceRequest(symbol=symbol, strategy="long_call", legs=_single(symbol, occ),
                       order_type="limit", limit_price=price, account_id="T", confirm=True,
                       auto_close=False, stop_loss_pct=0)
    await torque_place(req, FakeRequest(client), user=DEV)
    return client.placed[0]["price"]


async def test_penny_pilot_limit_above_three_snaps_to_a_nickel():
    assert await _place("SMH", "SMH300104C00280000", 4.12) == "4.10"


async def test_penny_pilot_limit_below_three_keeps_its_cents():
    assert await _place("XLF", "XLF300104C00050000", 2.37) == "2.37"


async def test_penny_everywhere_ticker_keeps_its_cents_above_three():
    assert await _place("SPY", "SPY300104C00700000", 4.12) == "4.12"


async def test_index_single_leg_above_three_snaps_to_a_dime():
    assert await _place("NDX", "NDXP300104C22000000", 12.37) == "12.40"


async def test_index_single_leg_below_three_snaps_to_a_nickel():
    assert await _place("NDX", "NDXP300104C22000000", 2.37) == "2.35"


async def test_index_spread_keeps_the_base_step_above_three():
    """$0.10-above-$3 is a single-option rule; a spread is a complex order in $0.05."""
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])
    legs = [{"symbol": "NDXP300104C22000000", "action": "buy_to_open", "quantity": 1,
             "side": "call", "strike": 22000.0, "role": "long"},
            {"symbol": "NDXP300104C22100000", "action": "sell_to_open", "quantity": 1,
             "side": "call", "strike": 22100.0, "role": "short"}]
    req = PlaceRequest(symbol="NDX", strategy="bull_call", legs=legs, order_type="limit",
                       limit_price=12.35, account_id="T", confirm=True, auto_close=False,
                       stop_loss_pct=0)
    await torque_place(req, FakeRequest(client), user=DEV)
    assert client.placed[0]["price"] == "12.35"


async def test_modify_of_a_spread_uses_the_base_step():
    client = FakeTradier(order_status={"status": "open"})
    r = await torque_modify("2", ModifyRequest(price=12.35, account_id="T", symbol="NDX",
                                               order_class="multileg"), FakeRequest(client), user=DEV)
    assert r["price"] == 12.35


async def test_modify_snaps_using_the_symbol_the_page_sends():
    client = FakeTradier(order_status={"status": "open"})
    r = await torque_modify("2", ModifyRequest(price=4.12, account_id="T", symbol="SMH"),
                            FakeRequest(client), user=DEV)
    assert r["price"] == 4.10
    assert any(p.get("_modify") == "2" and p.get("price") == 4.10 for p in client.placed)


async def test_modify_snaps_a_torque_close_from_its_own_record(monkeypatch):
    troute._CLOSE_GUARD["9"] = {"symbol": "XLE", "entry_type": "debit", "entry_fill": 2.0,
                                "tick": 0.01, "floor_pct": 1.0}
    try:
        client = FakeTradier(order_status={"status": "open"})
        r = await torque_modify("9", ModifyRequest(price=3.37, account_id="T"),
                                FakeRequest(client), user=DEV)
        assert r["price"] == 3.35
    finally:
        troute._CLOSE_GUARD.pop("9", None)


async def test_modify_without_any_symbol_is_left_as_typed():
    client = FakeTradier(order_status={"status": "open"})
    r = await torque_modify("2", ModifyRequest(price=24.53, account_id="T"), FakeRequest(client), user=DEV)
    assert r["price"] == 24.53


async def test_config_serves_each_tickers_steps_to_the_page():
    c = await torque_cfg()
    assert c["ticks"]["SMH"] == {"tick": 0.01, "tick_above_3": 0.05}
    assert c["ticks"]["SPY"] == {"tick": 0.01, "tick_above_3": None}
    assert c["ticks"]["NDX"] == {"tick": 0.05, "tick_above_3": 0.10}
    assert c["tickers_loaded"] is True


# ── review fix: per-ticker safety rules never fail open ─────────────────────
import pytest                                            # noqa: E402
from fastapi import HTTPException                        # noqa: E402

from app import torque_config as tcfg                    # noqa: E402
from app.routes.torque import BuildRequest, PriceRequest, torque_build, torque_cancel, torque_price  # noqa: E402

DJX_OCC = "DJXW300104C00525000"


def _unload_setup():
    tcfg._SETUP.clear()
    tcfg._SETUP_STATE.update(loaded=False, loaded_at=0.0)


async def _djx_entry_calls(client):
    legs = _single("DJX", DJX_OCC)
    yield "build", lambda: torque_build(BuildRequest(symbol="DJX", strategy="long_call"), FakeRequest(client))
    yield "price", lambda: torque_price(PriceRequest(legs=legs, symbol="DJX"), FakeRequest(client))
    yield "place", lambda: torque_place(PlaceRequest(
        symbol="DJX", strategy="long_call", legs=legs, order_type="market", account_id="T",
        confirm=True, auto_close=False, stop_loss_pct=0), FakeRequest(client), user=DEV)


async def test_new_entries_are_refused_while_the_setup_is_not_loaded():
    """Without the setup, DJX's rules (no market orders, mandatory auto-close,
    45% floor, 50% stop, penny step) would fall back to permissive defaults —
    so build, price and place all refuse with 503 instead, and nothing is sent."""
    _unload_setup()
    client = FakeTradier()
    async for name, call in _djx_entry_calls(client):
        with pytest.raises(HTTPException) as e:
            await call()
        assert e.value.status_code == 503, name
    assert client.placed == []


async def test_new_entries_on_an_unconfigured_or_disabled_ticker_are_refused():
    client = FakeTradier()
    with pytest.raises(HTTPException) as e:
        await torque_build(BuildRequest(symbol="ZZZZ", strategy="long_call"), FakeRequest(client))
    assert e.value.status_code == 400
    rows = [dict(r, enabled=(r["ticker"] != "DJX")) for r in tcfg._SETUP.values()]
    tcfg.apply_ticker_setup(rows)
    async for name, call in _djx_entry_calls(client):
        with pytest.raises(HTTPException) as e:
            await call()
        assert e.value.status_code == 400, name
    assert client.placed == []


async def test_djx_rules_still_apply_when_the_setup_is_loaded():
    client = FakeTradier()
    with pytest.raises(HTTPException) as e:          # market order on DJX is still refused
        await torque_place(PlaceRequest(
            symbol="DJX", strategy="long_call", legs=_single("DJX", DJX_OCC), order_type="market",
            account_id="T", confirm=True, auto_close=True), FakeRequest(client), user=DEV)
    assert e.value.status_code == 400 and "market orders are disabled" in e.value.detail


async def test_exits_on_existing_positions_keep_working_without_the_setup():
    """Cancel and modify are exits/adjustments, not new entries — never blocked.
    A DJX close placed earlier keeps its own penny step and its 45% floor."""
    troute._CLOSE_GUARD["70"] = {"symbol": "DJX", "entry_type": "debit", "entry_fill": 2.0,
                                 "tick": tcfg.tick_size("DJX"), "floor_pct": 45.0,
                                 "spread_scaled": True}
    _unload_setup()
    try:
        client = FakeTradier(order_status={"status": "open"})
        assert (await torque_cancel("71", FakeRequest(client), account_id="T", user=DEV))["order_id"] == "71"
        r = await torque_modify("70", ModifyRequest(price=2.98, account_id="T", symbol="DJX"),
                                FakeRequest(client), user=DEV)
        assert r["price"] == 2.98                     # penny step kept, NOT snapped to 3.00
        with pytest.raises(HTTPException) as e:       # floor (≥45% over a 2.00 fill) still enforced
            await torque_modify("70", ModifyRequest(price=2.10, account_id="T", symbol="DJX"),
                                FakeRequest(client), user=DEV)
        assert e.value.status_code == 400
    finally:
        troute._CLOSE_GUARD.pop("70", None)


async def test_modify_without_the_setup_or_a_record_leaves_the_price_as_typed():
    _unload_setup()
    client = FakeTradier(order_status={"status": "open"})
    r = await torque_modify("2", ModifyRequest(price=2.68, account_id="T", symbol="DJX"),
                            FakeRequest(client), user=DEV)
    assert r["price"] == 2.68                         # never rounded with a guessed default step


def test_index_single_legs_step_a_dime_at_three_dollars():
    for t in ("SPX", "NDX", "RUT"):
        k = tcfg.tick_size(t)
        assert k.at(2.95) == 0.05 and k.at(3.0) == 0.10 and k.at(12.37) == 0.10
