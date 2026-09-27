"""supabase_admin.get_users_with_tool — the live entitlement query the
news-signal webhook fans out against (replacing a static TORQUE_OPERATOR_UIDS
allowlist): "who currently has this tool granted," read straight from
public.profiles.tools_enabled.
"""
from __future__ import annotations

import pytest

from app import supabase_admin
from app import config as config_module
from app.config import Settings


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, calls, status_code, payload, **kw):
        self._calls = calls
        self._status_code = status_code
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        self._calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return _FakeResp(self._status_code, self._payload)

    async def post(self, url, headers=None, params=None, json=None):
        self._calls.append({"method": "POST", "url": url, "headers": headers,
                            "params": params, "json": json})
        return _FakeResp(self._status_code, self._payload)

    async def patch(self, url, headers=None, params=None, json=None):
        self._calls.append({"method": "PATCH", "url": url, "headers": headers,
                            "params": params, "json": json})
        return _FakeResp(self._status_code, self._payload)

    async def delete(self, url, headers=None, params=None):
        self._calls.append({"method": "DELETE", "url": url, "headers": headers, "params": params})
        return _FakeResp(self._status_code, self._payload)


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    config_module._cached = Settings(
        supabase_url="https://example.supabase.co",
        supabase_service_key="service-key",
        auth_enabled=False,
    )
    yield
    config_module._cached = None


async def test_not_configured_returns_empty_list(monkeypatch):
    config_module._cached = Settings(supabase_url="", supabase_service_key="", auth_enabled=False)
    result = await supabase_admin.get_users_with_tool("news-reactor")
    assert result == []


async def test_queries_the_contains_filter_and_returns_ids(monkeypatch):
    calls = []
    payload = [{"id": "uid-1"}, {"id": "uid-2"}]
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient(calls, 200, payload, **kw))
    result = await supabase_admin.get_users_with_tool("news-reactor")
    assert result == ["uid-1", "uid-2"]
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/profiles")
    assert calls[0]["params"]["tools_enabled"] == "cs.{news-reactor}"   # Postgres array-contains filter
    assert calls[0]["params"]["select"] == "id"


async def test_no_matching_profiles_returns_empty_list(monkeypatch):
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient([], 200, [], **kw))
    result = await supabase_admin.get_users_with_tool("news-reactor")
    assert result == []


async def test_http_error_fails_closed_to_empty_list(monkeypatch):
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient([], 500, "boom", **kw))
    result = await supabase_admin.get_users_with_tool("news-reactor")
    assert result == []


async def test_network_exception_fails_closed_to_empty_list(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient", _boom)
    result = await supabase_admin.get_users_with_tool("news-reactor")
    assert result == []


# ── news-signal idempotency / order correlation / watcher persistence ──────
async def test_claim_news_signal_first_time_succeeds(monkeypatch):
    calls = []
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient(calls, 201, {}, **kw))
    assert await supabase_admin.claim_news_signal("evt-1") is True
    assert calls[0]["json"] == {"source_event_id": "evt-1"}
    assert calls[0]["url"].endswith("/news_reactor_signals")


async def test_claim_news_signal_duplicate_returns_false_not_an_error(monkeypatch, caplog):
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient([], 409, {"message": "duplicate key"}, **kw))
    assert await supabase_admin.claim_news_signal("evt-1") is False
    assert not any(r.levelname == "ERROR" for r in caplog.records)   # expected case, not an error


async def test_claim_news_signal_real_error_returns_false_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient([], 500, {"message": "boom"}, **kw))
    with caplog.at_level("ERROR"):
        assert await supabase_admin.claim_news_signal("evt-1") is False
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_claim_news_signal_not_configured_fails_closed():
    config_module._cached = Settings(supabase_url="", supabase_service_key="", auth_enabled=False)
    assert await supabase_admin.claim_news_signal("evt-1") is False


async def test_insert_torque_order_posts_the_row(monkeypatch):
    calls = []
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient(calls, 201, {}, **kw))
    row = {"source_event_id": "evt-1", "uid": "u1", "entry_order_id": "o1",
          "symbol": "NDX", "strategy": "long_call", "quantity": 1,
          "is_single": True, "entry_type": "debit", "legs": [{"a": 1}], "tick": 0.05}
    assert await supabase_admin.insert_torque_order(row) is True
    assert calls[0]["url"].endswith("/torque_orders")
    assert calls[0]["json"] == row


async def test_upsert_torque_watcher_merges_on_entry_order_id(monkeypatch):
    calls = []
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient(calls, 201, {}, **kw))
    row = {"entry_order_id": "o1", "uid": "u1", "symbol": "NDX", "strategy": "long_call",
          "legs": [{"a": 1}], "is_single": True, "entry_type": "debit", "quantity": 1,
          "tick": 0.05, "state": "stop_placed"}
    assert await supabase_admin.upsert_torque_watcher(row) is True
    assert calls[0]["params"]["on_conflict"] == "entry_order_id"
    assert calls[0]["headers"]["Prefer"] == "return=minimal,resolution=merge-duplicates"


async def test_get_active_torque_watchers_filters_on_done_false(monkeypatch):
    calls = []
    payload = [{"entry_order_id": "o1", "state": "stop_placed"}]
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient(calls, 200, payload, **kw))
    result = await supabase_admin.get_active_torque_watchers()
    assert result == payload
    assert calls[0]["params"]["done"] == "eq.false"


async def test_get_active_torque_watchers_none_on_failure_returns_empty_list(monkeypatch):
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient([], 500, "boom", **kw))
    assert await supabase_admin.get_active_torque_watchers() == []


async def test_purge_stale_torque_records_deletes_from_all_three_tables(monkeypatch):
    calls = []
    monkeypatch.setattr(supabase_admin.httpx, "AsyncClient",
                        lambda **kw: _FakeAsyncClient(calls, 204, {}, **kw))
    await supabase_admin.purge_stale_torque_records(older_than_hours=24)
    tables_hit = {c["url"].rsplit("/", 1)[-1] for c in calls}
    assert tables_hit == {"news_reactor_signals", "torque_orders", "torque_watchers"}
    watcher_call = next(c for c in calls if c["url"].endswith("torque_watchers"))
    assert watcher_call["params"]["done"] == "eq.true"
