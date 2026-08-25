"""Simmer route auth gate (the 7-case Torque pattern) + the analyze-equals-
watcher-path invariant. `/simmer/*` must require the admin token or a Supabase
user entitled to the "simmer" tool — a bare JWT is NOT enough."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.config import Settings
from app import simmer_watcher as sw
from app import supabase_admin
from app.routes import simmer as sroute

from .conftest import EXP, FakeSimmerTradier

ADMIN = "test-admin-token"


def _make_client(tradier=None, db=None, user: dict | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(sroute.router)
    app.state.tradier = tradier or FakeSimmerTradier()
    if db is not None:
        app.state.db = db
    if user is not None:
        from app.auth import get_current_user
        app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_on(monkeypatch):
    s = Settings(auth_enabled=True, admin_api_token=ADMIN)
    monkeypatch.setattr(config, "_cached", s)
    yield
    monkeypatch.setattr(config, "_cached", None)


# ── The auth-gate pattern ───────────────────────────────────────────────────
def test_blocks_anonymous(auth_on):
    assert _make_client().get("/simmer/config").status_code == 401


def test_allows_admin_token(auth_on):
    r = _make_client().get("/simmer/config", headers={"X-Admin-Token": ADMIN})
    assert r.status_code == 200
    body = r.json()
    assert "engine_version" in body and "locked_gates" in body


def test_rejects_wrong_token(auth_on):
    r = _make_client().get("/simmer/config", headers={"X-Admin-Token": "nope"})
    assert r.status_code == 401


def test_plain_supabase_user_rejected(auth_on, monkeypatch):
    """Signed-in but NOT entitled to 'simmer' → 403 (a bare JWT is not enough)."""
    from app.auth import get_current_user
    async def _no_tools(uid):
        return []
    monkeypatch.setattr(supabase_admin, "get_user_tools", _no_tools)
    c = _make_client()
    c.app.dependency_overrides[get_current_user] = \
        lambda: {"id": "user-123", "auth": "supabase"}
    try:
        assert c.get("/simmer/config").status_code == 403
    finally:
        c.app.dependency_overrides.clear()


def test_entitled_supabase_user_allowed(auth_on, monkeypatch):
    from app.auth import get_current_user
    async def _has_simmer(uid):
        return ["simmer"]
    monkeypatch.setattr(supabase_admin, "get_user_tools", _has_simmer)
    c = _make_client()
    c.app.dependency_overrides[get_current_user] = \
        lambda: {"id": "user-123", "auth": "supabase"}
    try:
        assert c.get("/simmer/config").status_code == 200
    finally:
        c.app.dependency_overrides.clear()


def test_torque_entitlement_does_not_grant_simmer(auth_on, monkeypatch):
    from app.auth import get_current_user
    async def _torque_only(uid):
        return ["torque", "market"]
    monkeypatch.setattr(supabase_admin, "get_user_tools", _torque_only)
    c = _make_client()
    c.app.dependency_overrides[get_current_user] = \
        lambda: {"id": "user-123", "auth": "supabase"}
    try:
        assert c.get("/simmer/config").status_code == 403
    finally:
        c.app.dependency_overrides.clear()


def test_gate_is_noop_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(auth_enabled=False))
    c = _make_client()
    assert c.get("/simmer/config").status_code == 200
    assert c.get("/simmer/status").status_code == 200


def test_every_data_route_is_gated(auth_on):
    """No /simmer route may be reachable anonymously."""
    c = _make_client()
    for path in ("/simmer/config", "/simmer/status", "/simmer/watchlist",
                 "/simmer/analyze/NVDA", "/simmer/expirations/NVDA",
                 "/simmer/alerts", "/simmer/outcomes/summary",
                 "/simmer/validate/NVDA", "/simmer/settings",
                 "/simmer/news/NVDA"):
        assert c.get(path).status_code == 401, path
    assert c.post("/simmer/watchlist", json={"symbol": "NVDA"}).status_code == 401
    assert c.post("/simmer/alerts/ack", json={"ids": ["x"]}).status_code == 401
    assert c.post("/simmer/settings", json={"min_score": 80}).status_code == 401


# ── analyze == watcher path ─────────────────────────────────────────────────
@pytest.fixture
def auth_off(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(auth_enabled=False))
    yield
    monkeypatch.setattr(config, "_cached", None)


def test_analyze_route_calls_the_watcher_function(auth_off, monkeypatch):
    """During MARKET HOURS the route MUST delegate to analyze_symbol with
    persist=False (on-demand, no engine-verdict rows) — the paths can't diverge."""
    monkeypatch.setattr(sw.state, "market_open", True)
    calls = {}

    async def fake_analyze(tradier, db, symbol, expiration=None, **kw):
        calls.update(symbol=symbol, expiration=expiration,
                     persist=kw.get("persist"))
        return {"symbol": symbol.upper(), "sentinel": True}

    monkeypatch.setattr(sw, "analyze_symbol", fake_analyze)
    c = _make_client()
    r = c.get("/simmer/analyze/nvda", params={"expiration": EXP})
    assert r.status_code == 200
    assert r.json() == {"symbol": "NVDA", "sentinel": True}
    assert calls == {"symbol": "nvda", "expiration": EXP, "persist": False}


