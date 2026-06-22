"""Torque routes + the place→confirm→close flow (every branch)."""
from __future__ import annotations

import pytest

from app.routes import torque as troute
from app.routes.torque import (
    BuildRequest, PriceRequest, PlaceRequest,
    torque_cfg, torque_analyze, torque_build, torque_price, torque_place, torque_order,
    torque_orders, torque_cancel,
)
from fastapi import HTTPException

from .conftest import FakeTradier, FakeRequest


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    monkeypatch.setattr(troute, "_POLL_TIMEOUT", 0.3)
    monkeypatch.setattr(troute, "_POLL_INTERVAL", 0.05)
    monkeypatch.setattr(troute, "_WATCH_TIMEOUT", 0.3)
    monkeypatch.setattr(troute, "_WATCH_INTERVAL", 0.05)
    monkeypatch.setattr(troute, "_CLOSE_RETRY_DELAY", 0.01)


@pytest.fixture(autouse=True)
def _clear_watchers():
    troute._WATCHERS.clear()
    yield
    troute._WATCHERS.clear()


async def _await_watch(r):
    """Drive the background auto-close watcher to completion and return its state."""
    wid = r.get("watch_id")
    assert wid in troute._WATCHERS, "no watcher registered"
    await troute._WATCHERS[wid]["task"]
    return troute._WATCHERS[wid]


async def _build_legs(client, symbol, strategy):
    b = await torque_build(BuildRequest(symbol=symbol, strategy=strategy), FakeRequest(client))
    return b["legs"], b


# ── config / analyze / build / price ───────────────────────────────────────
async def test_config_endpoint():
    c = await torque_cfg()
    assert c["tickers"][0] == "NDX"
    assert c["default_strategy"] == "bull_call"
    assert len(c["strategies"]) == 10


async def test_analyze_endpoint():
    a = await torque_analyze("NDX", FakeRequest(FakeTradier()))
    assert a["symbol"] == "NDX"
    assert "lean" in a and "suggested_side" in a and a["expiration"]


async def test_yahoo_spot_is_primary_over_parity(monkeypatch):
    # when Yahoo returns a price it wins; parity is only the fallback
    async def _yh(sym):
        return 29999.0
    monkeypatch.setattr(troute, "_yahoo_spot", _yh)
    a = await torque_analyze("NDX", FakeRequest(FakeTradier(spot=22000.0)))
    assert a["spot"] == 29999.0


async def test_parity_fallback_when_yahoo_down(monkeypatch):
    # Yahoo returns None → spot comes from Tradier parity (~22000 chain), not None
    async def _none(sym):
        return None
    monkeypatch.setattr(troute, "_yahoo_spot", _none)
    a = await torque_analyze("NDX", FakeRequest(FakeTradier(spot=22000.0)))
    assert a["spot"] is not None and 21000 <= a["spot"] <= 23000


async def test_build_endpoint_resolves_symbols():
    legs, b = await _build_legs(FakeTradier(), "NDX", "bull_call")
    assert len(legs) == 2 and all(l["symbol"] for l in legs)
    assert b["type"] == "debit" and b["suggested_limit"] > 0
    assert b["missing_legs"] == []
    assert all("grid" in l for l in legs)   # stepper neighbors attached


async def test_build_rejects_unknown_strategy():
    with pytest.raises(HTTPException) as e:
        await torque_build(BuildRequest(symbol="NDX", strategy="bogus"), FakeRequest(FakeTradier()))
    assert e.value.status_code == 400


async def test_price_endpoint():
    legs, _ = await _build_legs(FakeTradier(), "NDX", "bull_call")
    p = await torque_price(PriceRequest(legs=legs), FakeRequest(FakeTradier()))
    assert p["type"] == "debit" and p["complete"] is True


# ── place: validation guards ───────────────────────────────────────────────
async def test_place_requires_confirm():
    legs, _ = await _build_legs(FakeTradier(), "NDX", "bull_call")
    req = PlaceRequest(symbol="NDX", strategy="bull_call", legs=legs,
                       order_type="limit", limit_price=10, account_id="T", confirm=False)
    with pytest.raises(HTTPException) as e:
        await torque_place(req, FakeRequest(FakeTradier()))
    assert e.value.status_code == 400


async def test_dry_run_previews_both_and_places_nothing():
    client = FakeTradier(place_responses=[
        {"order": {"status": "ok", "result": True, "cost": 2000.0}},   # entry preview
        {"order": {"status": "ok", "result": True, "cost": 0.0}},      # close preview
    ])
    legs, _ = await _build_legs(client, "NDX", "bull_call")
    req = PlaceRequest(symbol="NDX", strategy="bull_call", legs=legs, order_type="limit",
                       limit_price=20.0, auto_close=True, account_id="T", dry_run=True)
    r = await torque_place(req, FakeRequest(client))
    assert r["mode"] == "dry_run"
    assert r["entry_ok"] is True and r["close_ok"] is True
    assert r["close_target_price"] == 26.0
    # both submitted payloads carried preview=true (nothing executed)
    assert len(client.placed) == 2
    assert all(p.get("preview") == "true" for p in client.placed)


