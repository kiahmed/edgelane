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


# ── Product entitlements ─────────────────────────────────────────────────────
def test_signup_stamps_default_product_metadata(gotrue, monkeypatch):
    """No product in the body → signup_product defaults to 'simmer' (the proxy's
    only caller) so migration 0013 seeds the simmer tool."""
    calls, install = gotrue
    monkeypatch.setattr(auth_proxy, "_post", install(200, {"id": "u-1", "email": "a@b.co"}))
    r = _client(None).post("/auth/signup", json={"email": "a@b.co", "password": "secret1"})
    assert r.status_code == 200
    path, body, _ = calls[0]
    assert path == "/signup"
    assert body["data"] == {"signup_product": "simmer"}


def test_signup_forwards_explicit_product(gotrue, monkeypatch):
    calls, install = gotrue
    monkeypatch.setattr(auth_proxy, "_post", install(200, {"id": "u-1", "email": "a@b.co"}))
    r = _client(None).post("/auth/signup",
                           json={"email": "a@b.co", "password": "secret1", "product": "market"})
    assert r.status_code == 200
    assert calls[0][1]["data"] == {"signup_product": "market"}


def test_signup_rejects_arbitrary_product_422(gotrue, monkeypatch):
    calls, install = gotrue
    monkeypatch.setattr(auth_proxy, "_post", install(200, {"id": "u-1"}))
    r = _client(None).post("/auth/signup",
                           json={"email": "a@b.co", "password": "secret1", "product": "torque"})
    assert r.status_code == 422
    assert calls == []      # never reached GoTrue


@pytest.fixture
def auth_on(monkeypatch):
    """Turn the entitlement gate ON and stub the tool lookup + revoke."""
    from app import config, supabase_admin

    s = config.Settings()
    object.__setattr__(s, "auth_enabled", True)
    monkeypatch.setattr(config, "_cached", s)

    state = {"tools": [], "revoked": []}

    async def fake_tools(uid):
        return list(state["tools"])

    async def fake_revoke(sess):
        state["revoked"].append(sess.get("access_token"))

    monkeypatch.setattr(supabase_admin, "get_user_tools", fake_tools)
    monkeypatch.setattr(auth_proxy, "_revoke", fake_revoke)
    return state


def test_login_403_when_product_tool_missing(gotrue, monkeypatch, auth_on):
    """Rule 1/3: no 'simmer' tool → no session; the just-issued token is revoked."""
    calls, install = gotrue
    auth_on["tools"] = []                                   # zero-tool profile
    monkeypatch.setattr(auth_proxy, "_post", install(200, _SESSION))
    r = _client(None).post("/auth/login", json={"email": "a@b.co", "password": "secret1"})
    assert r.status_code == 403
    assert "Simmer isn't enabled" in r.json()["detail"]
    assert "session" not in r.json()                        # never handed back
    assert auth_on["revoked"] == ["jwt.x.y"]                # best-effort revoke ran


def test_login_403_for_matrix_only_user_on_simmer(gotrue, monkeypatch, auth_on):
    """No cross-product access: a {market}-only user can't get into Simmer."""
    calls, install = gotrue
    auth_on["tools"] = ["market"]
    monkeypatch.setattr(auth_proxy, "_post", install(200, _SESSION))
    r = _client(None).post("/auth/login", json={"email": "pete@b.co", "password": "secret1"})
    assert r.status_code == 403
    assert auth_on["revoked"] == ["jwt.x.y"]


def test_login_200_when_tool_present(gotrue, monkeypatch, auth_on):
    calls, install = gotrue
    auth_on["tools"] = ["simmer"]
    monkeypatch.setattr(auth_proxy, "_post", install(200, _SESSION))
    r = _client(None).post("/auth/login", json={"email": "a@b.co", "password": "secret1"})
    assert r.status_code == 200
    assert r.json()["session"]["access_token"] == "jwt.x.y"
    assert auth_on["revoked"] == []                         # nothing revoked


def test_refresh_403_when_tool_revoked_midsession(gotrue, monkeypatch, auth_on):
    """A tool taken away mid-session can't be refreshed back into a token."""
    calls, install = gotrue
    auth_on["tools"] = []
    monkeypatch.setattr(auth_proxy, "_post", install(200, _SESSION))
    r = _client(None).post("/auth/refresh", json={"refresh_token": "r" * 20})
    assert r.status_code == 403
    assert auth_on["revoked"] == ["jwt.x.y"]
