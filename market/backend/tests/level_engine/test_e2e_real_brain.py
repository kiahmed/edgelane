"""End-to-end: runtime + REAL brain + REAL strategy over a synthetic day, then
replay (§14) must reproduce every persisted output exactly, and the read API
must serve the decision."""
from __future__ import annotations

from fastapi.testclient import TestClient

from level_engine import replay
from level_engine.brain import create_brain
from level_engine.read_api import create_read_app

from ._feeds import CFG, TD, Clock, et, make_store
from .test_replay import _live


def _strip(rows, drop=("id",)):
    return [{k: v for k, v in r.items() if k not in drop} for r in rows]


def test_real_brain_live_then_replay_identical(monkeypatch):
    store, live = _live(monkeypatch, brain=create_brain(CFG))
    for t in ("level_state", "evidence", "beliefs", "picks", "legs"):
        assert store.list_rows(t, TD) is not None
    assert store.list_rows("beliefs", TD), "brain produced no beliefs"

    sink = make_store(monkeypatch, Clock(et(17, 0)))
    res = replay.run_replay(store, CFG, TD, sink=sink, brain=create_brain(CFG))
    for t in ("level_state", "evidence", "beliefs", "picks", "legs"):
        assert _strip(sink.list_rows(t, TD)) == _strip(store.list_rows(t, TD)), t
    assert sink.get_day_summary(TD) == store.get_day_summary(TD)
    assert res.brain is True


def test_real_brain_decision_served_by_read_api(monkeypatch):
    store, live = _live(monkeypatch, brain=create_brain(CFG))
    c = TestClient(create_read_app(live))
    r = c.get("/levels/decision/SPX")
    assert r.status_code == 200
    dec = r.json()["decision"]
    assert dec["call"] in ("up", "down", "range", "wait", "done")
    assert dec["symbol"] == "SPX"
    assert c.get("/levels/trace/SPX").status_code == 200
    assert c.get(f"/levels/day/{TD.isoformat()}").status_code == 200
    assert c.post("/levels/decision/SPX").status_code in (404, 405)
