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
                 place_responses=None, order_status=None, orders=None,
                 order_status_by_id=None, calendar_days=None):
        self._spot = spot
        self._exp = exp
        self._raw = raw if raw is not None else raw_chain(spot, exp=exp)
        self._place = list(place_responses or [])
        self._orders = list(orders or [])
        self._calendar_days = dict(calendar_days or {})
        self._order_status = order_status or {
            "id": 1, "status": "filled", "avg_fill_price": 1.0,
            "exec_quantity": 1.0, "remaining_quantity": 0.0, "class": "multileg",
        }
        # Per-order-id override (keyed by str(order_id)) for tests that need
        # different orders to report different statuses in the same run — e.g.
        # a stop-limit that never fills while the entry/profit-close did. A
        # value can be a dict (static) or a list of dicts (advanced by each
        # get_order call, holding the last entry once exhausted) — the list
        # form simulates an order's status changing between polls, e.g. a fill
        # landing after the last "open" check but before the next read.
        self._order_status_by_id = dict(order_status_by_id or {})
        self._status_call_idx = {}   # str(order_id) -> index into a list-type entry
        self.placed = []          # records every payload submitted

    def _status_for(self, order_id):
        key = str(order_id)
        val = self._order_status_by_id.get(key)
        if isinstance(val, list):
            idx = min(self._status_call_idx.get(key, 0), len(val) - 1)
            return val[idx]
        return val if val is not None else self._order_status

    async def market_calendar(self, month, year):
        """Tradier-shaped calendar. Defaults to every day open 09:30-16:00
        (tests that don't care about market hours shouldn't have to think
        about this); pass `calendar_days` to a FakeTradier to override
        specific dates (e.g. a holiday or early close) for clock tests."""
        import calendar as _cal
        override = getattr(self, "_calendar_days", None) or {}
        days = []
        for d in range(1, _cal.monthrange(year, month)[1] + 1):
            date_str = f"{year:04d}-{month:02d}-{d:02d}"
            days.append(override.get(date_str) or {
                "date": date_str, "status": "open", "description": "Market is open",
                "premarket": {"start": "07:00", "end": "09:24"},
                "open": {"start": "09:30", "end": "16:00"},
                "postmarket": {"start": "16:00", "end": "19:55"},
            })
        return {"calendar": {"month": month, "year": year, "days": {"day": days}}}

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
        key = str(order_id)
        st = self._status_for(order_id)
        if isinstance(self._order_status_by_id.get(key), list):
            self._status_call_idx[key] = self._status_call_idx.get(key, 0) + 1
        return dict(st, id=order_id)

    async def get_orders(self, account_id):
        return list(getattr(self, "_orders", []) or [])

    async def cancel_order(self, account_id, order_id):
        # Realistic broker semantics: cancelling an order that already filled
        # fails (this is the race the stop-exit ladder must survive — a fill
        # landing between the last status poll and the cancel call). Anything
        # else actually gets canceled, updating what get_order reports next.
        # exec_quantity (if any) is preserved through the transition, since a
        # partial-fill-then-cancel must still be attributable afterward.
        key = str(order_id)
        current = self._status_for(order_id)
        if str(current.get("status") or "").lower() == "filled":
            raise RuntimeError(f"order {order_id} already filled, cannot cancel")
        self._order_status_by_id[key] = {**current, "status": "canceled"}
        self._status_call_idx.pop(key, None)   # collapses to a static dict from here on
        return {"order": {"id": order_id, "status": "ok"}}

    async def modify_order(self, account_id, order_id, price=None, order_type=None,
                           duration=None, stop=None):
        # Same realistic semantics as cancel_order: modifying an order that
        # already filled fails — this is the race the requote-via-modify path
        # must survive too. A resting (or partially filled, still-open)
        # order's price updates in place; its exec_quantity is untouched.
        current = self._status_for(order_id)
        if str(current.get("status") or "").lower() == "filled":
            raise RuntimeError(f"order {order_id} already filled, cannot modify")
        self.placed.append({"_modify": order_id, "price": price})
        return {"order": {"id": order_id, "status": "ok", "price": price}}

    async def close(self):
        """No-op — a per-user client's cleanup, exercised by tests that
        resolve_broker as per_user=True (a real Supabase-configured user's own
        connection) rather than the house client every other test uses."""
        pass


class FakeNewsDB:
    """Minimal Database stand-in for POST /webhook/news_signal's idempotency
    check — claim_news_signal only. Always claims successfully by default so
    existing tests that don't care about replay/idempotency don't need to
    know this exists; pass a shared instance (or pre-seed `_seen`) to a test
    that specifically exercises duplicate-detection."""
    def __init__(self):
        self._seen = set()

    def claim_news_signal(self, source_event_id):
        if source_event_id in self._seen:
            return False
        self._seen.add(source_event_id)
        return True


class FakeRequest:
    def __init__(self, client, db=None):
        self.app = type("A", (), {"state": type("S", (), {"tradier": client, "db": db or FakeNewsDB()})()})()


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
