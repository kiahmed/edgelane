"""Map an EdgeLane candidate (the same Leg list the Tradier path uses) to a
Webull OpenAPI option order payload (order_v2.preview_option / place_option).

Webull leg shape (from the OpenAPI demo):
    {"side":"BUY|SELL", "quantity":"1", "symbol":<underlying>, "strike_price":"220",
     "option_expire_date":"YYYY-MM-DD", "instrument_type":"OPTION",
     "option_type":"CALL|PUT", "market":"US"}

Order envelope: combo_type NORMAL, entrust_type QTY, option_strategy SINGLE for a
single leg else the multi-leg strategy, net side BUY (debit) / SELL (credit).

NOTE: the multi-leg `option_strategy` enum mapping below is best-effort. The order
flow always calls preview_option first, so Webull validates the payload and any
mismatch surfaces as a preview error before a live order is placed.
"""
from __future__ import annotations

import uuid

from .order_builder import Leg

# EdgeLane strategy → Webull option_strategy (multi-leg). SINGLE is chosen
# automatically when there's only one leg.
WEBULL_STRATEGY = {
    "bull_put": "VERTICAL",
    "bear_call": "VERTICAL",
    "bull_call": "VERTICAL",
    "bear_put": "VERTICAL",
    "iron_condor": "IRON_CONDOR",
    "iron_butterfly": "IRON_BUTTERFLY",
    "call_butterfly": "BUTTERFLY",
    "put_butterfly": "BUTTERFLY",
}

# Net direction of the strategy: credit spreads are a net SELL, debit a net BUY.
_CREDIT_STRATEGIES = {"bull_put", "bear_call", "iron_condor", "iron_butterfly"}


def _fmt_strike(strike: float) -> str:
    return str(int(strike)) if float(strike) == int(strike) else f"{float(strike)}"


def _net_side(strategy: str, order_type: str) -> str:
    if order_type == "credit":
        return "SELL"
    if order_type == "debit":
        return "BUY"
    return "SELL" if strategy in _CREDIT_STRATEGIES else "BUY"


def build_webull_option_order(
    *,
    strategy: str,
    legs: list[Leg],
    expiration: str,
    underlying: str,
    quantity: int,
    order_type: str,            # 'credit' | 'debit' | 'market'
    limit_price: float | None,
) -> list[dict]:
    if not legs:
        raise ValueError("no legs to build a Webull order")
    strat = "SINGLE" if len(legs) == 1 else WEBULL_STRATEGY.get(strategy)
    if not strat:
        raise ValueError(f"no Webull option_strategy mapping for {strategy!r}")

    wlegs = [
        {
            "side": "BUY" if lg.action.startswith("buy") else "SELL",
            "quantity": str(lg.quantity),
            "symbol": underlying,
            "strike_price": _fmt_strike(lg.strike),
            "option_expire_date": expiration,
            "instrument_type": "OPTION",
            "option_type": "CALL" if lg.side == "call" else "PUT",
            "market": "US",
        }
        for lg in legs
    ]

    order: dict = {
        "client_order_id": uuid.uuid4().hex,
        "combo_type": "NORMAL",
        "order_type": "MARKET" if order_type == "market" else "LIMIT",
        "quantity": str(int(quantity)),
        "option_strategy": strat,
        "side": _net_side(strategy, order_type),
        "time_in_force": "DAY",
        "entrust_type": "QTY",
        "legs": wlegs,
    }
    if order["order_type"] == "LIMIT":
        if limit_price is None:
            raise ValueError("limit order requires a limit_price")
        order["limit_price"] = f"{abs(float(limit_price)):.2f}"
    return [order]
