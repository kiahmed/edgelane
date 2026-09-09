"""Read-only Simmer integration API (soljet-postiz upstream contract):
bearer-token auth (SIMMER_API_TOKEN), /simmer/ready + /simmer/state/{sym}.
Separate from the user-JWT surface — a bearer token, not a login."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.config import Settings
from app import simmer_watcher as sw
from app.routes import simmer as sroute

from .conftest import readiness_env

TOKEN = "smr-secret-token"


def _client(db=None) -> TestClient:
    app = FastAPI()
    app.include_router(sroute.router)
    if db is not None:
        app.state.db = db
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def token_on(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(simmer_api_token=TOKEN))
    yield


def _seed(env: dict) -> None:
    sw.state.latest_by_key[f"{env['symbol']}|{env['expiration']}"] = env


def _auth(tok=TOKEN):
    return {"Authorization": f"Bearer {tok}"}


# ── bearer auth ─────────────────────────────────────────────────────────────
def test_ready_401_without_token(token_on):
    assert _client().get("/simmer/ready").status_code == 401


def test_ready_401_wrong_token(token_on):
    assert _client().get("/simmer/ready", headers=_auth("nope")).status_code == 401


def test_ready_401_when_server_token_unset(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings())      # blank token → closed
    assert _client().get("/simmer/ready", headers=_auth()).status_code == 401


def test_state_401_without_token(token_on):
    assert _client().get("/simmer/state/NVDA").status_code == 401


# ── /simmer/ready ───────────────────────────────────────────────────────────
def test_ready_returns_only_ready_names(token_on):
    _seed(readiness_env(symbol="NVDA", score=85))                 # ready
    _seed(readiness_env(symbol="AMD", expiration="2026-10-16", score=55))  # watch
    r = _client().get("/simmer/ready", headers=_auth())
    assert r.status_code == 200
    syms = [row["symbol"] for row in r.json()["ready"]]
    assert syms == ["NVDA"] and r.json()["count"] == 1


def test_ready_since_filter(token_on):
    _seed(readiness_env(symbol="NVDA", score=85))   # computed_at 2026-08-15T14:00:00Z
    after = _client().get("/simmer/ready?since=2026-08-16T00:00:00Z", headers=_auth())
    assert after.json()["ready"] == []              # nothing changed since
    before = _client().get("/simmer/ready?since=2026-08-14T00:00:00Z", headers=_auth())
    assert [r["symbol"] for r in before.json()["ready"]] == ["NVDA"]


# ── /simmer/state/{sym} ─────────────────────────────────────────────────────
def test_state_card_block(token_on):
    _seed(readiness_env(symbol="NVDA", score=85))
    r = _client().get("/simmer/state/NVDA?block=card", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["block"] == "card"
    assert body["data"]["symbol"] == "NVDA"
    assert body["data"]["decision"] == "ready"


def test_state_score_block_has_components(token_on):
    _seed(readiness_env(symbol="NVDA", score=85))
    r = _client().get("/simmer/state/NVDA?block=score", headers=_auth())
    assert r.status_code == 200
    assert "components" in r.json()["data"]


def test_state_evolution_falls_back_without_db(token_on):
    _seed(readiness_env(symbol="NVDA", score=85))
    r = _client().get("/simmer/state/NVDA?block=evolution", headers=_auth())
    assert r.status_code == 200
    assert len(r.json()["data"]["history"]) == 1        # single current point


def test_state_invalid_symbol_422(token_on):
    assert _client().get("/simmer/state/nvda$/",
                         headers=_auth()).status_code in (404, 422)
    assert _client().get("/simmer/state/TOOLONGSYMBOL",
                         headers=_auth()).status_code == 422


def test_state_invalid_block_422(token_on):
    _seed(readiness_env(symbol="NVDA", score=85))
    r = _client().get("/simmer/state/NVDA?block=secrets", headers=_auth())
    assert r.status_code == 422


def test_state_404_when_no_readiness(token_on):
    r = _client().get("/simmer/state/TSLA?block=card", headers=_auth())
    assert r.status_code == 404
