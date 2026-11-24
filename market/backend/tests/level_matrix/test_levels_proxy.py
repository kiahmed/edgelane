"""/levels/* is a four-GET allow-list proxy behind the /snapshot gate (§3.3)."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import level_pick
from app.routes import levels_proxy
from app.session_auth import require_teaser_session

from .conftest import use_settings

BASE = "http://le-test.internal:9911"


@pytest.fixture
def upstream(monkeypatch):
    """Fake read listener. Records every (url, params, timeout) it was asked for."""
    calls: list[tuple] = []
    reply = {"status": 200, "body": {"ok": True}, "exc": None}

    async def _get(url, params, timeout):
        calls.append((url, params, timeout))
        if reply["exc"] is not None:
            raise reply["exc"]
        return reply["status"], reply["body"]

    monkeypatch.setattr(level_pick, "_http_get", _get)
    return calls, reply


def _client(full: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(levels_proxy.router)
    app.dependency_overrides[require_teaser_session] = (
        lambda: {"full": full, "kind": "user" if full else "anon", "id": "x"})
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    use_settings(monkeypatch, level_engine_read_url=BASE)


@pytest.mark.parametrize("path,upstream_path,params", [
    ("/levels/decision/SPX", "/levels/decision/SPX", None),
    ("/levels/day/2026-11-23", "/levels/day/2026-11-23", None),
    ("/levels/trace/SPX", "/levels/trace/SPX", None),
    ("/levels/trace/SPX?limit=5", "/levels/trace/SPX", {"limit": 5}),
    ("/levels/stats/SPX", "/levels/stats/SPX", None),
    ("/levels/stats/SPX?limit=200", "/levels/stats/SPX", {"limit": 200}),
])
def test_the_four_gets_are_forwarded_to_the_configured_base_only(upstream, path,
                                                                  upstream_path, params):
    calls, reply = upstream
    reply["body"] = {"symbol": "SPX", "decision": None}
    r = _client().get(path, headers={"Cookie": "sb=secret", "Authorization": "Bearer t"})
    assert r.status_code == 200
    assert r.json() == {"symbol": "SPX", "decision": None}
    assert len(calls) == 1
    url, p, timeout = calls[0]
    assert url == BASE + upstream_path            # fixed template on the read base
    assert p == params
    assert timeout <= 2.0


@pytest.mark.parametrize("method,path", [
    ("GET", "/levels"),
    ("GET", "/levels/"),
    ("GET", "/levels/admin"),
    ("GET", "/levels/levels/add"),
    ("GET", "/levels/decision/SPX/extra"),
    ("GET", "/levels/decision"),
    ("GET", "/healthz/../levels/x"),
    ("POST", "/levels/decision/SPX"),
    ("PUT", "/levels/day/2026-11-23"),
    ("DELETE", "/levels/day/2026-11-23"),
    ("PATCH", "/levels/trace/SPX"),
    ("POST", "/levels/levels"),
])
def test_anything_else_under_levels_is_404(upstream, method, path):
    calls, _ = upstream
    r = _client().request(method, path)
    assert r.status_code == 404
    assert calls == []


@pytest.mark.parametrize("path", [
    "/levels/decision/spx",
    "/levels/decision/SPXXXXX",
    "/levels/decision/SP1",
    "/levels/decision/%2E%2E",
    "/levels/trace/S%2FX",
    "/levels/day/2026-1-01",
    "/levels/day/20261123",
    "/levels/day/2026-11-23x",
    "/levels/day/%2E%2E%2Fadmin",
    "/levels/trace/SPX?limit=abc",
    "/levels/trace/SPX?limit=-1",
])
def test_bad_symbol_date_or_limit_is_404_and_never_forwarded(upstream, path):
    calls, _ = upstream
    assert _client().get(path).status_code == 404
    assert calls == []


@pytest.mark.parametrize("path", ["/levels/decision/SPX", "/levels/day/2026-11-23",
                                  "/levels/trace/SPX"])
def test_teaser_session_is_blocked(upstream, path):
    calls, _ = upstream
    assert _client(full=False).get(path).status_code == 403
    assert calls == []


def test_unauthenticated_is_refused_by_the_snapshot_gate(upstream, monkeypatch):
    use_settings(monkeypatch, level_engine_read_url=BASE, auth_enabled=True,
                 admin_api_token="adm")
    app = FastAPI()
    app.include_router(levels_proxy.router)
    calls, _ = upstream
    r = TestClient(app, raise_server_exceptions=False).get("/levels/decision/SPX")
    assert r.status_code == 401
    assert calls == []


@pytest.mark.parametrize("exc,status", [
    (httpx.ConnectError("refused"), None),
    (httpx.ReadTimeout("slow"), None),
    (None, 500),
    (None, 502),
])
def test_upstream_down_is_503(upstream, exc, status):
    calls, reply = upstream
    reply["exc"] = exc
    if status:
        reply["status"], reply["body"] = status, {"detail": "boom"}
    r = _client().get("/levels/decision/SPX")
    assert r.status_code == 503
    assert r.json() == {"error": "level engine unavailable"}


def test_upstream_404_is_mirrored_without_its_body(upstream):
    calls, reply = upstream
    reply["status"], reply["body"] = 404, {"detail": "Not Found", "leak": "x"}
    r = _client().get("/levels/decision/NDX")
    assert r.status_code == 404
    assert "leak" not in r.text


def test_blank_base_url_is_503_not_a_relative_call(upstream, monkeypatch):
    use_settings(monkeypatch, level_engine_read_url="")
    calls, _ = upstream
    r = _client().get("/levels/decision/SPX")
    assert r.status_code == 503
    assert calls == []


def test_the_real_app_mounts_the_proxy(upstream):
    """Through the real app (no lifespan): allow-listed GET forwards, the rest 404."""
    from app.main import app
    calls, reply = upstream
    reply["body"] = {"symbol": "SPX", "decision": None}
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/levels/decision/SPX").status_code == 200
    assert c.get("/levels/admin").status_code == 404
    assert c.post("/levels/decision/SPX").status_code == 404
    assert [u for u, _, _ in calls] == [BASE + "/levels/decision/SPX"]
