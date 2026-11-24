"""Spot for strike selection is the CURRENT price — never yesterday's close
(2026-11-24: pre-market Reflex puts were struck off Tradier `last` = prevclose)."""
import time

import pytest
from fastapi import HTTPException

from app import broker_resolver
from app.routes import torque as troute
from app import torque_engine as teng
from tests.torque.conftest import FakeTradier

_ORIG_YAHOO = troute._yahoo_spot     # captured before the autouse fixture stubs it


def _premarket(monkeypatch):
    async def _no(client):
        return False
    monkeypatch.setattr(troute, "_regular_session_now", _no)


def _q(*, bid_age=5, trade_age=13 * 3600, bid=714.19, ask=714.29, last=722.29):
    now = time.time()
    return {"last": last, "prevclose": last, "bid": bid, "ask": ask,
            "bid_date": (now - bid_age) * 1000, "ask_date": (now - bid_age) * 1000,
            "trade_date": (now - trade_age) * 1000}


async def test_premarket_uses_fresh_bid_ask_mid_not_stale_last(monkeypatch):
    _premarket(monkeypatch)
    async def _yh(sym):
        return 722.29                                   # Yahoo = prior close pre-market
    monkeypatch.setattr(troute, "_yahoo_spot", _yh)
    spot, src = await troute._live_spot(FakeTradier(), "SPY", _q(), [])
    assert src == "bid_ask_mid" and spot == pytest.approx(714.24)


async def test_premarket_stale_bid_ask_falls_to_time_and_sales(monkeypatch):
    _premarket(monkeypatch)
    async def _ts(client, sym, now):
        return 714.40
    monkeypatch.setattr(troute, "_timesale_price", _ts)
    spot, src = await troute._live_spot(FakeTradier(), "SPY", _q(bid_age=900), [])
    assert (spot, src) == (714.40, "timesales")


async def test_premarket_never_uses_parity(monkeypatch):
    """Stale bid/ask, no time & sales, parity available from (possibly stale)
    option quotes → still nothing: the caller refuses with 503."""
    _premarket(monkeypatch)
    async def _ts(client, sym, now):
        return None
    monkeypatch.setattr(troute, "_timesale_price", _ts)
    monkeypatch.setattr(teng, "implied_spot", lambda contracts, fallback=None: 722.1)
    assert await troute._live_spot(FakeTradier(), "SPY", _q(bid_age=900), []) == (None, "none")


async def test_premarket_stale_everything_gives_503(monkeypatch):
    _premarket(monkeypatch)
    troute._CHAIN_CACHE.clear(); troute._CHAIN_FAIL.clear()
    async def _ts(client, sym, now):
        return None
    monkeypatch.setattr(troute, "_timesale_price", _ts)
    class _Stale(FakeTradier):
        async def stock_quote(self, symbol):
            return _q(bid_age=900)
    with pytest.raises(HTTPException) as ei:
        await troute._get_chain(_Stale(), "SPY")
    assert ei.value.status_code == 503
    troute._CHAIN_CACHE.clear(); troute._CHAIN_FAIL.clear()


async def test_get_chain_refuses_without_a_fresh_price(monkeypatch):
    troute._CHAIN_CACHE.clear(); troute._CHAIN_FAIL.clear()
    async def _none(client, sym, q, contracts):
        return None, "none"
    monkeypatch.setattr(troute, "_live_spot", _none)
    with pytest.raises(HTTPException) as ei:
        await troute._get_chain(FakeTradier(), "NDX")
    assert ei.value.status_code == 503 and "no fresh price" in ei.value.detail
    troute._CHAIN_CACHE.clear(); troute._CHAIN_FAIL.clear()


async def test_regular_session_prefers_yahoo(monkeypatch):
    async def _yh(sym):
        return 714.5
    monkeypatch.setattr(troute, "_yahoo_spot", _yh)
    assert await troute._live_spot(FakeTradier(), "SPY", _q(), []) == (714.5, "yahoo")


def test_fresh_helpers_reject_stale_and_bad_quotes():
    now = time.time()
    assert troute._quote_mid_if_fresh(_q(bid_age=500), now) is None
    assert troute._quote_mid_if_fresh(_q(bid=0), now) is None
    assert troute._quote_mid_if_fresh(_q(bid=715, ask=714), now) is None        # crossed
    assert troute._last_if_fresh(_q(), now) is None                            # last from yesterday
    assert troute._last_if_fresh(_q(trade_age=10), now) == 722.29


