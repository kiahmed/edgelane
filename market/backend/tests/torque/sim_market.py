"""Reusable "moving market" simulation harness for Torque tests.

Unlike FakeTradier's static/sequenced-by-call-count order statuses, this
drives order fills off a single shared, monotonically-advancing sequence of
scripted package mid-prices — the same price feed backs BOTH `_package_price`
reads (via `sim_package_price`) AND every order-status check
(`SimulatedMarketTradier`). That lets a test script an adverse, fast-moving
market ("you drag the mid away faster than a resting limit can bind") and
assert on the resulting behavior: does a bounded re-quote loop ever catch up,
or does it correctly exhaust and fall back to an unbounded fill?

Not specific to the stop-exit ladder — any Torque code path that polls order
status against a live quote can be tested against a `SimulatedMarket` the same
way (e.g. a future profit-target chase, or the entry-fill poll).

Usage:
    market = SimulatedMarket([1.00, 0.95, 0.90, ...])   # last value repeats
    monkeypatch.setattr(tq, "_package_price", sim_package_price(market))
    client = SimulatedMarketTradier(market, entry_type="debit")
    ... drive the code under test, then assert against client.placed / market
"""
from __future__ import annotations

from .conftest import FakeTradier


class SimulatedMarket:
    """A scripted sequence of package mid-prices consumed one-per-interaction.

    Every call to `advance()` moves to the next price — modeling real time
    passing between successive polls/quotes/order actions, whether they come
    from a `_package_price` lookup or an order-status check. Once the script
    is exhausted, the market "goes quiet": `advance()` keeps returning the
    last value, which is exactly what a test needs to assert "it finally
    catches once the price stabilizes."
    """

    def __init__(self, prices: list[float]):
        if not prices:
            raise ValueError("SimulatedMarket needs at least one scripted price")
        self._prices = list(prices)
        self._idx = 0

    def advance(self) -> float:
        if self._idx < len(self._prices) - 1:
            self._idx += 1
        return self.current()

    def current(self) -> float:
        return self._prices[self._idx]


def sim_package_price(market: SimulatedMarket, *, spread: float = 0.04):
    """Drop-in replacement for `troute._package_price` backed by `market`.
    Each call advances the market by one step (same cadence a real re-quote
    fetch consumes) and derives a two-sided quote around it."""
    async def _price(client, symbol, legs):
        mid = market.advance()
        half = round(spread / 2, 4)
        return {"complete": True, "net_bid": round(mid - half, 2),
                "net_ask": round(mid + half, 2), "abs_mid": round(mid, 2)}
    return _price


class SimulatedMarketTradier(FakeTradier):
    """A FakeTradier whose order fills are decided against a shared
    SimulatedMarket instead of a canned/sequenced status — a resting limit
    fills once the market's current price actually crosses it, and every
    broker interaction (get_order/cancel_order/modify_order) advances the
    market by one step first, so a fill "landing" between polls is visible
    exactly where the real broker would report it.

    `entry_type` drives fill direction, matching how the real close payload
    is built (`_build_close_payload`/`_multileg_close_type`): closing a debit
    position sells to close (marketable once mid rises to/through the limit);
    closing a credit position buys to close (marketable once mid falls
    to/through the limit). A market order (or a limit with no price) always
    fills immediately at the market's current price.
    """

    def __init__(self, market: SimulatedMarket, *, entry_type: str = "debit", **kw):
        super().__init__(**kw)
        self.market = market
        self.entry_type = entry_type
        self._next_id = 5000
        self._limits: dict[str, float] = {}

    def _crosses(self, limit_price: float) -> bool:
        mid = self.market.current()
        return mid <= limit_price if self.entry_type == "credit" else mid >= limit_price

    def _snapshot(self, order_id, *, status: str, exec_qty: float, qty: float):
        self._order_status_by_id[str(order_id)] = {
            "id": order_id, "status": status,
            "avg_fill_price": self.market.current() if status == "filled" else None,
            "exec_quantity": exec_qty, "remaining_quantity": max(qty - exec_qty, 0.0),
        }

    def _resolve(self, order_id):
        """Re-check a resting limit against the market's CURRENT price and
        flip it to filled if it now crosses. Called before every read or
        mutation so a fill landing between two polls is never missed."""
        key = str(order_id)
        st = self._order_status_by_id.get(key)
        if st and str(st.get("status")) == "open" and key in self._limits:
            qty = (st.get("exec_quantity") or 0.0) + (st.get("remaining_quantity") or 0.0)
            if self._crosses(self._limits[key]):
                self._snapshot(order_id, status="filled", exec_qty=qty, qty=qty)

    async def place_order(self, account_id, payload):
        self.placed.append(payload)
        order_id = self._next_id
        self._next_id += 1
        qty = float(payload.get("quantity") or 1)
        price = payload.get("price")
        limit_price = float(price) if price is not None else None
        self.market.advance()
        if payload.get("type") == "market" or limit_price is None:
            self._snapshot(order_id, status="filled", exec_qty=qty, qty=qty)
        else:
            self._limits[str(order_id)] = limit_price
            self._snapshot(order_id, status="open", exec_qty=0.0, qty=qty)
            self._resolve(order_id)
        return {"order": {"id": order_id, "status": "ok"}}

    async def get_order(self, account_id, order_id):
        self.market.advance()
        self._resolve(order_id)
        return await super().get_order(account_id, order_id)

    async def cancel_order(self, account_id, order_id):
        self.market.advance()
        self._resolve(order_id)   # a fill landing right before cancel must win, like the real race
        result = await super().cancel_order(account_id, order_id)
        self._limits.pop(str(order_id), None)
        return result

    async def modify_order(self, account_id, order_id, price=None, order_type=None,
                           duration=None, stop=None):
        self.market.advance()
        self._resolve(order_id)   # already-filled check happens in the base class
        result = await super().modify_order(account_id, order_id, price=price,
                                            order_type=order_type, duration=duration, stop=stop)
        if price is not None:
            key = str(order_id)
            self._limits[key] = float(price)
            self._resolve(order_id)
        return result