async def test_dry_run_skips_confirm_requirement():
    client = FakeTradier()
    legs, _ = await _build_legs(client, "NDX", "bull_call")
    req = PlaceRequest(symbol="NDX", strategy="bull_call", legs=legs, order_type="market",
                       account_id="T", dry_run=True, confirm=False)
    r = await torque_place(req, FakeRequest(client))   # must NOT raise
    assert r["mode"] == "dry_run"


async def test_place_rejects_unresolved_legs():
    req = PlaceRequest(symbol="NDX", strategy="bull_call",
                       legs=[{"action": "buy_to_open", "quantity": 1, "side": "call", "strike": 1}],
                       order_type="market", account_id="T", confirm=True)
    with pytest.raises(HTTPException):
        await torque_place(req, FakeRequest(FakeTradier()))


# ── place: multileg confirm-then-close ─────────────────────────────────────
async def test_multileg_filled_then_close_placed():
    client = FakeTradier(place_responses=[
        {"order": {"id": 1, "status": "ok"}},     # entry accepted
        {"order": {"id": 2, "status": "ok"}},     # close accepted
    ], order_status={"status": "filled", "avg_fill_price": 20.0, "remaining_quantity": 0.0})
    legs, _ = await _build_legs(client, "NDX", "bull_call")
    req = PlaceRequest(symbol="NDX", strategy="bull_call", legs=legs, order_type="limit",
                       limit_price=20.0, quantity=1, auto_close=True, close_target_pct=30,
                       account_id="T", confirm=True)
    r = await torque_place(req, FakeRequest(client))
    assert r["mode"] == "watching_fill"        # returns immediately, doesn't block
    assert r["rejected"] is False
    w = await _await_watch(r)                  # background watcher fills + closes
    assert w["state"] == "close_placed"
    assert w["close_target_price"] == 26.0     # 20.0 × 1.30
    assert len(client.placed) == 2             # entry + close both sent


async def test_multileg_rejected_no_close():
    client = FakeTradier(place_responses=[
        {"order": {"status": "rejected", "reason_description": "insufficient buying power"}},
    ])
    legs, _ = await _build_legs(client, "NDX", "bull_call")
    req = PlaceRequest(symbol="NDX", strategy="bull_call", legs=legs, order_type="limit",
                       limit_price=20.0, auto_close=True, account_id="T", confirm=True)
    r = await torque_place(req, FakeRequest(client))
    assert r["rejected"] is True
    assert r["close_placed"] is False
    assert r["close"] is None
    assert len(client.placed) == 1             # ONLY the entry — no naked close


async def test_multileg_not_filled_no_close():
    # entry accepted but never fills (status stays 'open') → close skipped
    client = FakeTradier(place_responses=[{"order": {"id": 9, "status": "ok"}}],
                         order_status={"status": "open", "remaining_quantity": 1.0})
    legs, _ = await _build_legs(client, "NDX", "bull_call")
    req = PlaceRequest(symbol="NDX", strategy="bull_call", legs=legs, order_type="limit",
                       limit_price=20.0, auto_close=True, account_id="T", confirm=True)
    r = await torque_place(req, FakeRequest(client))
    assert r["mode"] == "watching_fill"
    w = await _await_watch(r)
    assert w["state"] == "entry_not_filled"    # never filled → close NOT placed
    assert len(client.placed) == 1             # only the entry


async def test_credit_spread_close_is_debit_buyback():
    client = FakeTradier(place_responses=[
        {"order": {"id": 1, "status": "ok"}}, {"order": {"id": 2, "status": "ok"}},
    ], order_status={"status": "filled", "avg_fill_price": 10.0, "remaining_quantity": 0.0})
    legs, _ = await _build_legs(client, "NDX", "iron_condor")
    req = PlaceRequest(symbol="NDX", strategy="iron_condor", legs=legs, order_type="limit",
                       limit_price=10.0, auto_close=True, close_target_pct=30,
                       account_id="T", confirm=True)
    r = await torque_place(req, FakeRequest(client))
    w = await _await_watch(r)
    assert w["state"] == "close_placed"
    assert w["close_target_price"] == 7.0      # credit: buy back at 0.70×
    assert client.placed[1]["type"] == "debit"  # closing a credit spread is a debit


# ── place: single-leg OTO bracket ──────────────────────────────────────────
async def test_single_leg_limit_autoclose_uses_oto():
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])
    legs, _ = await _build_legs(client, "NDX", "long_call")
    req = PlaceRequest(symbol="NDX", strategy="long_call", legs=legs, order_type="limit",
                       limit_price=70.0, auto_close=True, close_target_pct=30,
                       account_id="T", confirm=True)
    r = await torque_place(req, FakeRequest(client))
    assert r["mode"] == "oto_bracket"
    assert r["close_target_price"] == 91.0     # 70 × 1.30
    p = client.placed[0]
    assert p["class"] == "oto"
    assert p["side[0]"] == "buy_to_open" and p["side[1]"] == "sell_to_close"
    assert len(client.placed) == 1             # one atomic bracket


