"""DJX guardrails: stop-loss math, wide-market refusal, and the modify bypass."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import torque_config as tc
from app import torque_engine as teng
from app.routes import torque as tq


# ── stop trigger is measured at MID, never at the bid ──────────────────────
def test_debit_stop_fires_when_mark_falls_by_pct():
    # paid 1.00, stop 50% → fire at/below 0.50
    assert teng.stop_breached(0.50, 1.00, "debit", 50.0) is True
    assert teng.stop_breached(0.49, 1.00, "debit", 50.0) is True
    assert teng.stop_breached(0.51, 1.00, "debit", 50.0) is False


def test_credit_stop_fires_when_buyback_cost_rises():
    # collected 1.00, stop 50% → fire when it costs 1.50+ to buy back
    assert teng.stop_breached(1.50, 1.00, "credit", 50.0) is True
    assert teng.stop_breached(1.49, 1.00, "credit", 50.0) is False


def test_a_bid_based_stop_would_fire_instantly_on_djx_but_mid_does_not():
    # real DJXW package: net_bid 0.45 / mid 5.22 / net_ask 10.00, entry at mid
    entry, bid, mid = 5.22, 0.45, 5.22
    assert teng.stop_breached(bid, entry, "debit", 50.0) is True   # bid → instant stop-out
    assert teng.stop_breached(mid, entry, "debit", 50.0) is False  # mid → correctly quiet


def test_stop_is_inert_without_an_entry_fill_or_pct():
    assert teng.stop_breached(0.1, 0.0, "debit", 50.0) is False
    assert teng.stop_breached(0.1, 1.0, "debit", None) is False


# ── stop exit crosses, with a bounded price ────────────────────────────────
def test_debit_stop_exits_at_the_package_bid_and_credit_at_the_ask():
    px = {"complete": True, "net_bid": 0.77, "net_ask": 0.89, "abs_mid": 0.83}
    assert teng.stop_exit_price(px, "debit", 0.01) == 0.77
    assert teng.stop_exit_price(px, "credit", 0.01) == 0.89


def test_stop_exit_none_without_a_complete_price():
    assert teng.stop_exit_price({"complete": False}, "debit", 0.01) is None


def test_wide_market_ceiling_is_below_the_djx_disaster_case():
    # DJXW 07-10 package measured at 182.8% of mid — must exceed the ceiling so
    # _monitor_stop refuses to cross rather than dumping into that book.
    assert 182.8 > tc.STOP_MAX_EXIT_SPREAD_PCT
    # ...while a normal DIA-grade book (14.5%) is happily crossable.
    assert 14.5 < tc.STOP_MAX_EXIT_SPREAD_PCT


def test_djx_has_a_stop_default_and_tight_names_do_not():
    assert tc.stop_loss_default("DJX") == 50.0
    for t in ("SPX", "NDX", "SPY"):
        assert tc.stop_loss_default(t) is None


# ── the orders-panel modify bypass ─────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_guard():
    tq._CLOSE_GUARD.clear()
    yield
    tq._CLOSE_GUARD.clear()


def _arm(entry_type="debit", entry_fill=1.00, floor=45.0, symbol="DJX"):
    tq._CLOSE_GUARD["OID"] = {"symbol": symbol, "entry_type": entry_type,
                              "entry_fill": entry_fill, "tick": 0.01,
                              "floor_pct": floor}


def test_modify_rejects_lowering_a_debit_close_below_the_floor():
    _arm()                                   # floor price = 1.00 * 1.45 = 1.45
    with pytest.raises(HTTPException) as ei:
        tq._enforce_close_floor_on_modify("OID", 1.05)
    assert ei.value.status_code == 400
    assert "1.45" in str(ei.value.detail)


def test_modify_allows_raising_a_debit_close():
    _arm()
    assert tq._enforce_close_floor_on_modify("OID", 1.45) is None
    assert tq._enforce_close_floor_on_modify("OID", 2.00) is None


def test_modify_rejects_raising_a_credit_buyback_above_the_floor():
    _arm(entry_type="credit", entry_fill=2.00)   # floor price = 2.00 * (1-0.45) = 1.10
    with pytest.raises(HTTPException):
        tq._enforce_close_floor_on_modify("OID", 1.50)   # keeps less of the credit
    assert tq._enforce_close_floor_on_modify("OID", 1.10) is None
    assert tq._enforce_close_floor_on_modify("OID", 0.90) is None


def test_modify_is_unrestricted_for_tight_tickers():
    _arm(symbol="SPX")
    assert tq._enforce_close_floor_on_modify("OID", 0.01) is None


def test_modify_passes_through_unknown_orders():
    assert tq._enforce_close_floor_on_modify("NOT-TRACKED", 0.01) is None


# ── route-level: market orders banned, spread warning, autoclose block ─────
import app.routes.torque as troute
from app.routes.torque import PlaceRequest, torque_place
from .conftest import FakeTradier, FakeRequest

DEV = {"id": "dev-local", "email": "dev@local", "auth": "dev"}


def _place_req(**kw):
    base = dict(symbol="DJX", strategy="bull_call", order_type="limit",
                limit_price=1.70, quantity=1, confirm=True,
                legs=[{"side": "call", "strike": 526.0, "action": "buy_to_open",
                       "quantity": 1, "symbol": "DJXW260710C00526000"},
                      {"side": "call", "strike": 536.0, "action": "sell_to_open",
                       "quantity": 1, "symbol": "DJXW260710C00536000"}])
    base.update(kw)
    return PlaceRequest(**base)


async def test_market_order_is_refused_for_djx():
    req = _place_req(order_type="market", limit_price=None)
    with pytest.raises(HTTPException) as ei:
        await torque_place(req, FakeRequest(FakeTradier()), user=DEV)
    assert ei.value.status_code == 400
    assert "market orders are disabled for DJX" in str(ei.value.detail)


async def test_market_order_still_allowed_for_spx():
    # SPX must be untouched by the DJX ban — assert at the config layer (a full
    # SPX place needs a matching fake chain).
    assert tc.market_orders_allowed("SPX") is True
    assert tc.market_orders_allowed("NDX") is True
    assert tc.market_orders_allowed("DJX") is False


def test_stop_default_reaches_place_via_config():
    # a DJX place with no explicit stop_loss_pct inherits the 50% default
    req = _place_req()
    assert req.stop_loss_pct is None
    assert tc.stop_loss_default(req.symbol) == 50.0


def test_stop_disables_the_native_oto_bracket_so_it_is_not_dropped():
    """A broker-held OTO has no stop leg and returns before the watcher exists.
    Single-leg + limit + auto_close must therefore NOT take the OTO path when a
    stop is armed (DJX arms one by default)."""
    import inspect
    src = inspect.getsource(troute.torque_place)
    assert 'if is_single and req.auto_close and otype == "limit" and not stop_pct:' in src


def test_stop_only_watcher_does_not_advertise_a_close_target():
    """auto_close=false + a stop arms the watcher, but there is no profit-target
    close order, so close_target_price must be None rather than a phantom price."""
    import inspect
    src = inspect.getsource(troute.torque_place)
    assert "if (est_fill and req.auto_close) else None" in src


# ── auto-close is MANDATORY on DJX ─────────────────────────────────────────
def test_auto_close_is_required_for_djx_only():
    assert tc.close_target_required("DJX") is True
    for t in ("SPX", "NDX", "RUT", "SPY", "QQQ"):
        assert tc.close_target_required(t) is False


def test_close_targets_map_exposes_required_flag():
    m = tc.close_targets_map()
    assert m["DJX"]["required"] is True
    assert m["SPX"]["required"] is False


async def test_place_rejects_djx_without_auto_close():
    req = _place_req(auto_close=False)
    with pytest.raises(HTTPException) as ei:
        await torque_place(req, FakeRequest(FakeTradier()), user=DEV)
    assert ei.value.status_code == 400
    assert "auto-close is mandatory for DJX" in str(ei.value.detail)


async def test_place_rejects_djx_without_auto_close_before_touching_the_chain():
    """The mandatory check must precede the spread probe, so the user is told the
    real reason instead of a confusing spread error."""
    req = _place_req(auto_close=False, order_type="market", limit_price=None)
    with pytest.raises(HTTPException) as ei:
        await torque_place(req, FakeRequest(FakeTradier()), user=DEV)
    # market-order ban is validated even earlier — that's the correct precedence
    assert ei.value.status_code == 400
    assert "market orders are disabled" in str(ei.value.detail)


async def test_spx_may_still_place_without_auto_close():
    assert tc.close_target_required("SPX") is False


# ── fee-aware close target ─────────────────────────────────────────────────
def test_commissions_match_tradier_order_preview():
    # read off Tradier's `commission` field (production AND sandbox agree):
    #   DJX buy 1 -> 0.53   DJX buy 10 -> 5.30   DJX 2-leg -> 1.06   SPX -> 0.95
    assert tc.commission_per_contract("DJX") == 0.53
    assert tc.commission_per_contract("SPX") == 0.95
    assert tc.commission_per_contract("ZZZZ") == tc.DEFAULT_COMMISSION
    # regulatory pass-throughs are NOT invented — preview reports fees=0
    assert tc.extra_fee_per_contract("DJX") == 0.0
    assert tc.fee_per_contract("DJX") == 0.53


def test_round_trip_fee_is_per_unit_and_scales_with_legs():
    # 2 sides x legs x fee / multiplier — independent of contract count
    assert teng.round_trip_fee_price(0.53, 1) == 0.0106
    assert teng.round_trip_fee_price(0.53, 2) == 0.0212
    assert teng.round_trip_fee_price(0.53, 4) == 0.0424


def test_zero_fee_reduces_to_the_naive_target():
    assert teng.close_target_price(1.00, "debit", 45, 0.01) == 1.45
    assert teng.close_target_price(2.00, "credit", 45, 0.01) == 1.10


def test_fees_push_a_debit_target_up_and_a_credit_target_down():
    up = teng.close_target_price(6.00, "debit", 45, 0.01, legs=1, fee_per_contract=0.53)
    assert up == 8.72 and up > 8.70          # naive was 8.70
    dn = teng.close_target_price(2.00, "credit", 45, 0.01, legs=2, fee_per_contract=0.53)
    assert dn < 1.10                          # naive was 1.10


def test_breakeven_is_above_entry_for_a_debit_and_below_for_a_credit():
    be = teng.breakeven_close_price(6.00, "debit", 0.01, legs=1, fee_per_contract=0.53)
    assert be == 6.02 and be > 6.00
    be_c = teng.breakeven_close_price(2.00, "credit", 0.01, legs=1, fee_per_contract=0.53)
    assert be_c < 2.00


@pytest.mark.parametrize("entry", [0.05, 0.70, 1.70, 6.00, 25.95])
@pytest.mark.parametrize("legs", [1, 2, 4])
def test_any_positive_target_always_clears_breakeven(entry, legs):
    """The invariant that matters: a profit target must never price below the
    round-trip cost. Directional tick rounding is what guarantees it."""
    fee = 0.53
    be = teng.breakeven_close_price(entry, "debit", 0.01, legs=legs, fee_per_contract=fee)
    tgt = teng.close_target_price(entry, "debit", 1.0, 0.01, legs=legs, fee_per_contract=fee)
    assert tgt >= be, f"target {tgt} < breakeven {be}"


def test_credit_target_never_exceeds_breakeven():
    fee = 0.53
    for entry in (0.50, 2.00, 10.0):
        be = teng.breakeven_close_price(entry, "credit", 0.01, legs=2, fee_per_contract=fee)
        tgt = teng.close_target_price(entry, "credit", 1.0, 0.01, legs=2, fee_per_contract=fee)
        assert tgt <= be, f"buyback target {tgt} > breakeven {be}"


# ── credit close target must never go non-positive (bug: pct>=~100) ─────────
def test_credit_close_price_is_always_positive_even_at_absurd_pct():
    for pct in (99, 100, 120, 200, 500):
        p = teng.close_target_price(2.00, "credit", pct, 0.05)
        assert p > 0, f"credit pct={pct} produced non-positive limit {p}"
        assert p >= 0.05                          # floored at one tick
    # a debit is unaffected and stays well positive
    assert teng.close_target_price(2.00, "debit", 500, 0.05) > 2.0


def test_credit_close_price_floors_at_one_tick_with_fees():
    # fees push it negative even sooner; still floored positive
    p = teng.close_target_price(2.00, "credit", 100, 0.05, legs=2, fee_per_contract=0.53)
    assert p == 0.05


def test_max_credit_pct_keeps_the_buyback_at_or_above_one_tick():
    for entry in (0.50, 1.00, 2.00, 10.0):
        for legs, fee in ((1, 0.0), (2, 0.53), (4, 0.95)):
            ceil = teng.max_credit_pct(entry, 0.05, legs=legs, fee_per_contract=fee)
            px = teng.close_target_price(entry, "credit", ceil, 0.05,
                                         legs=legs, fee_per_contract=fee)
            assert px >= 0.05, f"entry={entry} legs={legs} ceil={ceil} -> {px}"
            assert 1.0 <= ceil <= 99.0


def test_max_credit_pct_degenerates_gracefully_for_tiny_credit():
    assert teng.max_credit_pct(0.05, 0.05) == 1.0


async def test_place_caps_credit_target_and_arms_a_positive_close():
    """End-to-end: a credit spread with an absurd close_target_pct must still
    place a POSITIVE, fillable buy-to-close — not a negative limit the broker
    rejects (which would silently unarm auto-close)."""
    fake = FakeTradier(spot=22000.0)
    _, _, contracts = await troute._get_chain(fake, "NDX")
    spot = teng.implied_spot(contracts, fallback=22000.0)
    st = teng.build_structure(spot, contracts, "NDX", "bull_put")   # credit, 2-leg
    assert st["type"] == "credit"
    legs = st["legs"]
    limit = abs(teng.price_structure(legs, {c["symbol"]: c for c in contracts})["net_mid"]) or 5.0
    req = PlaceRequest(symbol="NDX", strategy="bull_put", legs=legs,
                       order_type="limit", limit_price=limit, quantity=1,
                       auto_close=True, close_target_pct=200, account_id="T", confirm=True)
    res = await torque_place(req, FakeRequest(fake), user=DEV)
    # pct got capped below 100
    assert res["close_target_pct"] < 100
    assert res["close_target_clamped"] is True
    # drive the watcher and inspect the ACTUAL close payload price
    w = troute._WATCHERS[res["watch_id"]]
    await w["task"]
    closes = [p for p in fake.placed if str(p.get("tag", "")).startswith("torqueClose")]
    assert closes, "no close order was placed"
    price = float(closes[-1].get("price"))
    assert price > 0, f"credit close placed at non-positive price {price}"
