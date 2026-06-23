"""Torque is an admin/owner tool (it trades the server's single broker account),
so when AUTH_ENABLED=true every data/action endpoint must require the admin token
or a Supabase JWT, and the page must require ?token= on navigation."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.config import Settings
from app.routes import torque as troute

from .conftest import FakeTradier

ADMIN = "test-admin-token"


def _make_client(auth_enabled: bool):
    app = FastAPI()
    app.include_router(troute.router)
    app.state.tradier = FakeTradier()
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_on(monkeypatch):
    """Force AUTH_ENABLED=true with a known admin token for the gate tests."""
    s = Settings(auth_enabled=True, admin_api_token=ADMIN)
    monkeypatch.setattr(config, "_cached", s)
    yield
    monkeypatch.setattr(config, "_cached", None)


def test_data_endpoint_blocks_anonymous(auth_on):
    c = _make_client(True)
    assert c.get("/torque/config").status_code == 401


def test_data_endpoint_allows_admin_token(auth_on):
    c = _make_client(True)
    r = c.get("/torque/config", headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 200
    assert "tickers" in r.json()


def test_data_endpoint_rejects_wrong_token(auth_on):
    c = _make_client(True)
    assert c.get("/torque/config", headers={"X-Admin-Token": "nope"}).status_code == 401


def test_page_locked_without_token(auth_on):
    c = _make_client(True)
    r = c.get("/torque")
    assert r.status_code == 401
    assert "locked" in r.text.lower()


def test_page_unlocks_with_query_token(auth_on):
    c = _make_client(True)
    r = c.get("/torque", params={"token": ADMIN})
    assert r.status_code == 200
    assert "<html" in r.text.lower() or "torque" in r.text.lower()


def test_gate_is_noop_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(auth_enabled=False))
    c = _make_client(False)
    assert c.get("/torque/config").status_code == 200
    assert c.get("/torque").status_code == 200
