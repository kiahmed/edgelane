"""app/level_view.py and the /snapshot, /symbols, /status consumers (§3.4)."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import level_view
from app.poller import state as poller_state
from app.routes import snapshot as snapshot_route
from app.routes import status as status_route
from app.routes import symbols as symbols_route
from app.session_auth import require_teaser_session

from .conftest import (LEVEL_PICK_ID, decision_wait, decision_with_grid, fake_decision,
                       legacy_pick, level_grid, level_pick_dict, spx_snapshot, use_settings)


# ── helpers ─────────────────────────────────────────────────────────────────

def test_levels_mode():
    assert level_view.levels_mode(SimpleNamespace(matrix_decision_source="levels"))
    assert not level_view.levels_mode(SimpleNamespace(matrix_decision_source="engine"))


def test_levels_mode_falls_back_to_process_settings_for_the_poller_view(monkeypatch):
    """main._settings_view (the poller's SimpleNamespace) doesn't carry the field."""
    bare = SimpleNamespace(symbols=["SPX", "NDX"])
    use_settings(monkeypatch, matrix_decision_source="engine")
    assert not level_view.levels_mode(bare)
    use_settings(monkeypatch, matrix_decision_source="levels")
    assert level_view.levels_mode(bare)


def test_level_symbols():
    eng = SimpleNamespace(matrix_decision_source="engine")
    lvl = SimpleNamespace(matrix_decision_source="levels")
    syms = ["SPX", "NDX"]
    assert level_view.level_symbols(eng, syms) is syms          # unchanged, same object
    assert level_view.level_symbols(lvl, syms) == ["SPX"]
    assert level_view.level_symbols(lvl, ["NDX"]) == ["SPX"]    # SPX always (the feed)
    assert level_view.level_symbols(lvl, []) == ["SPX"]


async def test_level_snapshot_with_a_pick_and_grid(monkeypatch):
    calls = fake_decision(monkeypatch, decision_with_grid())
    snap = spx_snapshot()
    before = copy.deepcopy(snap)
    out = await level_view.level_snapshot(SimpleNamespace(), snap)
    assert snap == before                                        # never mutated
    assert out["engine_pick"] == level_pick_dict()
    assert out["engine_pick_legacy"] == legacy_pick()
    assert out["strategies"] == level_grid()
    assert out["strategies_legacy"] == before["strategies"]
    assert out["level_decision"]["pick_id"] == LEVEL_PICK_ID
    assert out["spot"] == snap["spot"] and out["expiration"] == snap["expiration"]
    assert calls == [("SPX", level_view.level_pick.CACHE_TTL_SEC)]
    # the copies are independent of the input and of fetch_decision's dict
    out["engine_pick_legacy"]["net_premium"] = -1
    out["strategies_legacy"]["bull_call"]["best"]["label"] = "X"
    assert snap == before


async def test_level_snapshot_does_not_alias_the_decision_cache(monkeypatch):
    dec = decision_with_grid()

    async def _same(settings, symbol, **kw):
        return dec                                              # cached object

    monkeypatch.setattr(level_view.level_pick, "fetch_decision", _same)
    out = await level_view.level_snapshot(SimpleNamespace(), spx_snapshot(), max_age=0)
    out["engine_pick"]["legs"].clear()
    out["strategies"]["bull_call"]["best"]["pick_id"] = "x"
    assert dec == decision_with_grid()


@pytest.mark.parametrize("decision", [decision_wait(), decision_wait("done"), None,
                                      {**decision_with_grid(), "pick": None}])
async def test_no_pick_or_unreachable_shows_nothing_legacy(monkeypatch, decision):
    fake_decision(monkeypatch, decision)
    out = await level_view.level_snapshot(SimpleNamespace(), spx_snapshot())
    assert out["engine_pick"] is None
    assert out["strategies"] == {}
    assert out["engine_pick_legacy"] == legacy_pick()
    if decision is None:
        assert out["level_decision"] == {"status": "unavailable"}
    else:
        assert out["level_decision"]["pick_id"] is None


async def test_max_age_is_passed_through(monkeypatch):
    calls = fake_decision(monkeypatch, decision_with_grid())
    await level_view.level_snapshot(SimpleNamespace(), spx_snapshot(), max_age=0)
    assert calls == [("SPX", 0)]


# ── /snapshot ───────────────────────────────────────────────────────────────

def _snap_client(full: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(snapshot_route.router)
    app.dependency_overrides[require_teaser_session] = (
        lambda: {"full": full, "kind": "user" if full else "anon", "id": "x"})
    return TestClient(app, raise_server_exceptions=False)


def test_snapshot_levels_mode_serves_the_level_grid(monkeypatch):
    use_settings(monkeypatch, matrix_decision_source="levels")
    fake_decision(monkeypatch, decision_with_grid())
    body = _snap_client().get("/snapshot/SPX").json()
    assert body["engine_pick"]["pick_id"] == LEVEL_PICK_ID
    assert body["strategies"] == level_grid()
    assert body["strategies_legacy"] == spx_snapshot()["strategies"]
    lst = _snap_client().get("/snapshot").json()
    assert lst["snapshots"]["SPX"]["strategies"] == level_grid()
    assert poller_state.latest_by_symbol["SPX"] == spx_snapshot()   # cache untouched


def test_snapshot_levels_mode_unreachable_has_no_legacy_decision(monkeypatch):
    use_settings(monkeypatch, matrix_decision_source="levels")
    fake_decision(monkeypatch, None)
    for body in (_snap_client().get("/snapshot/SPX").json(),
                 _snap_client().get("/snapshot").json()["snapshots"]["SPX"]):
        assert body["engine_pick"] is None and body["strategies"] == {}
        assert body["level_decision"] == {"status": "unavailable"}


def test_snapshot_teaser_unchanged_in_levels_mode(monkeypatch):
    use_settings(monkeypatch, matrix_decision_source="levels")
    calls = fake_decision(monkeypatch, decision_with_grid())
    t = _snap_client(full=False).get("/snapshot/SPX").json()
    assert t["teaser"] is True
    for k in ("engine_pick", "strategies", "engine_pick_legacy", "strategies_legacy",
              "level_decision"):
        assert k not in t
    assert calls == []


def test_snapshot_engine_mode_identical(monkeypatch):
    use_settings(monkeypatch, matrix_decision_source="engine")
    calls = fake_decision(monkeypatch, decision_with_grid())
    body = _snap_client().get("/snapshot/SPX").json()
    assert body == {"symbol": "SPX", "polled_at": None,
                    "market_open": poller_state.market_open,
                    "market_reason": poller_state.market_reason,
                    "evaluation_active": poller_state.evaluation_active, **spx_snapshot()}
    assert _snap_client().get("/snapshot").json()["snapshots"]["SPX"] == spx_snapshot()
    assert calls == []


# ── /symbols + /status (the UI dropdown reads /status.symbols) ──────────────

def _app_client(settings) -> TestClient:
    app = FastAPI()
    app.include_router(symbols_route.router)
    app.include_router(status_route.router)
    app.state.settings = settings
    app.state.using_mock = True
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("mode,expected", [("engine", ["SPX", "NDX"]), ("levels", ["SPX"])])
def test_symbols_and_status_advertise_only_spx_in_levels_mode(monkeypatch, mode, expected):
    s = use_settings(monkeypatch, matrix_decision_source=mode, symbols=["SPX", "NDX"])
    c = _app_client(s)
    assert c.get("/symbols").json() == {"symbols": expected}
    r = c.get("/status")
    assert r.status_code == 200, r.text
    assert r.json()["symbols"] == expected
