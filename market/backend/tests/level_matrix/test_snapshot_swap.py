"""MATRIX_DECISION_SOURCE=levels: /snapshot/SPX is the levels view (§3.4,
revised 2026-11-23 — nothing legacy is shown, even when the level engine is
unreachable). The grid side is covered in tests/level_switch_view."""
from __future__ import annotations

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.poller import state as poller_state
from app.routes import snapshot as snapshot_route
from app.session_auth import require_teaser_session

from .conftest import (LEVEL_PICK_ID, decision_wait, decision_with_pick, fake_decision,
                       legacy_pick, level_pick_dict, use_settings)


def _client(full: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(snapshot_route.router)
    app.dependency_overrides[require_teaser_session] = (
        lambda: {"full": full, "kind": "user" if full else "anon", "id": "x"})
    return TestClient(app, raise_server_exceptions=False)


def _expected_legacy_payload(sym: str) -> dict:
    return {"symbol": sym, "polled_at": poller_state.last_poll_at.get(sym),
            "market_open": poller_state.market_open,
            "market_reason": poller_state.market_reason,
            "evaluation_active": poller_state.evaluation_active,
            **copy.deepcopy(poller_state.latest_by_symbol[sym])}


def test_engine_mode_is_identical_and_never_calls_the_level_engine(monkeypatch):
    use_settings(monkeypatch, matrix_decision_source="engine")
    calls = fake_decision(monkeypatch, decision_with_pick())
    r = _client().get("/snapshot/SPX")
    assert r.status_code == 200
    assert r.json() == _expected_legacy_payload("SPX")
    lst = _client().get("/snapshot").json()
    assert lst["snapshots"]["SPX"] == poller_state.latest_by_symbol["SPX"]
    assert calls == []


def test_levels_mode_swaps_the_pick_into_engine_pick(monkeypatch):
    use_settings(monkeypatch, matrix_decision_source="levels")
    fake_decision(monkeypatch, decision_with_pick())
    body = _client().get("/snapshot/SPX").json()
    assert body["engine_pick"] == level_pick_dict()
    assert body["engine_pick"]["source"] == "levels"
    assert body["engine_pick_legacy"] == legacy_pick()
    ld = body["level_decision"]
    assert ld["call"] == "up" and ld["pick_id"] == LEVEL_PICK_ID and ld["status"] == "ok"
    assert ld["target_level"] == {"price": 7280.0, "name": "R1"}
    assert ld["invalidation_level"] == {"price": 7240.0, "name": "S1"}
    # the grid follows the level engine (this decision carries none → empty);
    # the legacy grid is kept only under strategies_legacy
    exp = _expected_legacy_payload("SPX")
    assert body["strategies"] == {}
    assert body["strategies_legacy"] == exp["strategies"]
    # the data service (spot / expiration / …) is the legacy snapshot, untouched
    for k in ("spot", "expiration", "dte", "symbol"):
        assert body[k] == exp[k]


@pytest.mark.parametrize("call", ["wait", "done"])
def test_levels_mode_without_a_pick_shows_the_level_state(monkeypatch, call):
    use_settings(monkeypatch, matrix_decision_source="levels")
    fake_decision(monkeypatch, decision_wait(call))
    body = _client().get("/snapshot/SPX").json()
    assert body["engine_pick"] is None
    assert body["strategies"] == {}
    assert body["engine_pick_legacy"] == legacy_pick()
    assert body["level_decision"]["call"] == call
    assert body["level_decision"]["pick_id"] is None


def test_level_engine_unreachable_shows_no_legacy_pick(monkeypatch):
    """Revised §3.4: no degrade to the legacy pick — levels mode shows nothing
    legacy as a decision (the legacy pick survives only as engine_pick_legacy)."""
    use_settings(monkeypatch, matrix_decision_source="levels")
    fake_decision(monkeypatch, None)
    body = _client().get("/snapshot/SPX").json()
    assert body["engine_pick"] is None
    assert body["strategies"] == {}
    assert body["engine_pick_legacy"] == legacy_pick()
    assert body["level_decision"] == {"status": "unavailable"}


def test_ndx_and_teasers_are_never_swapped(monkeypatch):
    use_settings(monkeypatch, matrix_decision_source="levels")
    calls = fake_decision(monkeypatch, decision_with_pick())
    assert _client().get("/snapshot/NDX").json() == _expected_legacy_payload("NDX")
    teaser = _client(full=False).get("/snapshot/SPX").json()
    assert teaser["teaser"] is True
    for k in ("engine_pick", "engine_pick_legacy", "level_decision", "strategies"):
        assert k not in teaser
    lst = _client(full=False).get("/snapshot").json()
    assert "level_decision" not in lst["snapshots"]["SPX"]
    assert calls == []


def test_list_endpoint_swaps_spx_only(monkeypatch):
    use_settings(monkeypatch, matrix_decision_source="levels")
    fake_decision(monkeypatch, decision_with_pick())
    lst = _client().get("/snapshot").json()
    assert lst["snapshots"]["SPX"]["engine_pick"]["pick_id"] == LEVEL_PICK_ID
    assert lst["snapshots"]["SPX"]["engine_pick_legacy"] == legacy_pick()
    assert lst["snapshots"]["NDX"] == poller_state.latest_by_symbol["NDX"]
    assert lst["symbols"] == ["SPX", "NDX"]


def test_the_poller_cache_is_never_mutated(monkeypatch):
    """Persisted bias_decisions, the evaluator, matrix_signals, the ledger and
    snap cards all read poller_state — they must stay on the legacy pick."""
    use_settings(monkeypatch, matrix_decision_source="levels")
    fake_decision(monkeypatch, decision_with_pick())
    cache = poller_state.latest_by_symbol
    spx_obj = cache["SPX"]
    pick_obj = spx_obj["engine_pick"]
    before = copy.deepcopy(dict(cache))
    c = _client()
    c.get("/snapshot/SPX")
    c.get("/snapshot")
    fake_decision(monkeypatch, decision_wait())
    c.get("/snapshot/SPX")
    fake_decision(monkeypatch, None)
    c.get("/snapshot/SPX")
    assert dict(cache) == before
    assert cache["SPX"] is spx_obj and cache["SPX"]["engine_pick"] is pick_obj
    assert set(cache["SPX"]) == set(before["SPX"])       # no level keys leaked in
