"""Torque news-signal ingestion (facades-news-reactor webhook).

See docs/torque.md 'News-signal ingestion' and the sibling repo's
docs/torque-integration-proposal.md.
"""
from __future__ import annotations

import pytest

from app.routes import torque as troute
from app.routes.torque import NewsSignalPayload, news_signal
from app import torque_config as tcfg

from .conftest import FakeTradier, FakeRequest, FakeNewsDB


def _payload(**kw):
    base = dict(
        # NDX matches FakeTradier's default synthetic chain (root NDXP, spot
        # 22000) so strikes actually resolve without a custom chain fixture.
        source_event_id="evt-1", headline="Fed signals pause", category="economic",
        symbol="NDX", direction="bullish", sentiment="bullish", confidence=0.8,
        rationale="test", tradier_confirmation={"symbol": "SPY", "samples": 3, "agreed": True, "lean": "call"},
        generated_at="2026-09-26T14:32:10Z",
    )
    base.update(kw)
    return NewsSignalPayload(**base)


@pytest.fixture
def enabled(monkeypatch):
    """Flip the feature on for this test, restored automatically after.
    Settings is a frozen pydantic model, so this patches get_settings()
    itself to return a modified copy rather than mutating the singleton."""
    s = troute.get_settings().model_copy(update={
        "accept_news_reactor_signals": True,
        "news_signal_quantity": 2,
    })
    monkeypatch.setattr(troute, "get_settings", lambda: s)
    return s


@pytest.fixture(autouse=True)
def _operator(monkeypatch):
    monkeypatch.setattr(tcfg, "operator_uids", lambda: ["op-uid-1"])


@pytest.fixture(autouse=True)
def _reset_rate_cap():
    """_NEWS_SIGNAL_RATE is a module-level counter (by design — it must
    persist across requests, not reset per-request); reset it here so one
    test's calls don't count against another's cap budget."""
    troute._NEWS_SIGNAL_RATE.update(window_start=0.0, count=0)
    yield
    troute._NEWS_SIGNAL_RATE.update(window_start=0.0, count=0)


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch):
    async def _open(request):
        return {"market_state": "REGULAR", "open": True}
    monkeypatch.setattr(troute, "torque_clock", _open)


async def test_disabled_by_default_drops_without_placing():
    client = FakeTradier()
    r = await news_signal(_payload(), FakeRequest(client))
    assert r == {"ok": True, "accepted": False, "reason": "ACCEPT_NEWS_REACTOR_SIGNALS is off"}
    assert client.placed == []


async def test_unrecognized_direction_is_dropped(enabled):
    client = FakeTradier()
    r = await news_signal(_payload(direction="sideways"), FakeRequest(client))
    assert r["accepted"] is False and "direction" in r["reason"]
    assert client.placed == []


async def test_unconfigured_ticker_is_dropped(enabled):
    client = FakeTradier()
    r = await news_signal(_payload(symbol="ZZZZ"), FakeRequest(client))
    assert r["accepted"] is False and "ZZZZ" in r["reason"]
    assert client.placed == []


async def test_market_closed_defensive_check_drops(enabled, monkeypatch):
    async def _closed(request):
        return {"market_state": "CLOSED", "open": False}
    monkeypatch.setattr(troute, "torque_clock", _closed)
    client = FakeTradier()
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False and "closed" in r["reason"]
    assert client.placed == []


async def test_no_operator_configured_is_dropped(enabled, monkeypatch):
    monkeypatch.setattr(tcfg, "operator_uids", lambda: [])
    client = FakeTradier()
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False and "operator" in r["reason"].lower()
    assert client.placed == []


async def test_operator_missing_torque_tool_is_dropped(enabled, monkeypatch):
    async def _tools(uid):
        return ["news-reactor"]   # has the news entitlement but not torque itself
    monkeypatch.setattr(troute.supabase_admin, "get_user_tools", _tools)
    client = FakeTradier()
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False
    assert len(r["results"]) == 1 and r["results"][0]["accepted"] is False
    assert "torque" in r["results"][0]["reason"]
    assert client.placed == []


