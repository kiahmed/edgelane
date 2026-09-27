"""News-signal order lifecycle after submission.

Covers what the first live sandbox run exposed: Tradier accepts a submission
and rejects it a moment later (so "submitted" != "placed"); a second signal on
the SAME contract is rejected because the first bracket's exit legs already
cover it (retried once on the next expiry as its own trade); Tradier drops the
tag on OTOCO brackets (signal orders are recognised by id instead); cancels
must never be rate-capped; and every signal order's status is kept in
torque_orders, including a cancel done by hand.
"""
from __future__ import annotations

import time

import pytest

from app.routes import torque as troute
from app.routes.torque import NewsSignalPayload, news_signal, torque_cancel, torque_orders

from .conftest import FakeRequest, FakeTradier, raw_chain

CONFLICT = ("Sell order is for more shares than your current long position, please review "
            "current position quantity along with open orders for security. ")
DEV = {"id": "dev-local", "auth": "dev"}


def _payload(**kw):
    base = dict(source_event_id="evt-1", headline="h", symbol="NDX", direction="bullish",
                generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    base.update(kw)
    return NewsSignalPayload(**base)


class TwoExpiryTradier(FakeTradier):
    """Two listed expiries, each with its own chain (OCC symbols differ)."""
    def __init__(self, exps, **kw):
        super().__init__(exp=exps[0], **kw)
        self._exps = list(exps)

    async def option_expirations(self, symbol):
        return list(self._exps)

    async def options_chain(self, symbol, expiration, greeks=True):
        return raw_chain(self._spot, exp=expiration)


@pytest.fixture
def enabled(monkeypatch):
    s = troute.get_settings().model_copy(update={
        "accept_news_reactor_signals": True, "news_signal_quantity": 1,
        "devmode": False, "tradier_env": "production", "news_signal_max_per_window": 10})
    monkeypatch.setattr(troute, "get_settings", lambda: s)
    return s


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    """One qualified account, market open, in-memory Supabase."""
    troute._NEWS_SIGNAL_RATE.update(window_start=0.0, count=0)
    troute._SUPERSEDED.clear()
    db = type("DB", (), {})()
    db.seen, db.inserted, db.outcomes, db.status = set(), [], {}, {}

    async def _q(tool):
        return ["u1"]

    async def _tools(uid):
        return ["torque", "news-reactor"]

    async def _open(request):
        return {"open": True}

    async def _claim(eid, row=None):
        if eid in db.seen:
            return False
        db.seen.add(eid)
        return True

    async def _insert(row):
        db.inserted.append(row)
        return True

    async def _outcome(eid, outcome, reason):
        db.outcomes[eid] = (outcome, reason)
        return True

    async def _by_event(eid):
        return [r for r in db.inserted if r["source_event_id"] == eid]

    async def _status(oid, status, reason=None):
        db.status[str(oid)] = (status, reason)
        return True

    async def _noop(row):
        return True
    for name, fn in (("get_users_with_tool", _q), ("get_user_tools", _tools),
                     ("claim_news_signal", _claim), ("insert_torque_order", _insert),
                     ("record_news_signal_outcome", _outcome),
                     ("get_torque_orders_by_event", _by_event),
                     ("update_torque_order_status", _status), ("upsert_torque_watcher", _noop)):
        monkeypatch.setattr(troute.supabase_admin, name, fn)
    monkeypatch.setattr(troute, "torque_clock", _open)
    yield db
    troute._NEWS_SIGNAL_RATE.update(window_start=0.0, count=0)
    troute._SUPERSEDED.clear()


def _broker(monkeypatch, client, statuses):
    """Route every per-account broker call to `client`; `statuses` maps an
    order id to the (status, reason) the post-submit confirmation reads."""
    async def _resolve(request, user):
        return "tradier", client, "ACC", True
    monkeypatch.setattr(troute, "resolve_broker", _resolve)

    async def _confirm(uid, oid):
        return statuses.get(str(oid), ("pending", None))
    monkeypatch.setattr(troute, "_confirm_entry_status", _confirm)


# ── submitted is not placed ─────────────────────────────────────────────────
async def test_broker_rejection_after_submit_is_reported_as_rejected(enabled, monkeypatch, _world):
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])
    _broker(monkeypatch, client, {"1": ("rejected", "insufficient buying power")})
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False
    assert r["results"][0]["rejected"] is True and "insufficient buying power" in r["reason"]
    assert _world.outcomes["evt-1"][0] == "rejected"
    row = _world.inserted[0]
    assert row["status"] == "rejected" and row["status_reason"] == "insufficient buying power"


async def test_immediate_rejection_is_not_reported_as_accepted(enabled, monkeypatch, _world):
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "rejected",
                                                     "reason_description": "bad"}}])
    _broker(monkeypatch, client, {})
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False and r["results"][0]["rejected"] is True