def test_strike_guard_allows_one_strike_either_side_only():
    contracts = [{"side": "put", "strike": k} for k in (712, 713, 714, 715, 716, 722)]
    spot = 714.24                                            # nearest strike 714
    assert not troute.strike_too_far(spot, contracts, "put", 714)
    assert not troute.strike_too_far(spot, contracts, "put", 713)
    assert not troute.strike_too_far(spot, contracts, "put", 715)
    assert troute.strike_too_far(spot, contracts, "put", 716)
    assert troute.strike_too_far(spot, contracts, "put", 722)               # the 2026-11-24 strike
    assert troute.strike_too_far(spot, contracts, "put", 719)               # not listed


async def test_reflex_guard_uses_an_independent_fresh_price(monkeypatch):
    """The build's cached spot was stale (722.29) so it chose 722 ATM; the guard
    re-reads a FRESH price (714.24) and refuses."""
    async def _chain(client, sym):
        return 722.29, "2026-11-24", [{"side": "put", "strike": k} for k in (713, 714, 715, 722)]
    monkeypatch.setattr(troute, "_get_chain", _chain)
    class _C(FakeTradier):
        async def stock_quote(self, symbol):
            return _q()
    from tests.torque.conftest import FakeRequest
    req = FakeRequest(_C())
    r = await troute._strike_too_far(req, "SPY", {"legs": [{"side": "put", "strike": 722}]})
    assert r and "live price 714.24 (bid_ask_mid)" in r
    assert await troute._strike_too_far(req, "SPY", {"legs": [{"side": "put", "strike": 714}]}) is None


async def test_reflex_guard_refuses_without_an_independent_fresh_price(monkeypatch):
    async def _ts(client, sym, now):
        return None
    monkeypatch.setattr(troute, "_timesale_price", _ts)
    class _C(FakeTradier):
        async def stock_quote(self, symbol):
            return _q(bid_age=900)
    from tests.torque.conftest import FakeRequest
    r = await troute._strike_too_far(FakeRequest(_C()), "SPY", {"legs": [{"side": "put", "strike": 714}]})
    assert r and "no fresh SPY price" in r


async def test_yahoo_ignored_when_its_print_is_stale(monkeypatch):
    class _Resp:
        def __init__(self, meta): self._m = meta
        def raise_for_status(self): pass
        def json(self): return {"chart": {"result": [{"meta": self._m}]}}
    class _Cli:
        def __init__(self, meta): self.meta = meta
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp(self.meta)
    monkeypatch.setattr(troute.tcfg, "yahoo_symbol", lambda t: "SPY")
    monkeypatch.setattr(troute.httpx, "AsyncClient", lambda **k: _Cli({"regularMarketPrice": 722.29,
                                                                      "regularMarketTime": time.time() - 3600,
                                                                      "previousClose": 722.29}))
    assert await _ORIG_YAHOO("SPY") is None
    monkeypatch.setattr(troute.httpx, "AsyncClient", lambda **k: _Cli({"regularMarketPrice": 714.5,
                                                                      "regularMarketTime": time.time() - 5}))
    assert await _ORIG_YAHOO("SPY") == 714.5


async def test_resolve_client_for_uid_fills_a_missing_account(monkeypatch):
    async def _cfg(uid):
        return {"broker": "tradier", "tradier_token": "t", "tradier_env": "production"}
    monkeypatch.setattr(broker_resolver.supabase_admin, "get_broker_config", _cfg)
    async def _acct(self):
        return "ACC123"
    monkeypatch.setattr(broker_resolver.TradierClient, "resolve_account_id", _acct)
    broker, client, account = await broker_resolver.resolve_client_for_uid("u1")
    assert broker == "tradier" and account == "ACC123"
    await client.close()


async def test_resolve_client_for_uid_none_when_account_unresolvable(monkeypatch):
    async def _cfg(uid):
        return {"broker": "tradier", "tradier_token": "t"}
    monkeypatch.setattr(broker_resolver.supabase_admin, "get_broker_config", _cfg)
    async def _boom(self):
        raise RuntimeError("profile failed")
    monkeypatch.setattr(broker_resolver.TradierClient, "resolve_account_id", _boom)
    assert await broker_resolver.resolve_client_for_uid("u1") is None
