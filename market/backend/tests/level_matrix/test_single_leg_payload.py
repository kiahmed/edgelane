"""One-leg level picks (long call / long put) go to Tradier as class=option."""
from app.order_builder import Leg, build_tradier_order_payload
from app.routes.orders import _single_leg_payload
from app.webull_order_builder import WEBULL_STRATEGY


def _multi(order_type="debit", price=3.1):
    leg = Leg(side="call", strike=7280.0, action="buy_to_open", quantity=1,
              occ_symbol="SPXW261120C07280000")
    return build_tradier_order_payload(account_id="A", symbol="SPX", legs=[leg],
                                       order_type=order_type, limit_price=price, preview=True)


def test_single_leg_limit():
    p = _single_leg_payload(_multi(), "debit")
    assert p["class"] == "option" and p["type"] == "limit" and p["price"] == "3.10"
    assert p["option_symbol"] == "SPXW261120C07280000" and p["side"] == "buy_to_open"
    assert p["quantity"] == "1" and p["preview"] == "true"
    assert not any(k.endswith("]") for k in p)


def test_single_leg_market():
    p = _single_leg_payload(_multi("market", None), "market")
    assert p["type"] == "market" and "price" not in p


def test_webull_long_vol_mapped():
    assert WEBULL_STRATEGY["straddle"] == "STRADDLE"
    assert WEBULL_STRATEGY["strangle"] == "STRANGLE"