async def test_live_order_records_status_and_expiration(enabled, monkeypatch, _world):
    client = FakeTradier(place_responses=[{"order": {"id": 1, "status": "ok"}}])
    _broker(monkeypatch, client, {"1": ("pending", None)})
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is True
    row = _world.inserted[0]
    assert row["status"] == "pending" and row["attempt"] == 1 and row["expiration"]


# ── same-contract conflict → next expiry ────────────────────────────────────
async def test_same_contract_conflict_retries_once_on_the_next_expiry(enabled, monkeypatch, _world):
    client = TwoExpiryTradier(["2030-01-02", "2030-01-03"], place_responses=[
        {"order": {"id": 1, "status": "ok"}}, {"order": {"id": 2, "status": "ok"}}])
    _broker(monkeypatch, client, {"1": ("rejected", CONFLICT), "2": ("pending", None)})
    r = await news_signal(_payload(), FakeRequest(client))

    assert r["accepted"] is True
    res = r["results"][0]
    assert res["attempt"] == 2 and res["expiration"] == "2030-01-03"
    assert "long position" in res["retried_after"]
    first, retry = _world.inserted
    assert first["status"] == "rejected" and first["expiration"] == "2030-01-02"
    assert retry["attempt"] == 2 and retry["expiration"] == "2030-01-03"
    assert "300103" in retry["legs"][0]["symbol"]           # really a different contract
    assert first["legs"][0]["symbol"] != retry["legs"][0]["symbol"]
    assert _world.outcomes["evt-1"][0] == "placed"


async def test_cancel_after_a_retry_unwinds_the_retried_order(enabled, monkeypatch, _world):
    client = TwoExpiryTradier(["2030-01-02", "2030-01-03"], place_responses=[
        {"order": {"id": 1, "status": "ok"}}, {"order": {"id": 2, "status": "ok"}}],
        order_status_by_id={"1": {"status": "rejected"}, "2": {"status": "open", "exec_quantity": 0.0}})
    _broker(monkeypatch, client, {"1": ("rejected", CONFLICT), "2": ("pending", None)})
    await news_signal(_payload(), FakeRequest(client))

    async def _uid(uid):
        return "tradier", client, "ACC"
    monkeypatch.setattr(troute, "resolve_client_for_uid", _uid)
    r = await news_signal(NewsSignalPayload(source_event_id="evt-1-cancel", headline="h",
                                            symbol="NDX", state="cancel", cancels_event_id="evt-1"),
                          FakeRequest(client))
    by_order = {x["entry_order_id"]: x["action"] for x in r["results"]}
    assert by_order["2"] == "canceled"                    # the live retried order was unwound
    assert client._order_status_by_id["2"]["status"] == "canceled"
    assert _world.status["2"][0] == "canceled"             # and recorded in torque_orders


async def test_conflict_with_no_later_expiry_stays_rejected(enabled, monkeypatch, _world):
    client = TwoExpiryTradier(["2030-01-02"], place_responses=[{"order": {"id": 1, "status": "ok"}}])
    _broker(monkeypatch, client, {"1": ("rejected", CONFLICT)})
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False
    assert "no usable next expiry" in r["results"][0]["reason"]
    assert len(_world.inserted) == 1


async def test_other_rejections_are_not_retried(enabled, monkeypatch, _world):
    client = TwoExpiryTradier(["2030-01-02", "2030-01-03"], place_responses=[
        {"order": {"id": 1, "status": "ok"}}])
    _broker(monkeypatch, client, {"1": ("rejected", "insufficient buying power")})
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False and len([p for p in client.placed if "_modify" not in p]) == 1


def test_position_conflict_matches_tradiers_actual_message():
    assert troute._is_position_conflict(CONFLICT) is True
    assert troute._is_position_conflict("insufficient buying power") is False
    assert troute._is_position_conflict(None) is False


# ── cancels are never rate-capped ───────────────────────────────────────────
async def test_cancel_is_processed_even_when_the_rate_cap_is_exhausted(enabled, monkeypatch, _world):
    troute._NEWS_SIGNAL_RATE.update(window_start=time.time(), count=enabled.news_signal_max_per_window)
    r = await news_signal(_payload(), FakeRequest(FakeTradier()))
    assert "rate cap" in r["reason"]                      # entries ARE capped
    r = await news_signal(NewsSignalPayload(source_event_id="evt-9-cancel", headline="h",
                                            symbol="NDX", state="cancel", cancels_event_id="evt-9"),
                          FakeRequest(FakeTradier()))
    assert "rate cap" not in (r.get("reason") or "")      # cancels are not
    assert "no orders on file" in r["reason"]


