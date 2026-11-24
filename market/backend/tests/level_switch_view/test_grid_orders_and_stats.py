"""Grid cards are orderable by pick_id (C1) and GET /levels/stats is the 4th
allow-listed proxy GET (C3)."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import level_pick
from app.routes import levels_proxy
from app.routes import orders as O
from app.session_auth import require_teaser_session

from ..level_matrix.test_level_orders import FakeTradier
from .conftest import (CAND_PICK_ID, EXTREME_PICK_ID, GRID_PICK_ID, LEVEL_PICK_ID,
                       decision_with_grid, decision_with_pick, fake_decision, use_settings)


# ── find_level_pick over the grid ───────────────────────────────────────────

def test_find_level_pick_searches_the_grid():
    dec = decision_with_grid()
    assert level_pick.find_level_pick(dec, LEVEL_PICK_ID) is dec["pick"]
    assert level_pick.find_level_pick(dec, GRID_PICK_ID)["strategy"] == "bull_call"
    assert level_pick.find_level_pick(dec, EXTREME_PICK_ID)["label"] == "Extreme"
    assert level_pick.find_level_pick(dec, CAND_PICK_ID)["strategy"] == "bull_call"
    assert level_pick.find_level_pick(dec, "nope") is None
    # grid only (no tile pick) still resolves; junk grid shapes are ignored
    assert level_pick.find_level_pick({"pick": None, "strategies": dec["strategies"]},
                                      GRID_PICK_ID)["pick_id"] == GRID_PICK_ID
    assert level_pick.find_level_pick({"strategies": {"x": None, "y": {"all_widths": [1]}}},
                                      GRID_PICK_ID) is None


def test_full_grid_candidate_wins_over_a_compact_one():
    dec = decision_with_grid()
    dec["pick"]["candidates"] = [{"pick_id": GRID_PICK_ID, "strategy": "bull_call"}]
    assert level_pick.find_level_pick(dec, GRID_PICK_ID)["legs"]


def test_level_strategy_names():
    dec = decision_with_grid()
    assert level_pick.level_strategy_names(dec, GRID_PICK_ID) == {"bull_call"}
    assert level_pick.level_strategy_names(dec, LEVEL_PICK_ID) == {"call_butterfly"}
    dec["strategies"]["fly_alias"] = dec["strategies"].pop("call_butterfly")
    assert level_pick.level_strategy_names(dec, EXTREME_PICK_ID) == {"fly_alias",
                                                                     "call_butterfly"}
    assert level_pick.level_strategy_names(dec, "nope") == set()
    assert level_pick.level_strategy_names(None, GRID_PICK_ID) == set()


# ── grid "Place" orders ─────────────────────────────────────────────────────

def _req(**over) -> O.OrderRequest:
    base = dict(symbol="SPX", strategy="bull_call", candidate_label="Near", quantity=1,
                duration="day", pick_id=GRID_PICK_ID, source="levels")
    base.update(over)
    return O.OrderRequest(**base)


@pytest.fixture(autouse=True)
def _levels(monkeypatch):
    use_settings(monkeypatch, matrix_decision_source="levels",
                 level_engine_read_url="http://le-test.internal:9911")


async def test_grid_card_pick_id_is_orderable(monkeypatch):
    calls = fake_decision(monkeypatch, decision_with_grid())
    out = await O._submit_order(_req(), FakeTradier(), preview=True, per_user=True)
    assert calls == [("SPX", 0)]
    p = out["payload"]
    assert [p["option_symbol[0]"], p["option_symbol[1]"]] == [
        "SPXW261123C07265000", "SPXW261123C07280000"]
    assert out["order_type"] == "debit" and out["limit_price"] == 5.0


async def test_extreme_sibling_in_all_widths_is_orderable(monkeypatch):
    fake_decision(monkeypatch, decision_with_grid())
    out = await O._submit_order(_req(strategy="call_butterfly", pick_id=EXTREME_PICK_ID,
                                     candidate_label="Extreme"),
                                FakeTradier(), preview=True, per_user=True)
    assert out["payload"]["option_symbol[2]"] == "SPXW261123C07320000"


async def test_grid_card_sent_under_its_grid_key_is_accepted(monkeypatch):
    dec = decision_with_grid()
    dec["strategies"]["bcs_key"] = dec["strategies"].pop("bull_call")
    fake_decision(monkeypatch, dec)
    out = await O._submit_order(_req(strategy="bcs_key"), FakeTradier(), preview=True,
                                per_user=True)
    assert out["payload"]["option_symbol[0]"] == "SPXW261123C07265000"


@pytest.mark.parametrize("over", [dict(strategy="put_butterfly"),
                                  dict(strategy="call_butterfly")])
async def test_grid_card_strategy_mismatch_is_409(monkeypatch, over):
    fake_decision(monkeypatch, decision_with_grid())
    t = FakeTradier()
    with pytest.raises(HTTPException) as ei:
        await O._submit_order(_req(**over), t, preview=False, per_user=True)
    assert ei.value.status_code == 409 and t.placed == []


async def test_grid_card_gone_after_a_flip_is_409_never_legacy(monkeypatch):
    fake_decision(monkeypatch, decision_with_pick())       # no grid any more
    t = FakeTradier()
    with pytest.raises(HTTPException) as ei:
        await O._submit_order(_req(candidate_label="Balanced"), t, preview=False,
                              per_user=True)
    assert ei.value.status_code == 409 and t.placed == []


# ── /levels/stats proxy ─────────────────────────────────────────────────────

BASE = "http://le-test.internal:9911"


@pytest.fixture
def upstream(monkeypatch):
    calls: list[tuple] = []

    async def _get(url, params, timeout):
        calls.append((url, params, timeout))
        return 200, {"symbol": "SPX", "legs": [], "open_leg": None, "counts": {"n": 0}}

    monkeypatch.setattr(level_pick, "_http_get", _get)
    return calls


def _client(full: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(levels_proxy.router)
    app.dependency_overrides[require_teaser_session] = (
        lambda: {"full": full, "kind": "user" if full else "anon", "id": "x"})
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("q,params", [("", None), ("?limit=1", {"limit": 1}),
                                      ("?limit=50", {"limit": 50}),
                                      ("?limit=200", {"limit": 200})])
def test_stats_is_forwarded(upstream, q, params):
    r = _client().get("/levels/stats/SPX" + q,
                      headers={"Cookie": "sb=secret", "Authorization": "Bearer t"})
    assert r.status_code == 200 and r.json()["symbol"] == "SPX"
    assert upstream == [(BASE + "/levels/stats/SPX", params, level_pick.TIMEOUT_SEC)]


@pytest.mark.parametrize("path", ["/levels/stats/SPX?limit=0", "/levels/stats/SPX?limit=201",
                                  "/levels/stats/SPX?limit=1000", "/levels/stats/SPX?limit=-1",
                                  "/levels/stats/SPX?limit=abc", "/levels/stats/SPX?limit=",
                                  "/levels/stats/SPX?limit=5.0", "/levels/stats/spx",
                                  "/levels/stats/SPX/extra", "/levels/stats",
                                  "/levels/stats/%2E%2E"])
def test_bad_stats_requests_are_404_and_never_forwarded(upstream, path):
    assert _client().get(path).status_code == 404
    assert upstream == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_stats_other_methods_are_404(upstream, method):
    assert _client().request(method, "/levels/stats/SPX").status_code == 404
    assert upstream == []


def test_stats_teaser_is_403(upstream):
    assert _client(full=False).get("/levels/stats/SPX").status_code == 403
    assert upstream == []


def test_stats_upstream_down_is_503(monkeypatch):
    async def _boom(url, params, timeout):
        raise OSError("refused")

    monkeypatch.setattr(level_pick, "_http_get", _boom)
    r = _client().get("/levels/stats/SPX")
    assert r.status_code == 503 and r.json() == {"error": "level engine unavailable"}