async def test_single_leg_market_autoclose_falls_back_to_confirm():
    # market entry can't be an OTO first leg → confirm-then-close path
    client = FakeTradier(place_responses=[
        {"order": {"id": 1, "status": "ok"}}, {"order": {"id": 2, "status": "ok"}},
    ], order_status={"status": "filled", "avg_fill_price": 70.0, "remaining_quantity": 0.0})
    legs, _ = await _build_legs(client, "NDX", "long_call")
    req = PlaceRequest(symbol="NDX", strategy="long_call", legs=legs, order_type="market",
                       auto_close=True, account_id="T", confirm=True)
    r = await torque_place(req, FakeRequest(client))
    assert r["mode"] == "watching_fill"        # market entry → background watch+close
    w = await _await_watch(r)
    assert w["state"] == "close_placed"


# ── place: no auto-close ───────────────────────────────────────────────────
async def test_no_autoclose_single_order():
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])
    legs, _ = await _build_legs(client, "NDX", "bull_call")
    req = PlaceRequest(symbol="NDX", strategy="bull_call", legs=legs, order_type="market",
                       auto_close=False, account_id="T", confirm=True)
    r = await torque_place(req, FakeRequest(client))
    assert r["auto_close"] is False
    assert r["close_placed"] is False
    assert len(client.placed) == 1


# ── order status passthrough ───────────────────────────────────────────────
async def test_order_status_endpoint():
    client = FakeTradier(order_status={"status": "filled", "avg_fill_price": 5.0})
    o = await torque_order("123", FakeRequest(client), account_id="T")
    assert o["status"] == "filled" and o["id"] == "123"


# ── orders panel + cancel + modify ─────────────────────────────────────────
async def test_orders_endpoint_returns_only_working():
    client = FakeTradier(orders=[
        {"id": 1, "symbol": "NDX", "class": "multileg", "type": "debit", "status": "filled",
         "quantity": 2, "exec_quantity": 2, "avg_fill_price": 16.0, "tag": "torquebullcall",
         "create_date": "2026-06-18T18:51:25Z"},
        {"id": 2, "symbol": "NDX", "class": "multileg", "type": "credit", "status": "open",
         "quantity": 2, "exec_quantity": 0, "price": 20.0, "tag": "torqueClosebullcall",
         "create_date": "2026-06-18T18:59:55Z"},
        {"id": 3, "symbol": "SPY", "class": "option", "type": "limit", "status": "canceled",
         "quantity": 1, "create_date": "2026-06-18T17:00:00Z"},
    ])
    r = await torque_orders(FakeRequest(client), account_id="T")
    assert len(r["orders"]) == 1                # only the OPEN one (no filled/canceled)
    assert r["orders"][0]["id"] == "2" and r["orders"][0]["working"] is True


async def test_orders_endpoint_shows_pending_watcher():
    client = FakeTradier(orders=[])
    troute._WATCHERS["99"] = {"entry_order_id": "99", "symbol": "NDX", "strategy": "bull_call",
                              "state": "watching_fill", "close_target_price": 26.0, "done": False}
    r = await torque_orders(FakeRequest(client), account_id="T")
    assert len(r["watchers"]) == 1 and r["watchers"][0]["state"] == "watching_fill"


async def test_cancel_endpoint():
    client = FakeTradier()
    r = await torque_cancel("2", FakeRequest(client), account_id="T")
    assert r["order_id"] == "2"


async def test_modify_endpoint_changes_price():
    from app.routes.torque import torque_modify, ModifyRequest
    client = FakeTradier()
    r = await torque_modify("2", ModifyRequest(price=24.5, account_id="T"), FakeRequest(client))
    assert r["order_id"] == "2"
    assert any(p.get("_modify") == "2" and p.get("price") == 24.5 for p in client.placed)


async def test_autoclose_retries_when_position_not_settled():
    # first close attempt rejected (position not ready), second succeeds
    client = FakeTradier(place_responses=[
        {"order": {"id": 1, "status": "ok"}},                                   # entry
        {"order": {"status": "rejected", "reason_description": "no position to close"}},  # close try 1
        {"order": {"id": 3, "status": "ok"}},                                   # close try 2
    ], order_status={"status": "filled", "avg_fill_price": 20.0, "remaining_quantity": 0.0})
    legs, _ = await _build_legs(client, "NDX", "bull_call")
    req = PlaceRequest(symbol="NDX", strategy="bull_call", legs=legs, order_type="limit",
                       limit_price=20.0, auto_close=True, account_id="T", confirm=True)
    r = await torque_place(req, FakeRequest(client))
    w = await _await_watch(r)
    assert w["state"] == "close_placed"
    assert len(client.placed) == 3              # entry + 2 close attempts