async def test_operator_missing_news_reactor_tool_is_dropped(enabled, monkeypatch):
    async def _tools(uid):
        return ["torque"]        # can use Torque, but never opted into news-reactor
    monkeypatch.setattr(troute.supabase_admin, "get_user_tools", _tools)
    client = FakeTradier()
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False
    assert "news-reactor" in r["results"][0]["reason"]
    assert client.placed == []


async def test_operator_with_no_broker_connection_is_dropped_not_errored(enabled, monkeypatch):
    """resolve_broker raises 403 for a supabase user with no active broker
    connection — must surface as a graceful accepted:false, not an exception,
    and never fall back to the house account for execution."""
    async def _tools(uid):
        return ["torque", "news-reactor"]
    monkeypatch.setattr(troute.supabase_admin, "get_user_tools", _tools)

    from fastapi import HTTPException
    async def _no_broker(request, user):
        raise HTTPException(403, "No active brokerage connection for your account.")
    monkeypatch.setattr(troute, "resolve_broker", _no_broker)

    client = FakeTradier()
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False
    assert "brokerage" in r["results"][0]["reason"]
    assert client.placed == []


async def test_two_qualified_accounts_both_get_the_order_from_one_post(enabled, monkeypatch):
    """The sender posts once; Torque fans out to every qualified account —
    not the other way around. One account with no broker must not block a
    second account that has one."""
    monkeypatch.setattr(tcfg, "operator_uids", lambda: ["op-uid-1", "op-uid-2"])

    async def _tools(uid):
        return ["torque", "news-reactor"]
    monkeypatch.setattr(troute.supabase_admin, "get_user_tools", _tools)

    from fastapi import HTTPException
    client2 = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])
    async def _fake_resolve(request, user):
        if user["id"] == "op-uid-1":
            raise HTTPException(403, "No active brokerage connection for your account.")
        return "tradier", client2, "TEST456", True
    monkeypatch.setattr(troute, "resolve_broker", _fake_resolve)

    client = FakeTradier()   # the house client used for the shared build/price step
    r = await news_signal(_payload(direction="bullish", symbol="NDX"), FakeRequest(client))

    assert r["accepted"] is True
    assert len(r["results"]) == 2
    by_uid = {x["uid"]: x for x in r["results"]}
    assert by_uid["op-uid-1"]["accepted"] is False and "brokerage" in by_uid["op-uid-1"]["reason"]
    assert by_uid["op-uid-2"]["accepted"] is True
    assert len(client2.placed) == 1   # op-uid-2's own connection is what actually got the order


async def test_spread_too_wide_is_dropped(enabled, monkeypatch):
    async def _tools(uid):
        return ["torque", "news-reactor"]
    monkeypatch.setattr(troute.supabase_admin, "get_user_tools", _tools)

    def _wide_price(legs, px_map):
        return {"complete": True, "net_bid": 0.05, "net_ask": 6.00, "abs_mid": 3.00}
    monkeypatch.setattr(troute.teng, "price_structure", _wide_price)

    client = FakeTradier()
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False and "spread too wide" in r["reason"]
    assert client.placed == []


async def test_bullish_signal_places_a_long_call_with_stop_and_news_tag(enabled, monkeypatch):
    async def _tools(uid):
        return ["torque", "news-reactor"]
    monkeypatch.setattr(troute.supabase_admin, "get_user_tools", _tools)

    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])

    async def _fake_resolve(request, user):
        assert user == {"id": "op-uid-1", "auth": "supabase"}   # acts as the resolved operator
        return "tradier", client, "TEST123", True
    monkeypatch.setattr(troute, "resolve_broker", _fake_resolve)

    r = await news_signal(_payload(direction="bullish", symbol="NDX"), FakeRequest(client))

    assert r["accepted"] is True
    assert r["symbol"] == "NDX" and r["strategy"] == "long_call"
    assert r["stop_loss_pct"] == tcfg.stop_loss_default("SPY") or tcfg.DEFAULT_STOP_LOSS_PCT
    assert len(client.placed) == 1
    entry = client.placed[0]
    assert entry["class"] == "otoco"                    # native bracket: TP + SL both broker-held
    assert entry["quantity[0]"] == "2"                  # news_signal_quantity from the `enabled` fixture
    assert entry["tag"].startswith("torqueNews")        # distinguishable from a normal UI-placed order


