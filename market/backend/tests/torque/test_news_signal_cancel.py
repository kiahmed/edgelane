"""Torque news-signal CANCEL: unwind a prior signal by source_event_id.

Simulates the full round trip: a signal comes in and becomes a live order,
then a SECOND signal (state="cancel") arrives referencing the first one's
source_event_id and must either market-close whatever actually executed (only
the executed quantity) or let a plain cancel stand for whatever never filled —
regardless of current P/L or the stop-loss level, per spec. See
_handle_cancel_signal / _cancel_or_close_entry in app/routes/torque.py.
"""
from __future__ import annotations

import pytest

from app.routes import torque as troute
from app.routes.torque import NewsSignalPayload, news_signal

from .conftest import FakeTradier, FakeRequest


def _payload(**kw):
    base = dict(
        source_event_id="evt-1", headline="Fed signals pause", category="economic",
        symbol="NDX", direction="bullish", sentiment="bullish", confidence=0.8,
        rationale="test", tradier_confirmation=None, generated_at="2026-09-26T14:32:10Z",
    )
    base.update(kw)
    return NewsSignalPayload(**base)


def _cancel_payload(**kw):
    base = dict(source_event_id="evt-1-cancel", headline="retraction", symbol="NDX",
               state="cancel", cancels_event_id="evt-1")
    base.update(kw)
    return NewsSignalPayload(**base)


@pytest.fixture
def enabled(monkeypatch):
    s = troute.get_settings().model_copy(update={
        "accept_news_reactor_signals": True, "news_signal_quantity": 2,
    })
    monkeypatch.setattr(troute, "get_settings", lambda: s)
    return s


@pytest.fixture(autouse=True)
def _qualified_uids(monkeypatch):
    async def _q(tool):
        return ["op-uid-1"]
    monkeypatch.setattr(troute.supabase_admin, "get_users_with_tool", _q)


@pytest.fixture(autouse=True)
def _tools(monkeypatch):
    async def _t(uid):
        return ["torque", "news-reactor"]
    monkeypatch.setattr(troute.supabase_admin, "get_user_tools", _t)


@pytest.fixture(autouse=True)
def _reset_rate_cap():
    troute._NEWS_SIGNAL_RATE.update(window_start=0.0, count=0)
    yield
    troute._NEWS_SIGNAL_RATE.update(window_start=0.0, count=0)


@pytest.fixture(autouse=True)
def _fast_clock(monkeypatch):
    async def _open(request):
        return {"market_state": "REGULAR", "open": True}
    monkeypatch.setattr(troute, "torque_clock", _open)


@pytest.fixture(autouse=True)
def _reset_superseded():
    troute._SUPERSEDED.clear()
    yield
    troute._WATCHERS.pop("1", None)
    troute._SUPERSEDED.clear()


@pytest.fixture(autouse=True)
def _idempotency_and_correlation(monkeypatch):
    """Stand-in for Supabase: claim_news_signal + insert_torque_order (records
    into `inserted`) + get_torque_orders_by_event (reads `inserted` back by
    source_event_id — this IS what the real torque_orders correlation table
    does, just in memory for the test) + upsert_torque_watcher checkpoints."""
    seen = set()
    inserted = []

    async def _claim(source_event_id):
        if source_event_id in seen:
            return False
        seen.add(source_event_id)
        return True

    async def _insert_order(row):
        inserted.append(row)
        return True

    async def _by_event(source_event_id):
        return [r for r in inserted if r["source_event_id"] == source_event_id]

    async def _upsert_watcher(row):
        return True

    monkeypatch.setattr(troute.supabase_admin, "claim_news_signal", _claim)
    monkeypatch.setattr(troute.supabase_admin, "insert_torque_order", _insert_order)
    monkeypatch.setattr(troute.supabase_admin, "get_torque_orders_by_event", _by_event)
    monkeypatch.setattr(troute.supabase_admin, "upsert_torque_watcher", _upsert_watcher)
    return type("NS", (), {"seen": seen, "inserted": inserted})()


async def _place_entry(monkeypatch, client):
    """Step 1 of the round trip: the original directional signal, fanned out
    to the one qualified account, becomes a real (fake-broker) order."""
    async def _fake_resolve(request, user):
        return "tradier", client, "TEST123", True
    monkeypatch.setattr(troute, "resolve_broker", _fake_resolve)
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is True
    return r


