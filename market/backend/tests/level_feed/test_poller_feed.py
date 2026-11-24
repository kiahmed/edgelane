"""Poller side of the level feed: SPY rides the SPX quote call (one batched
request) only when LEVEL_FEED_ENABLED; disabled ⇒ the exact same Tradier calls
as before and nothing new cached. Also covers the clients' stock_quotes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import poller
from app.mock_tradier import MockTradierClient
from app.poller import state as poller_state
from app.tradier_client import TradierClient


class CountingMock(MockTradierClient):
    """MockTradierClient that records every quote call."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    async def stock_quote(self, symbol):
        self.calls.append(("stock_quote", symbol))
        return await super().stock_quote(symbol)

    async def stock_quotes(self, symbols):
        self.calls.append(("stock_quotes", tuple(symbols)))
        return await super().stock_quotes(symbols)


class SingleQuoteOnly:
    """A client with no stock_quotes (e.g. a test fake / older wrapper)."""

    def __init__(self):
        self._m = MockTradierClient()
        self.calls: list[tuple] = []

    async def stock_quote(self, symbol):
        self.calls.append(("stock_quote", symbol))
        return await self._m.stock_quote(symbol)

    async def option_expirations(self, symbol):
        return await self._m.option_expirations(symbol)

    async def options_chain(self, symbol, expiration):
        return await self._m.options_chain(symbol, expiration)


def _settings(enabled: bool):
    return SimpleNamespace(level_feed_enabled=enabled)


def _quote_calls(client):
    return [c for c in client.calls if c[0].startswith("stock_quote")]


# ── enabled ─────────────────────────────────────────────────────────────────

async def test_enabled_spx_uses_one_batched_quote_and_records_feed():
    c = CountingMock()
    await poller._poll_all(["SPX"], c, None, _settings(True), persist=False)
    assert _quote_calls(c) == [("stock_quotes", ("SPX", "SPY"))]
    assert "SPX" in poller_state.latest_by_symbol, poller_state.last_error
    out = poller_state.latest_by_symbol["SPX"]
    contracts = poller_state.latest_contracts_by_symbol["SPX"]
    assert contracts and all("strike" in k and "side" in k for k in contracts)
    assert poller_state.latest_spy_spot == pytest.approx(out["spot"] / 10.0)
    q = poller_state.latest_quote_by_symbol
    assert q["SPX"]["last"] == out["spot"] and q["SPX"]["prevclose"] == 7580.0
    assert q["SPY"]["prevclose"] == pytest.approx(758.0)
    assert not poller_state._pending_feed


async def test_enabled_spot_matches_disabled_spot():
    """SPX spot (and its mock drift) is unchanged by the batched path."""
    a, b = CountingMock(), CountingMock()
    on = await poller.poll_symbol("SPX", a, None, _settings(True), persist=False)
    off = await poller.poll_symbol("SPX", b, None, _settings(False), persist=False)
    assert on["spot"] == off["spot"]
    poller_state._pending_feed.clear()


async def test_enabled_non_spx_keeps_single_quote_call():
    c = CountingMock()
    await poller._poll_all(["NDX"], c, None, _settings(True), persist=False)
    assert _quote_calls(c) == [("stock_quote", "NDX")]
    assert poller_state.latest_contracts_by_symbol.get("NDX")


async def test_enabled_without_stock_quotes_falls_back_to_single_quote():
    c = SingleQuoteOnly()
    poller_state.latest_spy_spot = 999.0      # stale value must not survive
    await poller._poll_all(["SPX"], c, None, _settings(True), persist=False)
    assert c.calls == [("stock_quote", "SPX")]
    assert poller_state.latest_contracts_by_symbol.get("SPX")
    assert poller_state.latest_spy_spot is None


async def test_failed_poll_commits_nothing():
    class Broken(CountingMock):
        async def options_chain(self, symbol, expiration):
            return []
    poller_state.latest_contracts_by_symbol.pop("SPX", None)
    await poller._poll_all(["SPX"], Broken(), None, _settings(True), persist=False)
    assert "SPX" not in poller_state.latest_contracts_by_symbol
    assert not poller_state._pending_feed


# ── disabled ────────────────────────────────────────────────────────────────

async def test_disabled_uses_stock_quote_and_caches_nothing():
    c = CountingMock()
    poller_state.latest_contracts_by_symbol.clear()
    poller_state.latest_quote_by_symbol.clear()
    poller_state.latest_spy_spot = None
    await poller._poll_all(["SPX"], c, None, _settings(False), persist=False)
    assert _quote_calls(c) == [("stock_quote", "SPX")]
    assert "SPX" in poller_state.latest_by_symbol
    assert poller_state.latest_contracts_by_symbol == {}
    assert poller_state.latest_quote_by_symbol == {}
    assert poller_state.latest_spy_spot is None
    assert not poller_state._pending_feed


async def test_settings_without_flag_is_disabled():
    c = CountingMock()
    poller_state.latest_contracts_by_symbol.clear()
    await poller._poll_all(["SPX"], c, None, SimpleNamespace(), persist=False)
    assert _quote_calls(c) == [("stock_quote", "SPX")]
    assert poller_state.latest_contracts_by_symbol == {}


# ── clients ─────────────────────────────────────────────────────────────────

async def test_mock_stock_quotes_shape():
    m = MockTradierClient()
    q = await m.stock_quotes(["spx", "SPY"])
    assert set(q) == {"SPX", "SPY"}
    assert q["SPY"]["last"] == pytest.approx(q["SPX"]["last"] / 10.0)
    assert q["SPX"]["prevclose"] == 7580.0
    # SPX drift counter advanced exactly once, as one stock_quote would.
    ref = MockTradierClient()
    await ref.stock_quote("SPX")
    assert (await m.stock_quote("SPX"))["last"] == (await ref.stock_quote("SPX"))["last"]


@pytest.mark.parametrize("raw,expected", [
    ({"quotes": {"quote": {"symbol": "spx", "last": 1}}}, {"SPX"}),
    ({"quotes": {"quote": [{"symbol": "SPX", "last": 1}, {"symbol": "SPY", "last": 2}]}},
     {"SPX", "SPY"}),
    ({"quotes": {"quote": None, "unmatched_symbols": {"symbol": "ZZZ"}}}, set()),
])
async def test_tradier_stock_quotes_unwraps(monkeypatch, raw, expected):
    seen = {}

    async def fake_get(self, path, params=None, _retried=False):
        seen["path"], seen["params"] = path, params
        return raw

    monkeypatch.setattr(TradierClient, "_get", fake_get)
    c = TradierClient("https://example.invalid", "t")
    out = await c.stock_quotes(["SPX", "spy"])
    assert set(out) == expected
    assert seen["path"] == "markets/quotes"
    assert seen["params"]["symbols"] == "SPX,SPY"
