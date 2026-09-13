"""Server-rendered snap card — the surface the headless snapshot service hits
(bearer-gated, no user session). Renderer output + route auth."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, simmer_snap
from app.config import Settings
from app import simmer_watcher as sw
from app.routes import simmer as sroute


def _ready_env():
    return {"symbol": "NVDA", "expiration": "2026-10-17", "decision": "ready",
            "score": 78.0, "structure": "bull_put",
            "strikes": {"short": 170, "long": 165, "width": 5},
            "credit_fill": 0.92, "max_loss": 4.08, "pop_breakeven": 0.81,
            "expected_value": 0.121, "regime": {"state": "contango"}}


def test_render_ready_card_has_trade_block():
    html = simmer_snap.render_snap_card(_ready_env())
    assert html.startswith("<!doctype html>")
    for tok in ("NVDA", "exp 2026-10-17", "READY TO SELL", "78",
                "Bull Put Spread", "170 / 165", "$0.92", "81%",
                'data-snap="card"'):
        assert tok in html, tok


def test_render_vetoed_card_refuses():
    env = {"symbol": "NVDA", "expiration": "2026-09-14", "decision": "vetoed",
           "score": None, "veto_reasons": ["vrp_floor"]}
    html = simmer_snap.render_snap_card(env)
    assert "NO TRADE" in html and "1 gate vetoed" in html
    assert "READY TO SELL" not in html


def test_render_escapes_symbol():
    env = {**_ready_env(), "symbol": "AB<x>"}   # uppercased to AB<X> then escaped
    html = simmer_snap.render_snap_card(env)
    assert "AB<" not in html and "AB&lt;" in html


# ── Route auth (bearer-gated, HTML) ─────────────────────────────────────────
def _client(token: str) -> TestClient:
    config._cached = Settings(auth_enabled=False, simmer_api_token=token)
    app = FastAPI()
    app.include_router(sroute.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_cfg():
    yield
    config._cached = None


def test_snap_route_401_without_bearer(monkeypatch):
    monkeypatch.setitem(sw.state.latest_by_key, "NVDA|2026-10-17", _ready_env())
    r = _client("sekret").get("/simmer/snap/NVDA")
    assert r.status_code == 401


def test_snap_route_200_html_with_bearer(monkeypatch):
    monkeypatch.setitem(sw.state.latest_by_key, "NVDA|2026-10-17", _ready_env())
    r = _client("sekret").get("/simmer/snap/NVDA",
                              headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "READY TO SELL" in r.text and 'data-snap="card"' in r.text


def test_snap_route_404_unknown_symbol():
    r = _client("sekret").get("/simmer/snap/ZZZZ",
                              headers={"Authorization": "Bearer sekret"})
    assert r.status_code == 404