async def test_analyze_route_matches_direct_watcher_call(auth_off, fresh_db, monkeypatch):
    """End-to-end equivalence on the REAL engine (market hours): the route's
    envelope must equal a direct analyze_symbol(...) call on every field."""
    monkeypatch.setattr(sw.state, "market_open", True)
    tradier = FakeSimmerTradier()
    direct = await sw.analyze_symbol(tradier, fresh_db, "NVDA", EXP,
                                     regime=None, persist=False)
    c = _make_client(tradier=FakeSimmerTradier(), db=fresh_db)
    r = c.get("/simmer/analyze/NVDA", params={"expiration": EXP})
    assert r.status_code == 200
    routed = r.json()
    for k in ("symbol", "expiration", "decision", "score", "structure",
              "veto_reasons", "engine_version"):
        assert routed[k] == direct[k], k
    # on-demand analysis must not write engine-verdict rows
    assert fresh_db.latest_simmer_readiness("NVDA", EXP) is None


def test_analyze_route_frozen_when_market_closed(auth_off, monkeypatch):
    """Market CLOSED with an existing readiness → serve the frozen verdict; never
    recompute off dead after-hours quotes."""
    monkeypatch.setattr(sw.state, "market_open", False)
    frozen = {"symbol": "NVDA", "expiration": EXP, "decision": "watch",
              "score": 55.0, "rehydrated": True}
    monkeypatch.setattr(sw.state, "latest_by_key", {f"NVDA|{EXP}": frozen})

    async def boom(*a, **k):
        raise AssertionError("must not recompute when frozen")

    monkeypatch.setattr(sw, "analyze_symbol", boom)
    r = _make_client().get("/simmer/analyze/NVDA", params={"expiration": EXP})
    assert r.status_code == 200
    assert r.json()["score"] == 55.0 and r.json()["rehydrated"] is True


def test_analyze_route_never_persists_even_when_closed(auth_off, monkeypatch):
    """Market CLOSED with NO stored readiness → return a PROVISIONAL compute so
    the card doesn't hang, but the route NEVER persists (no calibration pollution
    from walking listed expirations off-hours). Persistence is the sweep's job."""
    monkeypatch.setattr(sw.state, "market_open", False)
    monkeypatch.setattr(sw.state, "latest_by_key", {})
    calls = {}

    async def fake_analyze(tradier, db, symbol, expiration=None, **kw):
        calls.update(symbol=symbol, persist=kw.get("persist"))
        return {"symbol": symbol.upper(), "expiration": expiration, "decision": "ready"}

    monkeypatch.setattr(sw, "analyze_symbol", fake_analyze)
    r = _make_client().get("/simmer/analyze/NVDA", params={"expiration": EXP})
    assert r.status_code == 200
    assert calls == {"symbol": "NVDA", "persist": False}   # provisional, unpersisted


def test_analyze_unknown_expiration_is_422(auth_off):
    c = _make_client()
    r = c.get("/simmer/analyze/NVDA", params={"expiration": "2031-01-17"})
    assert r.status_code == 422


# ── The rest of the surface (smoke, auth off) ───────────────────────────────
def test_expirations_marks_window_and_recommended(auth_off):
    c = _make_client()
    r = c.get("/simmer/expirations/NVDA")
    assert r.status_code == 200
    rows = r.json()["expirations"]
    assert rows and rows[0]["date"] == EXP
    assert any(e["recommended"] for e in rows)


def test_status_mirrors_watcher_state(auth_off):
    sw.state.watched_count = 3
    sw.state.last_sweep_at = "2026-08-15T14:00:00Z"
    r = _make_client().get("/simmer/status")
    assert r.status_code == 200
    body = r.json()
    assert body["alive"] is True
    assert body["watched_count"] == 3
    assert body["last_sweep_at"] == "2026-08-15T14:00:00Z"


def test_validate_rejects_ineligible_index(auth_off):
    r = _make_client().get("/simmer/validate/SPX")
    assert r.status_code == 422        # index products are Matrix's domain


def test_watchlist_post_writes_for_real_user(auth_off, monkeypatch):
    """API-first data plane: POST validates then upserts on behalf of the
    VERIFIED user. The browser never writes Supabase tables directly."""
    wrote = []
    async def fake_upsert(table, row, on_conflict):
        wrote.append((table, row, on_conflict))
        return True
    monkeypatch.setattr(supabase_admin, "upsert_row", fake_upsert)
    c = _make_client(user={"id": "11111111-2222-3333-4444-555555555555",
                           "auth": "supabase"})
    r = c.post("/simmer/watchlist", json={"symbol": "NVDA", "expiration": EXP})
    assert r.status_code == 200
    assert r.json()["written"] is True
    assert wrote == [("simmer_watchlist",
                      {"user_id": "11111111-2222-3333-4444-555555555555",
                       "symbol": "NVDA", "active": True, "expiration": EXP},
                      "user_id,symbol")]


def test_watchlist_post_rejects_bypass_identities(auth_off, monkeypatch):
    """admin/dev bypass identities own no watchlist rows — a write must 422
    with a clear message, never silently attribute rows to 'admin'."""
    called = []
    async def fake_upsert(table, row, on_conflict):
        called.append(row)
        return True
    monkeypatch.setattr(supabase_admin, "upsert_row", fake_upsert)
    c = _make_client()                      # dev-local bypass identity
    r = c.post("/simmer/watchlist", json={"symbol": "NVDA", "expiration": EXP})
    assert r.status_code == 422
    assert "signed-in" in r.json()["detail"]
    assert called == []