# ── orders panel: recognise signal orders by id, sync their status ──────────
async def test_orders_panel_flags_untagged_signal_orders_and_syncs_status(monkeypatch, _world):
    async def _recent(hours=24):
        return [{"entry_order_id": "77", "status": "pending"}]
    monkeypatch.setattr(troute.supabase_admin, "get_recent_torque_orders", _recent)
    client = FakeTradier(orders=[
        {"id": 77, "class": "otoco", "status": "canceled", "tag": None, "symbol": "SPY"},
        {"id": 78, "class": "option", "status": "filled", "tag": None, "symbol": "SPY"},
    ])
    r = await torque_orders(FakeRequest(client), account_id="T", user=DEV)
    hist = {h["id"]: h for h in r["history"]}
    assert hist["77"]["news_signal"] is True               # untagged OTOCO still recognised
    assert "78" not in hist                                # an unrelated untagged order is not
    assert _world.status["77"][0] == "canceled"            # e.g. cancelled directly at the broker


async def test_orders_panel_never_overwrites_a_recorded_torque_action(monkeypatch, _world):
    async def _recent(hours=24):
        return [{"entry_order_id": "77", "status": "closed_at_market"}]
    monkeypatch.setattr(troute.supabase_admin, "get_recent_torque_orders", _recent)
    client = FakeTradier(orders=[{"id": 77, "class": "otoco", "status": "filled", "tag": None}])
    await torque_orders(FakeRequest(client), account_id="T", user=DEV)
    assert "77" not in _world.status


async def test_manual_cancel_in_torque_records_canceled(monkeypatch, _world):
    async def _recent(hours=24):
        return [{"entry_order_id": "77", "status": "pending"}]
    monkeypatch.setattr(troute.supabase_admin, "get_recent_torque_orders", _recent)
    client = FakeTradier(order_status={"status": "pending"})
    await torque_cancel("77", FakeRequest(client), account_id="T", user=DEV)
    assert _world.status["77"] == ("canceled", "cancelled manually in Torque")


async def test_cancel_skips_a_rejected_attempt_and_unwinds_only_the_live_retry(enabled, monkeypatch, _world):
    """A retried signal owns two rows; the cancel must not touch (or relabel)
    the rejected first attempt."""
    client = TwoExpiryTradier(["2030-01-02", "2030-01-03"], place_responses=[
        {"order": {"id": 1, "status": "ok"}}, {"order": {"id": 2, "status": "ok"}}],
        order_status_by_id={"1": {"status": "rejected"}, "2": {"status": "open", "exec_quantity": 0.0}})
    _broker(monkeypatch, client, {"1": ("rejected", CONFLICT), "2": ("pending", None)})
    await news_signal(_payload(), FakeRequest(client))

    async def _uid(uid):
        return "tradier", client, "ACC"
    monkeypatch.setattr(troute, "resolve_client_for_uid", _uid)
    r = await news_signal(NewsSignalPayload(source_event_id="evt-1-cancel", headline="h",
                                            symbol="NDX", state="cancel", cancels_event_id="evt-1"),
                          FakeRequest(client))
    by_order = {x["entry_order_id"]: x["action"] for x in r["results"]}
    assert by_order == {"2": "canceled", "1": "already_closed"}
    assert "1" not in _world.status                       # rejected attempt's status left as-is
    assert _world.status["2"][0] == "canceled"


async def test_repeated_conflicts_walk_forward_until_an_expiry_takes(enabled, monkeypatch, _world):
    """With a cap of 2+: 01-02 and 01-03 both already taken → lands on the third."""
    s = enabled.model_copy(update={"news_signal_max_expiry_retries": 4})
    monkeypatch.setattr(troute, "get_settings", lambda: s)
    client = TwoExpiryTradier(["2030-01-02", "2030-01-03", "2030-01-04"], place_responses=[
        {"order": {"id": n, "status": "ok"}} for n in (1, 2, 3)])
    _broker(monkeypatch, client, {"1": ("rejected", CONFLICT), "2": ("rejected", CONFLICT),
                                  "3": ("pending", None)})
    r = await news_signal(_payload(), FakeRequest(client))
    res = r["results"][0]
    assert r["accepted"] is True and res["attempt"] == 3 and res["expiration"] == "2030-01-04"
    assert [row["status"] for row in _world.inserted] == ["rejected", "rejected", "pending"]


async def test_walk_stops_at_the_configured_cap(enabled, monkeypatch, _world):
    s = enabled.model_copy(update={"news_signal_max_expiry_retries": 1})
    monkeypatch.setattr(troute, "get_settings", lambda: s)
    client = TwoExpiryTradier(["2030-01-02", "2030-01-03", "2030-01-04"], place_responses=[
        {"order": {"id": n, "status": "ok"}} for n in (1, 2, 3)])
    _broker(monkeypatch, client, {"1": ("rejected", CONFLICT), "2": ("rejected", CONFLICT)})
    r = await news_signal(_payload(), FakeRequest(client))
    assert r["accepted"] is False and "after 1 later expiries" in r["results"][0]["reason"]
    assert len(_world.inserted) == 2                       # never tried the third expiry