async def test_bearish_signal_places_a_long_put(enabled, monkeypatch):
    async def _tools(uid):
        return ["torque", "news-reactor"]
    monkeypatch.setattr(troute.supabase_admin, "get_user_tools", _tools)

    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])

    async def _fake_resolve(request, user):
        return "tradier", client, "TEST123", True
    monkeypatch.setattr(troute, "resolve_broker", _fake_resolve)

    r = await news_signal(_payload(direction="bearish", symbol="NDX"), FakeRequest(client))
    assert r["accepted"] is True and r["strategy"] == "long_put"


# ── idempotency, fail-closed clock, rate cap ────────────────────────────────
async def test_replay_of_the_same_source_event_id_does_not_place_twice(enabled, monkeypatch):
    """The legitimate sender's own retry, or a captured-and-replayed request
    within the signature's timestamp window, must never place a second order.
    Same FakeRequest (hence same FakeNewsDB) used for both calls, mirroring a
    real replay hitting the same backend process/DB."""
    async def _tools(uid):
        return ["torque", "news-reactor"]
    monkeypatch.setattr(troute.supabase_admin, "get_user_tools", _tools)

    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])

    async def _fake_resolve(request, user):
        return "tradier", client, "TEST123", True
    monkeypatch.setattr(troute, "resolve_broker", _fake_resolve)

    req = FakeRequest(client)
    payload = _payload(source_event_id="evt-replay-1")
    r1 = await news_signal(payload, req)
    r2 = await news_signal(payload, req)   # identical event, same request/db

    assert r1["accepted"] is True
    assert r2["accepted"] is False and r2["reason"] == "duplicate source_event_id"
    assert len(client.placed) == 1   # only the first call ever placed anything


async def test_idempotency_store_unavailable_drops_rather_than_risk_a_double_place(enabled):
    """If app.state.db isn't wired for some reason, refuse to guess — drop
    rather than place without any replay protection at all."""
    client = FakeTradier()
    req = FakeRequest(client)
    req.app.state.db = None
    r = await news_signal(_payload(), req)
    assert r["accepted"] is False and "idempotency" in r["reason"]
    assert client.placed == []


async def test_market_clock_failure_fails_closed_not_open(enabled, monkeypatch):
    """An unattended trader must never act on an uncertain market-open read —
    a calendar-fetch exception must drop the signal, not proceed as if open."""
    async def _boom(request):
        raise RuntimeError("calendar fetch failed")
    monkeypatch.setattr(troute, "torque_clock", _boom)

    client = FakeTradier()
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False and "market" in r["reason"]
    assert client.placed == []


async def test_market_open_unknown_fails_closed(enabled, monkeypatch):
    async def _unknown(request):
        return {"market_state": None, "open": None}
    monkeypatch.setattr(troute, "torque_clock", _unknown)

    client = FakeTradier()
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False and "market" in r["reason"]


async def test_rate_cap_blocks_after_the_configured_limit(enabled, monkeypatch):
    s = troute.get_settings().model_copy(update={"news_signal_max_per_window": 2})
    monkeypatch.setattr(troute, "get_settings", lambda: s)

    async def _tools(uid):
        return ["torque", "news-reactor"]
    monkeypatch.setattr(troute.supabase_admin, "get_user_tools", _tools)

    results = []
    for i in range(3):
        client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])

        async def _fake_resolve(request, user, _c=client):
            return "tradier", _c, "TEST123", True
        monkeypatch.setattr(troute, "resolve_broker", _fake_resolve)
        r = await news_signal(_payload(source_event_id=f"evt-rate-{i}"), FakeRequest(client))
        results.append(r["accepted"])

    assert results == [True, True, False]   # third call in the same window is capped
