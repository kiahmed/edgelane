"""Shared fixtures for Torque tests: synthetic chains + fake Tradier clients."""
from __future__ import annotations

import pytest

from app import torque_engine as teng
from app.routes import torque as troute


def occ(root: str, exp: str, side: str, strike: float) -> str:
    yymmdd = exp[2:].replace("-", "")
    cp = "C" if side == "call" else "P"
    return f"{root}{yymmdd}{cp}{int(round(strike * 1000)):08d}"


def raw_chain(spot=22000.0, step=25, span=1000, exp="2026-06-18", root="NDXP",
              price_fn=None, call_oi=None, put_oi=None, call_vol=None, put_vol=None):
    """Raw Tradier-shaped option rows around `spot`. Callables take (strike,side)."""
    price_fn = price_fn or (lambda k, s: max(0.5, 300 - abs(k - spot) * 0.05))
    call_oi = call_oi or (lambda k: 1000)
    put_oi = put_oi or (lambda k: 1000)
    call_vol = call_vol or (lambda k: 500)
    put_vol = put_vol or (lambda k: 500)
    rows = []
    lo, hi = int(spot - span), int(spot + span)
    for k in range(lo, hi + 1, step):
        for side in ("call", "put"):
            mid = price_fn(float(k), side)
            oi = call_oi(k) if side == "call" else put_oi(k)
            vol = call_vol(k) if side == "call" else put_vol(k)
            rows.append({
                "symbol": occ(root, exp, side, k),
                "strike": float(k),
                "option_type": side,
                "bid": round(mid - 1, 2),
                "ask": round(mid + 1, 2),
                "last": mid,
                "open_interest": oi,
                "volume": vol,
                "greeks": {"mid_iv": 0.2, "delta": 0.5 if side == "call" else -0.5},
            })
    return rows


@pytest.fixture
def spot():
    return 22000.0


@pytest.fixture
def chain(spot):
    """Normalized contracts (what build_structure/analyze consume)."""
    return teng.normalize_chain(raw_chain(spot))


class FakeTradier:
    """Configurable async stand-in for TradierClient used in route tests."""
    def __init__(self, spot=22000.0, exp="2026-06-18", raw=None,
                 place_responses=None, order_status=None, orders=None):
        self._spot = spot
        self._exp = exp
        self._raw = raw if raw is not None else raw_chain(spot, exp=exp)
        self._place = list(place_responses or [])
        self._orders = list(orders or [])
        self._order_status = order_status or {
            "id": 1, "status": "filled", "avg_fill_price": 1.0,
            "exec_quantity": 1.0, "remaining_quantity": 0.0, "class": "multileg",
        }
        self.placed = []          # records every payload submitted

    async def stock_quote(self, symbol):
        return {"symbol": symbol.upper(), "last": self._spot, "close": self._spot}

    async def option_expirations(self, symbol):
        return [self._exp]

    async def options_chain(self, symbol, expiration, greeks=True):
        return self._raw

    async def quotes(self, symbols, greeks=False):
        if isinstance(symbols, str):
            symbols = symbols.split(",")
        by = {r["symbol"]: r for r in self._raw}
        out = []
        for s in symbols:
            r = by.get(s, {})
            out.append({"symbol": s, "bid": r.get("bid", 1.0), "ask": r.get("ask", 1.2)})
        return out

    async def place_order(self, account_id, payload):
        self.placed.append(payload)
        if self._place:
            return self._place.pop(0)
        return {"order": {"id": 1000 + len(self.placed), "status": "ok"}}

    async def get_order(self, account_id, order_id):
        return dict(self._order_status, id=order_id)

    async def get_orders(self, account_id):
        return list(getattr(self, "_orders", []) or [])

    async def cancel_order(self, account_id, order_id):
        return {"order": {"id": order_id, "status": "ok"}}

    async def modify_order(self, account_id, order_id, price=None, order_type=None,
                           duration=None, stop=None):
        self.placed.append({"_modify": order_id, "price": price})
        return {"order": {"id": order_id, "status": "ok", "price": price}}


class FakeRequest:
    def __init__(self, client):
        self.app = type("A", (), {"state": type("S", (), {"tradier": client})()})()


@pytest.fixture(autouse=True)
def _clear_chain_cache():
    troute._CHAIN_CACHE.clear()
    troute._CHAIN_FAIL.clear()      # negative cache — also reset so failures don't leak between tests
    troute._MKT_CACHE.update(t=0.0, state=None, open=None)
    yield
    troute._CHAIN_CACHE.clear()
    troute._CHAIN_FAIL.clear()
    troute._MKT_CACHE.update(t=0.0, state=None, open=None)


@pytest.fixture(autouse=True)
def _no_yahoo(monkeypatch):
    """Never hit real Yahoo in tests; force the Tradier-parity fallback so spot
    is deterministic. Tests that want to exercise the Yahoo path patch it back."""
    async def _none(sym):
        return None
    monkeypatch.setattr(troute, "_yahoo_spot", _none)
