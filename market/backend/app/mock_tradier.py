"""MockTradierClient — synthetic deterministic chain for offline / no-token mode.

Matches the public interface of `app.tradier_client.TradierClient`:

    await client.stock_quote(symbol)        -> dict (quote)         (unwrapped)
    await client.option_expirations(symbol) -> list[str]            (dates)
    await client.options_chain(symbol, expiration) -> list[dict]    (contracts)
    await client.close() -> None
    client.rate_limit -> dict

Shapes returned by stock_quote() and options_chain() mirror what the real
TradierClient returns after its internal unwrapping (quote unwrapped from
`{'quotes': {'quote': {...}}}`; expirations as a plain list; chain as a plain
list of option contracts in Tradier's raw shape with embedded greeks).

The synthetic chain is structurally similar to fixtures.fx_spx_0dte_put_wall_above:
a put wall ~15 points above spot drives a bullish-leaning bias so the engine
exercises a non-trivial code path (recommended_strategies non-empty, engine_pick
populated).
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable


def _next_weekday(d: date) -> date:
    """Return today if weekday, else nearest upcoming Mon-Fri."""
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d


def _mk_raw_option(
    symbol_root: str,
    expiration: str,
    strike: float,
    side: str,
    spot: float,
    oi: int,
    delta: float,
    gamma: float = 0.04,
    theta: float = -0.5,
    vega: float = 10.0,
    iv: float = 0.20,
) -> dict:
    """Build a Tradier-shaped option contract dict (option_type='call'|'put',
    embedded greeks under 'greeks').
    """
    intrinsic = max(0.0, (spot - strike) if side == "call" else (strike - spot))
    time_val = max(0.10, 5.0 - abs(strike - spot) / 30.0)
    mid = intrinsic + time_val
    bid = round(max(0.05, mid - 0.05), 2)
    ask = round(mid + 0.05, 2)
    sign = "C" if side == "call" else "P"
    occ = f"{symbol_root}{expiration.replace('-', '')[2:]}{sign}{int(strike * 1000):08d}"
    return {
        "symbol": occ,
        "underlying": symbol_root,
        "strike": float(strike),
        "option_type": side,
        "expiration_date": expiration,
        "open_interest": int(oi),
        "volume": 0,
        "bid": float(bid),
        "ask": float(ask),
        "last": float((bid + ask) / 2),
        "greeks": {
            "delta": float(delta),
            "gamma": float(gamma),
            "theta": float(theta),
            "vega": float(vega),
            "mid_iv": float(iv),
            "smv_vol": float(iv),
        },
    }


def _build_mock_chain(symbol: str, expiration: str, spot: float) -> list[dict]:
    """Mirror fixtures.fx_spx_0dte_put_wall_above geometry: huge put wall
    15 pts above spot at strike=7595. Generates 23 strike levels around spot
    (step=5) → 46 contracts (call+put per strike)."""
    strikes = [spot - 50 + i * 5 for i in range(23)]
    out: list[dict] = []
    for strike in strikes:
        # Crude linear delta (matches the fixture builder)
        call_delta = max(0.05, min(0.95, 0.5 + (spot - strike) / 100))
        put_delta = call_delta - 1.0
        # Heavy put OI at spot+15
        put_oi = 8000 if abs(strike - (spot + 15)) < 0.01 else 300
        call_oi = 400
        out.append(_mk_raw_option(symbol, expiration, strike, "call", spot,
                                  oi=call_oi, delta=call_delta))
        out.append(_mk_raw_option(symbol, expiration, strike, "put", spot,
                                  oi=put_oi, delta=put_delta))
    return out


# Default spots — tuned so the synthetic chain produces a non-trivial bias.
_DEFAULT_SPOTS: dict[str, float] = {
    "SPX": 7580.0,
    "SPY": 758.0,
    "QQQ": 600.0,
    "IWM": 220.0,
    "NDX": 24000.0,
}


class MockTradierClient:
    """Drop-in offline replacement for TradierClient.  Same async surface."""

    def __init__(self, default_spot: float | None = None):
        self._default_spot = default_spot
        # Per-symbol call counter for deterministic spot drift. The chain
        # geometry stays anchored on the base spot so engine output remains
        # stable; only the *reported* spot drifts, which is enough to drive
        # the evaluator's win/loss math.
        self._quote_calls: dict[str, int] = {}
        self._rate_limit: dict[str, str] = {
            "x-ratelimit-allowed": "120",
            "x-ratelimit-available": "120",
            "x-ratelimit-used": "0",
        }

    @property
    def rate_limit(self) -> dict[str, str]:
        return dict(self._rate_limit)

    async def close(self) -> None:
        return None

    def _spot_for(self, symbol: str) -> float:
        if self._default_spot is not None:
            return self._default_spot
        return _DEFAULT_SPOTS.get(symbol.upper(), 100.0)

    async def stock_quote(self, symbol: str) -> dict:
        base = self._spot_for(symbol)
        # Deterministic non-monotonic drift in absolute price units:
        #   call 1..5  → +0.4 .. +2.0   (small up)
        #   call 6..10 → +2.4 .. -0.4   (pulls back through zero)
        #   call 11..  → -0.8 .. +N     (overshoots low then recovers)
        # Net result over ~20 calls: a mix of wins / losses / neutrals when
        # the evaluator compares spot_at_eval to spot_at_decision.
        sym = symbol.upper()
        c = self._quote_calls.get(sym, 0) + 1
        self._quote_calls[sym] = c
        # Triangle wave: rises for 6 calls, falls for 6, repeats. Amplitude scales
        # with base so SPX drifts a few points while SPY drifts cents.
        amplitude = max(base * 0.0005, 0.5)   # ~5 bps swing per peak
        phase = c % 12
        if phase <= 6:
            drift = amplitude * (phase / 6.0)
        else:
            drift = amplitude * ((12 - phase) / 6.0)
        # Add a slow rising bias so we definitely cross the neutral band
        drift += amplitude * (c // 12) * 0.4
        spot = base + drift
        return {
            "symbol": sym,
            "description": f"MOCK {sym}",
            "last": spot,
            "close": spot,
            "bid": spot - 0.05,
            "ask": spot + 0.05,
            "volume": 0,
            "type": "index",
        }

    async def option_expirations(self, symbol: str) -> list[str]:
        # Today (or next weekday) for 0DTE, plus a 7DTE for variety.
        today = _next_weekday(date.today())
        plus7 = _next_weekday(today + timedelta(days=7))
        return [today.isoformat(), plus7.isoformat()]

    async def options_chain(self, symbol: str, expiration: str) -> list[dict]:
        spot = self._spot_for(symbol)
        return _build_mock_chain(symbol.upper(), expiration, spot)

    # --- order submission (synthetic, mirrors Tradier's response shape) ----
    # Real Tradier returns:
    #   {"order": {"id": 12345, "status": "ok", "partner_id": "..."}}
    # The mock returns a deterministic increasing id so tests can assert on it.
    _order_id_seq: int = 100000

    async def place_order(self, account_id: str, payload: dict) -> dict:
        """Synthetic multi-leg order submission. Returns a Tradier-shaped
        response. `preview=true` in the payload makes the response carry
        a synthetic cost/margin estimate so the UI preview pane is non-empty."""
        MockTradierClient._order_id_seq += 1
        oid = MockTradierClient._order_id_seq
        is_preview = str(payload.get("preview", "")).lower() == "true"
        # Sum quantities to derive a synthetic cost estimate
        try:
            qty_total = sum(
                int(payload.get(f"quantity[{i}]", 0))
                for i in range(8) if f"quantity[{i}]" in payload
            )
        except Exception:
            qty_total = 1
        try:
            price = float(payload.get("price") or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        cost = round(price * qty_total * 100, 2)
        order: dict = {
            "id": oid,
            "status": "ok",
            "partner_id": f"mock-{account_id}",
        }
        if is_preview:
            order.update({
                "cost": cost,
                "margin_change": cost,
                "commission": 0.0,
                "fees": 0.0,
                "result": True,
                "preview": True,
            })
        else:
            order.update({"preview": False})
        return {"order": order}

    async def quotes(self, symbols, greeks: bool = False) -> list[dict]:
        """Synthetic quotes for Torque's live re-price poll. Deterministic
        bid/ask per symbol so the demo net price is stable (not real data)."""
        if isinstance(symbols, (list, tuple, set)):
            syms = [str(s) for s in symbols]
        else:
            syms = [s for s in str(symbols).split(",") if s]
        out = []
        for s in syms:
            # crude per-symbol value seeded by the strike digits, just so the
            # spread nets to something believable in demo mode
            digits = "".join(ch for ch in s[-8:] if ch.isdigit()) or "1000"
            base = max(0.5, (int(digits) % 5000) / 100.0)
            out.append({"symbol": s, "bid": round(base, 2), "ask": round(base + 0.20, 2),
                        "last": round(base + 0.10, 2)})
        return out

    async def get_order(self, account_id: str, order_id) -> dict:
        """Synthetic order lookup — always reports FILLED so the demo
        confirm-then-close path completes end to end."""
        return {"id": order_id, "status": "filled", "avg_fill_price": 1.0,
                "exec_quantity": 1.0, "remaining_quantity": 0.0, "class": "multileg"}

    async def get_orders(self, account_id: str) -> list[dict]:
        """Synthetic order list (empty in demo)."""
        return []

    async def cancel_order(self, account_id: str, order_id) -> dict:
        return {"order": {"id": order_id, "status": "ok"}}

    async def modify_order(self, account_id: str, order_id, price=None,
                           order_type=None, duration=None, stop=None) -> dict:
        return {"order": {"id": order_id, "status": "ok", "price": price}}
