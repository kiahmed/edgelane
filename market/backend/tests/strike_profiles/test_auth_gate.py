"""Strike-profile config is a server-owner tool, so when AUTH_ENABLED=true every
endpoint must require the ADMIN token specifically — a regular signed-in user
(non-admin) gets 403, anonymous gets 401. No-op when AUTH_ENABLED=false."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.config import Settings
from app.db import Database
from app.routes import strike_profiles as sp

ADMIN = "test-admin-token"


def _client(tmp_path):
    app = FastAPI()
    app.include_router(sp.router)
    db = Database(tmp_path / "x.duckdb")
    db.connect()
    app.state.db = db
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


def test_anonymous_blocked(auth_on, tmp_path):
    assert _client(tmp_path).get("/strike-profiles").status_code == 401


def test_admin_token_allowed(auth_on, tmp_path):
    r = _client(tmp_path).get("/strike-profiles", headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 200
    assert {p["symbol"] for p in r.json()["profiles"]} >= {"DEFAULT", "SPX"}


def test_wrong_token_forbidden(auth_on, tmp_path):
    assert _client(tmp_path).get(
        "/strike-profiles", headers={"X-Admin-Token": "nope"}).status_code == 401


def test_query_token_allowed(auth_on, tmp_path):
    # Browser navigation can't set headers — ?token= must work too.
    assert _client(tmp_path).get(f"/strike-profiles?token={ADMIN}").status_code == 200


def test_open_when_auth_disabled(auth_off, tmp_path):
    assert _client(tmp_path).get("/strike-profiles").status_code == 200
