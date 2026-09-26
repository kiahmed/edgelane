"""Signing in to Matrix requires the Matrix tool ("market").

Matrix and Simmer share one Supabase identity pool, so a valid session alone
proves nothing about WHICH product you may use. Before this, any signed-in user
— a Simmer-only customer included — got the full Matrix payload from
/snapshot. The owner's rule: no cross-product access; every product checks its
own key in profiles.tools_enabled.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, config, supabase_admin
from app.config import Settings
from app.poller import state as poller_state
from app.routes import broker as broker_route
from app.routes import snapshot as snapshot_route
from app.routes import status as status_route

ADMIN = "test-admin-token"
BEARER = {"Authorization": "Bearer any.jwt.value"}


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(auth_enabled=True, admin_api_token=ADMIN))
    # Any bearer decodes to this Supabase user; the tools are what vary.
    monkeypatch.setattr(auth, "_decode", lambda tok: {"sub": "user-123", "email": "u@example.com"})
    prev = dict(poller_state.latest_by_symbol)
    poller_state.latest_by_symbol["SPX"] = {"engine_pick": {"strategy": "bear_call"},
                                           "strategies": {}, "spot": 7790.0}
    yield
    poller_state.latest_by_symbol.clear()
    poller_state.latest_by_symbol.update(prev)
    monkeypatch.setattr(config, "_cached", None)


def _tools(monkeypatch, tools):
    async def _get(uid):
        return list(tools)
    monkeypatch.setattr(supabase_admin, "get_user_tools", _get)


@pytest.fixture
def client():
    app = FastAPI()
    for r in (snapshot_route.router, status_route.router, broker_route.router):
        app.include_router(r)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("tools", [[], ["simmer"], ["torque"]])
def test_a_signed_in_user_without_market_is_refused(client, monkeypatch, tools):
    """No tools, Simmer-only, Torque-only: none of them is Matrix."""
    _tools(monkeypatch, tools)
    assert client.get("/snapshot/SPX", headers=BEARER).status_code == 403
    assert client.get("/snapshot", headers=BEARER).status_code == 403


def test_an_entitled_user_gets_the_full_payload(client, monkeypatch):
    _tools(monkeypatch, ["market"])
    r = client.get("/snapshot/SPX", headers=BEARER)
    assert r.status_code == 200
    assert "teaser" not in r.json() and r.json()["engine_pick"]["strategy"] == "bear_call"


def test_a_refused_user_is_not_silently_downgraded_to_the_teaser(client, monkeypatch):
    """A teaser would look like a working product with data missing; the UI
    needs a clear 403 to show 'Matrix isn't enabled for this account'."""
    _tools(monkeypatch, ["simmer"])
    r = client.get("/snapshot/SPX", headers=BEARER)
    assert r.status_code == 403
    assert "market" in r.text


def test_the_admin_token_still_bypasses(client, monkeypatch):
    _tools(monkeypatch, [])
    assert client.get("/snapshot/SPX", headers={"X-Admin-Token": ADMIN}).status_code == 200


def test_no_credentials_is_still_401(client):
    assert client.get("/snapshot/SPX").status_code == 401


def test_broker_test_requires_market(client, monkeypatch):
    from app.auth import get_current_user
    _tools(monkeypatch, ["simmer"])
    client.app.dependency_overrides[get_current_user] = (
        lambda: {"id": "user-123", "auth": "supabase"})
    try:
        assert client.post("/broker/test", json={"id": "x"}).status_code == 403
    finally:
        client.app.dependency_overrides.clear()


def test_status_stays_public(client):
    """Health/metadata — no product data, so deliberately left open."""
    # (500 here only because this stripped app lacks a Tradier client — the
    # point is that it is never auth-refused.)
    assert client.get("/status").status_code not in (401, 403)
