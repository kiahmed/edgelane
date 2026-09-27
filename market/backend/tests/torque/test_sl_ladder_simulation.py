"""Automated SL-ladder simulation: a moving market, not a single static quote.

Answers the question that motivated `sim_market.py`: if the mid keeps running
away faster than the stop-exit ladder can re-quote and bind, does it correctly
exhaust after STOP_MAX_REQUOTES and fall back to a guaranteed market exit? And
if the market stabilizes partway through, does the ladder actually catch the
fill instead of needlessly escalating? Both driven off the same reusable
`SimulatedMarket` harness — see sim_market.py for the design.
"""
from __future__ import annotations

from app import torque_config as tc
from app.routes import torque as tq

from .sim_market import SimulatedMarket, SimulatedMarketTradier, sim_package_price


async def _run_ladder_against(monkeypatch, prices, *, entry_type="debit",
                              entry_fill=1.00, stop_pct=10.0, tick=0.05, quantity=1):
    """Shared drive: wires a SimulatedMarket into both `_package_price` and a
    SimulatedMarketTradier, then runs `_monitor_stop` to completion (a single
    breach-to-resolution cycle) exactly like the real watcher does."""
    monkeypatch.setattr(tq, "_STOP_INTERVAL", 0.0)
    market = SimulatedMarket(prices)
    monkeypatch.setattr(tq, "_package_price", sim_package_price(market))
    client = SimulatedMarketTradier(market, entry_type=entry_type)
    w = {}
    legs = [{"symbol": "NDXP123", "action": "buy_to_open", "quantity": 1}]
    await tq._monitor_stop(
        client, "T", w, legs=legs, symbol="NDX", strategy="long_call", is_single=True,
        entry_type=entry_type, entry_fill=entry_fill, stop_pct=stop_pct, tick=tick,
        quantity=quantity, close_order_id=None)
    return client, w, market


def _real_orders(client):
    return [p for p in client.placed if "_modify" not in p]


def _modifies(client):
    return [p for p in client.placed if "_modify" in p]


async def test_ladder_exhausts_to_market_when_price_never_stops_running_away(monkeypatch):
    """A relentless adverse move: every single re-quote is stale by the time
    it's placed, because the market keeps advancing on every poll/modify too.
    The ladder must chase for exactly STOP_MAX_REQUOTES rounds, bounded the
    whole time, then give up on price and guarantee the exit at market."""
    # Falls fast forever — each step (0.10) is well clear of a single tick's
    # (0.05) rounding slack, so a re-quote can never accidentally land on a
    # price the market has already fallen through by the time it's placed.
    prices = [round(2.00 - 0.10 * i, 2) for i in range(60)]
    client, w, market = await _run_ladder_against(
        monkeypatch, prices, entry_fill=2.00, stop_pct=10.0)

    assert w["state"] == "stop_market_placed"
    assert w["stop_market_fallback"] is True
    assert "no bounded fill after" in w["stop_note"]

    modifies = _modifies(client)
    real_orders = _real_orders(client)
    assert len(modifies) == tc.STOP_MAX_REQUOTES     # chased the same order every round, never caught
    assert len(real_orders) == 2                     # initial bounded limit + the final market fallback
    assert real_orders[0]["type"] == "limit"
    assert real_orders[1]["type"] == "market" and "price" not in real_orders[1]
    assert real_orders[1]["tag"] == "torqueStopMktlongcall"


async def test_ladder_catches_the_fill_once_the_market_slows_down(monkeypatch):
    """The same relentless drop, but it stabilizes partway through (the
    scripted sequence flattens, and SimulatedMarket repeats the last value
    forever once exhausted). Once price stops moving, the very next re-quote
    lands exactly on the now-static market and must fill — no market
    fallback, no full STOP_MAX_REQUOTES exhaustion."""
    falling = [round(1.00 - 0.01 * i, 2) for i in range(15)]
    prices = falling + [falling[-1]] * 50   # flattens out and stays put
    client, w, market = await _run_ladder_against(monkeypatch, prices)

    assert w["state"] == "stop_filled"
    assert w.get("stop_market_fallback") is not True
    real_orders = _real_orders(client)
    assert all(o["type"] != "market" for o in real_orders)   # never needed the guaranteed exit
    assert len(_modifies(client)) < tc.STOP_MAX_REQUOTES     # caught it before exhausting the ladder


async def test_ladder_catches_immediately_when_price_is_already_calm(monkeypatch):
    """Sanity check for the harness itself: a flat market from the start
    should behave like the existing static-price tests — one bounded limit,
    filled promptly, no requoting at all."""
    prices = [0.62] * 20
    client, w, market = await _run_ladder_against(monkeypatch, prices)

    assert w["state"] == "stop_filled"
    assert len(_modifies(client)) == 0
    assert len(_real_orders(client)) == 1


async def test_credit_close_fills_on_a_rising_buyback_cost_stabilizing(monkeypatch):
    """Direction check: a credit position's stop exit BUYS to close, so it's
    marketable once the (rising) buyback cost stops climbing — mirror image
    of the debit scenarios above, exercising the harness's other branch."""
    rising = [round(1.00 + 0.01 * i, 2) for i in range(15)]
    prices = rising + [rising[-1]] * 50
    client, w, market = await _run_ladder_against(
        monkeypatch, prices, entry_type="credit", stop_pct=10.0)

    assert w["state"] == "stop_filled"
    assert w.get("stop_market_fallback") is not True
