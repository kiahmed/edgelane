"""read_api.py — exactly four GETs under /levels/ (+ /healthz); everything
else (paths, methods, other symbols, bad dates) is a plain 404; no docs."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from level_engine.read_api import create_read_app
from level_engine.tick import TickRunner

from ._feeds import CFG, TD, Clock, Driver, StubBrain, et, make_store, path, spx_levels

NF = {"detail": "Not Found"}


@pytest.fixture
def setup(monkeypatch):
    clock = Clock(et(9, 30))
    store = make_store(monkeypatch, clock)
    spx_levels(store)
    runner = TickRunner(CFG, store, brain=StubBrain(), today=lambda: TD, now=clock)
    Driver(runner, clock, et(9, 31)).run(path(6990, 7010, 9) + [7010.0] * 35)
    return runner, store, TestClient(create_read_app(runner))


def test_decision(setup):
    runner, store, c = setup
    r = c.get("/levels/decision/SPX")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "SPX" and body["decision"]["call"] == "wait"
    assert body["status"]["trade_date"] == TD.isoformat() and body["status"]["clusters"] == 2
    assert c.get("/levels/decision/spx").status_code == 200


def test_trace(setup):
    runner, store, c = setup
    body = c.get("/levels/trace/SPX").json()
    assert len(body["traces"]) == len(runner.trace_tail) > 3
    assert len(c.get("/levels/trace/SPX?limit=3").json()["traces"]) == 3
    assert c.get("/levels/trace/SPX?limit=zz").json() == NF


def test_day(setup):
    runner, store, c = setup
    body = c.get(f"/levels/day/{TD.isoformat()}").json()
    assert {"levels", "clusters", "level_states", "beliefs", "picks", "legs",
            "day_summary", "ratio"} <= set(body)
    assert len(body["levels"]) == 2 and len(body["clusters"]) == 2
    assert body["level_states"] and body["beliefs"] and body["legs"]
    # another (past) day is rebuilt from the store: empty but well-formed
    other = c.get("/levels/day/2026-11-19").json()
    assert other["levels"] == [] and other["clusters"] == [] and other["day_summary"] is None


@pytest.mark.parametrize("url", ["/levels/decision/NDX", "/levels/trace/SPY",
                                 "/levels/day/not-a-date", "/levels/day/2026-13-40", "/levels",
                                 "/levels/", "/levels/other/SPX", "/", "/docs", "/redoc",
                                 "/openapi.json", "/admin", "/levels/decision",
                                 "/levels/stats/NDX", "/levels/stats", "/levels/stats/SPX?limit=0",
                                 "/levels/stats/SPX?limit=201", "/levels/stats/SPX?limit=abc",
                                 "/levels/stats/SPX?limit=-1", "/levels/stats/SPX?limit=2.5"])
def test_everything_else_404(setup, url):
    r = setup[2].get(url)
    assert r.status_code == 404 and r.json() == NF


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@pytest.mark.parametrize("url", ["/levels/decision/SPX", f"/levels/day/{TD.isoformat()}",
                                 "/levels/trace/SPX", "/levels/stats/SPX", "/healthz"])
def test_no_write_methods(setup, method, url):
    runner, store, c = setup
    n = len(store.list_levels(TD))
    r = c.request(method, url, json={"symbol": "SPX", "price": 1, "name": "x"})
    assert r.status_code == 404 and r.json() == NF
    assert len(store.list_levels(TD)) == n


def test_only_get_routes_registered(setup):
    app = create_read_app(setup[0])
    routes = {(r.path, tuple(sorted(r.methods))) for r in app.routes if hasattr(r, "methods")}
    assert {p for p, _ in routes} == {"/healthz", "/levels/decision/{symbol}",
                                      "/levels/day/{day}", "/levels/trace/{symbol}",
                                      "/levels/stats/{symbol}"}
    assert all(set(m) <= {"GET", "HEAD"} for _, m in routes)


def test_healthz(setup):
    assert setup[2].get("/healthz").json() == {"ok": True}


# ---- GET /levels/stats/{symbol} (contract C3) ----
from datetime import timedelta  # noqa: E402

from level_engine.read_api import leg_view, stats_counts  # noqa: E402

LEG_KEYS = {"id", "trade_date", "opened_at", "closed_at", "call", "result", "close_reason",
            "target_level", "invalidation_level", "entry_spot", "exit_spot", "progress_pct",
            "mae_pts", "time_to_target_min", "strategy_pnl", "pick_id", "strategy", "label",
            "structure_text", "spread_type"}


def _seed_legs(store):
    """Closed legs over two days (+ one open today) straight into the store."""
    d0 = TD - timedelta(days=1)
    pick = {"pick_id": "p1", "strategy": "bull_call", "label": "Near",
            "structure_text": "7000/7020 C", "spread_type": "debit"}
    rows = [
        # (trade_date, n, call, result, reason, target name, opened (ET h, m), closed)
        (d0, 1, "up", "win", "target", "Y", (10, 0), (10, 30)),
        (d0, 2, "down", "loss", "flipped", "L", (11, 0), (11, 20)),
        (TD, 1, "range", "win", "zone_held", "U", (9, 45), (10, 15)),
        (TD, 2, "up", "loss", "session_close", "Y", (10, 30), (15, 59)),
        (TD, 3, "up", "flipped", None, "Y", (8, 0), (8, 1)),     # legacy pre-binary row
    ]
    for td, n, call, res, why, tname, (oh, om), (ch, cm) in rows:
        off = (td - TD).days
        store.insert("legs", {
            "id": f"{td.isoformat()}:{n}", "trade_date": td, "call": call,
            "opened_at": et(oh, om) + timedelta(days=off), "entry_spot": 7010.0,
            "target_level": {"price": 7040.0, "name": tname},
            "invalidation_level": {"price": 7000.0, "name": "L"},
            "pick": pick if n == 1 else None, "closed_at": et(ch, cm) + timedelta(days=off),
            "result": res, "close_reason": why, "exit_spot": 7030.0, "progress_pct": 50.0,
            "mae_pts": 1.0, "time_to_target_min": None, "strategy_pnl": None})
    store.insert("legs", {"id": f"{TD.isoformat()}:9", "trade_date": TD, "call": "down",
                          "opened_at": et(15, 0), "entry_spot": 7012.0,
                          "target_level": {"price": 7000.0, "name": "L"},
                          "invalidation_level": {"price": 7020.0, "name": "U"},
                          "pick": pick, "result": None})


def test_stats_shape_order_and_counts(setup):
    runner, store, c = setup
    store.rows["legs"].clear()                 # drop the stub brain's leg
    _seed_legs(store)
    body = c.get("/levels/stats/SPX").json()
    assert body["symbol"] == "SPX" and body["as_of"]
    legs = body["legs"]
    assert all(set(g) == LEG_KEYS for g in legs)
    # newest closed first, across days
    assert [g["id"] for g in legs] == [f"{TD}:2", f"{TD}:1", f"{TD}:3",
                                       f"{TD - timedelta(days=1)}:2",
                                       f"{TD - timedelta(days=1)}:1"]
    assert {g["result"] for g in legs} == {"win", "loss"}            # legacy → loss
    legacy = next(g for g in legs if g["id"] == f"{TD}:3")
    assert legacy["result"] == "loss" and legacy["close_reason"] == "flipped"
    first = legs[0]
    assert first["target_level"] == {"price": 7040.0, "name": "Y"}
    assert first["invalidation_level"] == {"price": 7000.0, "name": "L"}
    assert first["pick_id"] is None and first["exit_spot"] == 7030.0
    assert legs[1]["pick_id"] == "p1" and legs[1]["strategy"] == "bull_call"
    assert legs[1]["spread_type"] == "debit" and legs[1]["structure_text"] == "7000/7020 C"
    ol = body["open_leg"]
    assert set(ol) == LEG_KEYS and ol["result"] is None and ol["id"] == f"{TD}:9"
    k = body["counts"]
    assert (k["n"], k["wins"], k["losses"], k["win_rate"]) == (5, 2, 3, 40.0)
    assert set(k["by_call"]) == {"up", "down", "range"}
    assert k["by_call"]["range"] == {"n": 1, "wins": 1, "losses": 0, "win_rate": 100.0}
    assert k["by_call"]["up"] == {"n": 3, "wins": 1, "losses": 2, "win_rate": 33.3}
    assert k["by_level"]["Y"]["n"] == 3 and k["by_level"]["L"]["win_rate"] == 0.0
    # limit
    lim = c.get("/levels/stats/SPX?limit=2").json()
    assert [g["id"] for g in lim["legs"]] == [f"{TD}:2", f"{TD}:1"]
    assert lim["counts"]["n"] == 2
    assert c.get("/levels/stats/SPX?limit=200").status_code == 200
    assert c.get("/levels/stats/spx?limit=1").status_code == 200


def test_stats_empty(setup):
    runner, store, c = setup
    store.rows["legs"].clear()
    body = c.get("/levels/stats/SPX").json()
    assert body["legs"] == [] and body["open_leg"] is None
    k = body["counts"]
    assert k["n"] == 0 and k["win_rate"] is None
    assert all(b["win_rate"] is None for b in k["by_call"].values()) and k["by_level"] == {}


def test_stats_helpers_tolerate_json_strings_and_shadow_legs():
    g = leg_view({"id": "x:1", "call": "up", "result": "unresolved",
                  "target_level": '{"price": 7040, "name": "Y"}', "pick": None})
    assert g["result"] == "loss" and g["close_reason"] == "session_close"
    assert g["target_level"] == {"price": 7040, "name": "Y"} and g["pick_id"] is None
    assert stats_counts([g])["by_level"] == {"Y": {"n": 1, "wins": 0, "losses": 1,
                                                    "win_rate": 0.0}}