async def test_cancel_of_a_never_filled_entry_just_cancels(enabled, monkeypatch):
    """Round trip: signal in -> order placed -> a second (cancel) signal
    arrives before it ever filled -> plain cancel, no market close needed."""
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}],
                         order_status={"status": "open", "exec_quantity": 0.0})
    await _place_entry(monkeypatch, client)

    async def _resolve_uid(uid):
        return "tradier", client, "TEST123"
    monkeypatch.setattr(troute, "resolve_client_for_uid", _resolve_uid)

    r = await news_signal(_cancel_payload(), FakeRequest(client))
    assert r["accepted"] is True
    assert r["results"][0]["action"] == "canceled"
    real_orders = [p for p in client.placed if "_modify" not in p]
    assert len(real_orders) == 1   # only the original entry — no close order needed


async def test_cancel_of_a_filled_entry_closes_at_market_for_the_executed_qty(enabled, monkeypatch):
    """Round trip: signal in -> order FILLS -> a second (cancel) signal
    arrives -> closes at market for exactly the executed quantity, regardless
    of current profitability or the stop-loss threshold (per spec)."""
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}],
                         order_status={"status": "filled", "exec_quantity": 2.0})
    await _place_entry(monkeypatch, client)

    async def _resolve_uid(uid):
        return "tradier", client, "TEST123"
    monkeypatch.setattr(troute, "resolve_client_for_uid", _resolve_uid)

    r = await news_signal(_cancel_payload(), FakeRequest(client))
    assert r["accepted"] is True
    result = r["results"][0]
    assert result["action"] == "closed_at_market"
    assert result["exec_quantity"] == 2.0

    real_orders = [p for p in client.placed if "_modify" not in p]
    assert len(real_orders) == 2                    # the original entry + the market close
    close = real_orders[-1]
    assert close["type"] == "market" and "price" not in close
    assert close["tag"].startswith("torqueCancelMkt")


async def test_cancel_of_a_partially_filled_entry_closes_only_what_executed(enabled, monkeypatch):
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}],
                         order_status={"status": "partially_filled", "exec_quantity": 1.0})
    await _place_entry(monkeypatch, client)

    async def _resolve_uid(uid):
        return "tradier", client, "TEST123"
    monkeypatch.setattr(troute, "resolve_client_for_uid", _resolve_uid)

    r = await news_signal(_cancel_payload(), FakeRequest(client))
    result = r["results"][0]
    assert result["action"] == "closed_at_market"
    assert result["exec_quantity"] == 1.0   # not the full order quantity of 2


async def test_cancel_supersedes_a_still_running_watcher_on_the_same_entry(enabled, monkeypatch):
    """If a TP/SL watcher happens to be actively polling this exact entry
    when the cancel signal lands, it must stop acting on it — never keep
    chasing a stop exit for a position the cancel signal already closed."""
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}],
                         order_status={"status": "filled", "exec_quantity": 2.0})
    await _place_entry(monkeypatch, client)
    w = {"entry_order_id": "1", "state": "watching_fill", "done": False}
    troute._WATCHERS["1"] = w

    async def _resolve_uid(uid):
        return "tradier", client, "TEST123"
    monkeypatch.setattr(troute, "resolve_client_for_uid", _resolve_uid)

    await news_signal(_cancel_payload(), FakeRequest(client))
    assert "1" in troute._SUPERSEDED
    assert "superseded_reason" in w


async def test_cancel_with_no_matching_signal_on_file_is_a_clean_no_op(enabled):
    r = await news_signal(_cancel_payload(cancels_event_id="evt-never-seen"), FakeRequest(FakeTradier()))
    assert r["accepted"] is False
    assert "no orders on file" in r["reason"]


async def test_cancel_missing_cancels_event_id_is_dropped(enabled):
    r = await news_signal(_cancel_payload(cancels_event_id=None), FakeRequest(FakeTradier()))
    assert r["accepted"] is False
    assert "cancels_event_id" in r["reason"]


async def test_cancel_fails_closed_when_market_is_closed(enabled, monkeypatch):
    async def _closed(request):
        return {"market_state": "CLOSED", "open": False}
    monkeypatch.setattr(troute, "torque_clock", _closed)
    r = await news_signal(_cancel_payload(), FakeRequest(FakeTradier()))
    assert r["accepted"] is False
    assert "market is closed" in r["reason"]


async def test_cancel_on_an_account_with_no_broker_connection_is_skipped_not_errored(enabled, monkeypatch):
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}],
                         order_status={"status": "filled", "exec_quantity": 2.0})
    await _place_entry(monkeypatch, client)

    async def _no_broker(uid):
        return None
    monkeypatch.setattr(troute, "resolve_client_for_uid", _no_broker)

    r = await news_signal(_cancel_payload(), FakeRequest(client))
    assert r["accepted"] is True
    assert r["results"][0]["action"] == "skipped"
