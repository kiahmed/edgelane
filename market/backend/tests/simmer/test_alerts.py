"""Alert state machine (hysteresis), per-user fan-out thresholds, and the
settings enforcement path (locked-gate rejection, short-delta clamp)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import config
from app.config import Settings
from app import simmer_watcher as sw
from app import supabase_admin
from app.routes import simmer as sroute

from .conftest import EXP, readiness_env

KEY = f"NVDA|{EXP}"
THRESHOLD = 70.0


def _drive(scores, threshold=THRESHOLD, t0=1_000_000.0, step=300.0,
           vetoed_at=None):
    """Feed a score sequence through the state machine at 5-minute steps.
    Returns the list of sweep indices that fired."""
    fired = []
    for i, score in enumerate(scores):
        env = readiness_env(score=score, vetoed=(vetoed_at is not None and i in vetoed_at))
        if not env["veto_reasons"]:
            env["score"] = score
        if sw.update_alert_state(KEY, env, threshold, now=t0 + i * step):
            fired.append(i)
    return fired


def test_oscillating_score_fires_exactly_once(monkeypatch):
    monkeypatch.setattr(sw, "HYSTERESIS_MARGIN", 5.0)
    monkeypatch.setattr(sw, "READY_DWELL_SEC", 900.0)
    # Oscillates around the 70 threshold but never below 65 (threshold − margin)
    fired = _drive([72, 68, 73, 69, 71, 66, 74, 68])
    assert fired == [0]
    assert sw.state.alert_states[KEY]["state"] == "ready"


def test_rearm_requires_dwell_below_margin(monkeypatch):
    monkeypatch.setattr(sw, "HYSTERESIS_MARGIN", 5.0)
    monkeypatch.setattr(sw, "READY_DWELL_SEC", 900.0)
    # fire → drop below margin → dwell 4 sweeps (1200s > 900s) → re-arm → fire
    fired = _drive([75, 60, 60, 60, 60, 75])
    assert fired == [0, 5]


def test_bounce_during_dwell_does_not_refire(monkeypatch):
    monkeypatch.setattr(sw, "HYSTERESIS_MARGIN", 5.0)
    monkeypatch.setattr(sw, "READY_DWELL_SEC", 900.0)
    # cooling, then back above threshold BEFORE the dwell elapsed → resumed
    # ready silently; a later dip inside the margin changes nothing.
    fired = _drive([75, 60, 74, 68, 74])
    assert fired == [0]
    assert sw.state.alert_states[KEY]["state"] == "ready"


def test_veto_moves_to_vetoed_and_clearing_can_refire(monkeypatch):
    monkeypatch.setattr(sw, "HYSTERESIS_MARGIN", 5.0)
    monkeypatch.setattr(sw, "READY_DWELL_SEC", 900.0)
    fired = _drive([75, 75, 75, 75], vetoed_at={1, 2})
    # fire at 0; veto at 1-2; veto clears at 3 with score ≥ threshold → fires
    # again (a genuine new transition into ready from an armed state)
    assert fired == [0, 3]
    assert sw.state.alert_states[KEY]["state"] == "ready"


def test_suppressed_envelope_never_fires():
    env = readiness_env(score=90.0)
    env["sector_dispersion_suppressed"] = True
    assert sw.update_alert_state(KEY, env, THRESHOLD, now=1.0) is False
    assert sw.state.alert_states[KEY]["state"] == "cold"


# ── Per-user fan-out ────────────────────────────────────────────────────────
@pytest.fixture()
def supabase_stub(monkeypatch):
    """Two users watch NVDA; their settings differ. Collects alert inserts."""
    inserts: list[dict] = []
    settings_by_uid = {
        "u-low": {"min_score": 60, "structures_enabled": ["bull_put", "bear_call"],
                  "regime_strictness": "balanced"},
        "u-high": {"min_score": 90, "structures_enabled": ["bull_put"],
                   "regime_strictness": "balanced"},
        "u-calls-only": {"min_score": 60, "structures_enabled": ["bear_call"],
                         "regime_strictness": "balanced"},
        "u-strict": {"min_score": 60, "structures_enabled": ["bull_put"],
                     "regime_strictness": "strict"},
    }

    async def fake_select_many(table, select="*", filters=None, **kw):
        assert table == "simmer_watchlist"
        return [{"user_id": uid, "expiration": None} for uid in settings_by_uid]

    async def fake_select_one(table, key, value, select):
        assert table == "simmer_settings"
        return settings_by_uid.get(value)

    async def fake_insert(table, row):
        assert table == "simmer_alerts"
        inserts.append(row)
        return True

    monkeypatch.setattr(supabase_admin, "select_many", fake_select_many)
    monkeypatch.setattr(supabase_admin, "_select_one", fake_select_one)
    monkeypatch.setattr(supabase_admin, "insert_row", fake_insert)
    return inserts


async def test_fanout_applies_each_users_min_score(supabase_stub):
    env = readiness_env(score=75.0, structure="bull_put")
    written = await sw.fanout_alert(env, {"state": "contango", "calm": True})
    got = {r["user_id"] for r in supabase_stub}
    # ONLY min_score filters now — structures_enabled / regime_strictness were
    # removed as per-user filters (engine behavior is admin-global). So u-high
    # (min_score 90) is the only one filtered; the rest (60) all pass, including
    # u-calls-only (structure no longer filters).
    assert got == {"u-low", "u-calls-only", "u-strict"}
    assert written == 3
    payload = supabase_stub[0]["payload"]
    assert payload["symbol"] == "NVDA" and "components" in payload  # full envelope


async def test_fanout_regime_strictness_no_longer_filters(supabase_stub):
    # A stressed regime must NOT block a "strict" user any more — that filter was
    # removed and a leftover value must not silently drop alerts.
    env = readiness_env(score=95.0, structure="bull_put")
    await sw.fanout_alert(env, {"state": "backwardation", "calm": False})
    got = {r["user_id"] for r in supabase_stub}
    assert got == {"u-low", "u-high", "u-calls-only", "u-strict"}  # all pass min_score 95


async def test_fanout_respects_pinned_expiration(monkeypatch):
    inserts: list[dict] = []

    async def fake_select_many(table, select="*", filters=None, **kw):
        return [{"user_id": "u1", "expiration": "2026-10-16"},   # other expiry
                {"user_id": "u2", "expiration": EXP}]

    async def fake_select_one(table, key, value, select):
        return {"min_score": 0}

    async def fake_insert(table, row):
        inserts.append(row)
        return True

    monkeypatch.setattr(supabase_admin, "select_many", fake_select_many)
    monkeypatch.setattr(supabase_admin, "_select_one", fake_select_one)
    monkeypatch.setattr(supabase_admin, "insert_row", fake_insert)
    await sw.fanout_alert(readiness_env(score=80.0), {"calm": True, "state": "contango"})
    assert [r["user_id"] for r in inserts] == ["u2"]


# ── Settings enforcement (server-side, authoritative) ───────────────────────
def _client() -> TestClient:
    app = FastAPI()
    app.include_router(sroute.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def auth_off(monkeypatch):
    monkeypatch.setattr(config, "_cached", Settings(auth_enabled=False))
    yield
    monkeypatch.setattr(config, "_cached", None)


def test_locked_gate_override_rejected_422(auth_off):
    c = _client()
    r = c.post("/simmer/settings",
               json={"gate_overrides": {"catalyst_lockout": False}})
    assert r.status_code == 422
    assert "locked" in r.json()["detail"]


def test_unknown_gate_override_rejected_422(auth_off):
    c = _client()
    r = c.post("/simmer/settings", json={"gate_overrides": {"made_up_gate": False}})
    assert r.status_code == 422


def test_toggleable_gate_override_accepted(auth_off):
    c = _client()
    r = c.post("/simmer/settings", json={"gate_overrides": {"squeeze_veto": False}})
    assert r.status_code == 200
    assert r.json()["settings"]["gate_overrides"] == {"squeeze_veto": False}


def test_short_delta_band_clamped_to_research_bounds(auth_off):
    c = _client()
    r = c.post("/simmer/settings",
               json={"short_delta_min": 0.05, "short_delta_max": 0.90})
    assert r.status_code == 200
    s = r.json()["settings"]
    assert s["short_delta_min"] == 0.15      # 0.05 is the measurably −EV region
    assert s["short_delta_max"] == 0.40


def test_invalid_structures_rejected(auth_off):
    c = _client()
    r = c.post("/simmer/settings", json={"structures_enabled": ["naked_put"]})
    assert r.status_code == 422


def test_sanitize_is_pure_and_reusable():
    out = sroute.sanitize_settings({"min_score": 150.0})
    assert out["min_score"] == 100.0
    with pytest.raises(HTTPException):
        sroute.sanitize_settings({"gate_overrides": {"liquidity": False}})
