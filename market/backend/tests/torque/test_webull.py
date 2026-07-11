"""Torque ↔ Webull: strategy mapping, OCC expiry recovery, capability gating.

The Webull OpenAPI SDK cannot be imported here (it pins python <3.14 and this
venv is 3.14), so these tests exercise the pure builder + the route helper with a
fake client. They do NOT prove a live Webull order fills.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routes import torque as tq
from app.routes.torque import PlaceRequest, _expiration_from_occ, _place_webull
from app.webull_order_builder import WEBULL_STRATEGY, build_webull_option_order
from app.torque_config import STRATEGY_DEFS
from app.torque_engine import legs_to_order_legs


# ── OCC → expiration ───────────────────────────────────────────────────────
def test_expiration_recovered_from_occ_symbol():
    assert _expiration_from_occ("DJXW260710C00526000") == "2026-07-10"
    assert _expiration_from_occ("SPXW260709P07550000") == "2026-07-09"
    assert _expiration_from_occ("SPY260821C00600000") == "2026-08-21"


def test_expiration_none_for_garbage():
    for bad in ("", None, "NOTASYMBOL", "DJXW26071C00526000"):
        assert _expiration_from_occ(bad) is None


# ── strategy mapping covers every Torque strategy ──────────────────────────
def test_every_multileg_torque_strategy_maps_to_webull():
    multileg = {k for k, v in STRATEGY_DEFS.items() if v["legs"] > 1}
    missing = multileg - set(WEBULL_STRATEGY)
    assert not missing, f"Torque strategies with no Webull mapping: {missing}"


@pytest.mark.parametrize("strat,expected", [
    ("iron_fly", "IRON_BUTTERFLY"),
    ("call_fly", "BUTTERFLY"),
    ("put_fly", "BUTTERFLY"),
    ("bull_call", "VERTICAL"),
    ("iron_condor", "IRON_CONDOR"),
])
def test_torque_butterfly_names_build_a_webull_order(strat, expected):
    legs = legs_to_order_legs([
        {"side": "call", "strike": 525.0, "action": "buy_to_open", "quantity": 1,
         "symbol": "DJXW260710C00525000"},
        {"side": "call", "strike": 535.0, "action": "sell_to_open", "quantity": 1,
         "symbol": "DJXW260710C00535000"},
    ], 1)
    orders = build_webull_option_order(
        strategy=strat, legs=legs, expiration="2026-07-10", underlying="DJX",
        quantity=1, order_type="debit", limit_price=1.70)
    assert orders[0]["option_strategy"] == expected
    assert orders[0]["limit_price"] == "1.70"
    assert [l["symbol"] for l in orders[0]["legs"]] == ["DJX", "DJX"]


# ── route helper: capability gating ────────────────────────────────────────
class _FakeWebull:
    def __init__(self):
        self.previewed = self.placed = None

    async def preview_option(self, account_id, orders):
        self.previewed = (account_id, orders)
        return {"ok": True}

    async def place_option(self, account_id, orders):
        self.placed = (account_id, orders)
        return {"order_id": "WB-1"}


def _req(**kw):
    base = dict(symbol="DJX", strategy="bull_call", order_type="limit",
                limit_price=1.70, quantity=1, confirm=True,
                legs=[{"side": "call", "strike": 526.0, "action": "buy_to_open",
                       "quantity": 1, "symbol": "DJXW260710C00526000"},
                      {"side": "call", "strike": 536.0, "action": "sell_to_open",
                       "quantity": 1, "symbol": "DJXW260710C00536000"}])
    base.update(kw)
    return PlaceRequest(**base)


async def test_webull_autoclose_is_refused_not_silently_dropped():
    req = _req(auto_close=True)
    with pytest.raises(HTTPException) as ei:
        await _place_webull(req, _FakeWebull(), "ACC", req.legs)
    assert ei.value.status_code == 501
    assert "auto-close" in str(ei.value.detail).lower()


async def test_webull_dry_run_previews_and_places_nothing():
    req = _req(dry_run=True, confirm=False)
    fake = _FakeWebull()
    out = await _place_webull(req, fake, "ACC", req.legs)
    assert out["mode"] == "dry_run" and out["broker"] == "webull"
    assert fake.previewed is not None
    assert fake.placed is None, "dry_run must never place"


async def test_webull_place_previews_before_placing():
    req = _req()
    fake = _FakeWebull()
    out = await _place_webull(req, fake, "ACC", req.legs)
    assert out["mode"] == "entry_only"
    assert out["expiration"] == "2026-07-10"
    assert fake.previewed is not None, "must preview before going live"
    assert fake.placed is not None
    assert out["auto_close"] is False and out["close_placed"] is False


async def test_webull_rejects_legs_without_a_parseable_expiration():
    req = _req(legs=[{"side": "call", "strike": 526.0, "action": "buy_to_open",
                      "quantity": 1, "symbol": "BOGUS"}])
    with pytest.raises(HTTPException) as ei:
        await _place_webull(req, _FakeWebull(), "ACC", req.legs)
    assert ei.value.status_code == 400


# ── the four Tradier-only routes refuse Webull with 501, not AttributeError ─
class _ClosableFake(_FakeWebull):
    def __init__(self):
        super().__init__()
        self.closed = False

    async def close(self):
        self.closed = True


async def test_require_tradier_501s_and_closes_per_user_client():
    fake = _ClosableFake()
    with pytest.raises(HTTPException) as ei:
        await tq._require_tradier("webull", fake, True, "cancelling an order")
    assert ei.value.status_code == 501
    assert fake.closed is True, "per-user client must not leak"


async def test_require_tradier_passes_through_for_tradier():
    assert await tq._require_tradier("tradier", _ClosableFake(), True, "x") is None
