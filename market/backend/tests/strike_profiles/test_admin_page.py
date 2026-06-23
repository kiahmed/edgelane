"""The /strike-profiles/admin HTML page mirrors Torque's page gate: it must NOT
inherit the JSON router's require_admin (which returns a JSON 401). Instead its
own _page_authorized returns a friendly HTML 401 when unauthorized. With
AUTH_ENABLED=true: anonymous → 401 HTML, ?token=<ADMIN> → 200 HTML. With
AUTH_ENABLED=false (dev) the gate is a no-op → 200."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.config import Settings
from app.routes import strike_profiles as sp

ADMIN = "test-admin-token"


def _client():
    app = FastAPI()
    # register the page router first (same order as main.py) so /admin matches
    # the literal page route before the API router's /{symbol} route.
    app.include_router(sp.page_router)
    app.include_router(sp.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_on(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(auth_enabled=True, admin_api_token=ADMIN))
    yield
    monkeypatch.setattr(config, "_cached", None)


@pytest.fixture
def auth_off(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(auth_enabled=False, admin_api_token=ADMIN))
    yield
    monkeypatch.setattr(config, "_cached", None)


def test_page_blocked_without_token(auth_on):
    r = _client().get("/strike-profiles/admin")
    assert r.status_code == 401
    assert "text/html" in r.headers["content-type"]
    assert "?token=" in r.text


def test_page_allowed_with_query_token(auth_on):
    r = _client().get(f"/strike-profiles/admin?token={ADMIN}")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "EdgeLane Matrix" in r.text


def test_page_allowed_with_admin_header(auth_on):
    r = _client().get("/strike-profiles/admin", headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_page_open_when_auth_disabled(auth_off):
    r = _client().get("/strike-profiles/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_admin_path_not_swallowed_by_symbol_route(auth_on):
    # /strike-profiles/admin must hit the HTML page route, not the JSON
    # /strike-profiles/{symbol} API route (which would 401 as JSON).
    r = _client().get(f"/strike-profiles/admin?token={ADMIN}")
    assert "text/html" in r.headers["content-type"]
