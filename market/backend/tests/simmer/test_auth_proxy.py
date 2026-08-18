"""Auth proxy — the browser's ONLY identity surface. Supabase is reached
exclusively server-side; these tests pin the contract the frontend builds on."""
from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import auth_proxy


def _client(handler) -> TestClient:
    app = FastAPI()
    app.include_router(auth_proxy.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def gotrue(monkeypatch):
    """Fake GoTrue: patch the proxy's upstream POST with a canned responder."""
    calls: list[tuple] = []

    def install(status=200, body=None):
        async def fake_post(path, json_body, params=None, client_ip=None):
            calls.append((path, json_body, params))
            if status >= 400:
                r = httpx.Response(status, json=body or {"msg": "nope"})
                raise auth_proxy._passthrough_error(r)
            return body or {}
        return fake_post
    return calls, install


_SESSION = {"access_token": "jwt.x.y", "refresh_token": "r" * 20,
            "expires_in": 3600, "expires_at": 1900000000,
            "user": {"id": "u-1", "email": "a@b.co"}}


def test_login_returns_normalized_session(gotrue, monkeypatch):
    calls, install = gotrue
    monkeypatch.setattr(auth_proxy, "_post", install(200, _SESSION))
    r = _client(None).post("/auth/login", json={"email": "A@B.co ", "password": "secret1"})
    assert r.status_code == 200
    s = r.json()["session"]
    assert s["access_token"] == "jwt.x.y" and s["user"]["email"] == "a@b.co"
    path, body, params = calls[0]
    assert path == "/token" and params == {"grant_type": "password"}
    assert body["email"] == "a@b.co"          # normalized lowercase/stripped


def test_login_passthrough_upstream_message(gotrue, monkeypatch):
    calls, install = gotrue
    monkeypatch.setattr(auth_proxy, "_post",
                        install(400, {"error_description": "Invalid login credentials"}))
    r = _client(None).post("/auth/login", json={"email": "a@b.co", "password": "wrongpw"})
    assert r.status_code == 400
    assert "Invalid login credentials" in r.json()["detail"]


def test_signup_confirmation_required_when_no_session(gotrue, monkeypatch):
    calls, install = gotrue
    monkeypatch.setattr(auth_proxy, "_post", install(200, {"id": "u-1", "email": "a@b.co"}))
    r = _client(None).post("/auth/signup", json={"email": "a@b.co", "password": "secret1"})
    assert r.status_code == 200
    assert r.json() == {"session": None, "confirmation_required": True}


def test_refresh_rotates(gotrue, monkeypatch):
    calls, install = gotrue
    monkeypatch.setattr(auth_proxy, "_post", install(200, _SESSION))
    r = _client(None).post("/auth/refresh", json={"refresh_token": "r" * 20})
    assert r.status_code == 200
    assert calls[0][2] == {"grant_type": "refresh_token"}


def test_logout_always_200_even_unconfigured():
    r = _client(None).post("/auth/logout", json={"refresh_token": "x" * 12})
    assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize("email", ["nope", "@b.co", "a@", "a b@c.co"])
def test_email_shape_422(email):
    r = _client(None).post("/auth/login", json={"email": email, "password": "secret1"})
    assert r.status_code == 422


def test_short_password_422():
    r = _client(None).post("/auth/login", json={"email": "a@b.co", "password": "abc"})
    assert r.status_code == 422
