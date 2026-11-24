"""replay.py — a live run recorded in a MemoryStore, replayed over the same
ticks + audit, reproduces the level-state transitions (and brain outputs)
exactly; mid-session edits replay at the tick they landed; config-B diffs."""
from __future__ import annotations

from datetime import timedelta

from level_engine import replay
from level_engine.store import MemoryStore
from level_engine.tick import TickRunner

from ._feeds import CFG, TD, Clock, Driver, StubBrain, et, make_store, path, spx_levels

SPOTS = (path(6990, 7010, 9) + [7010.0] * 35 + path(7010, 6994, 8) + [6994.0] * 5
         + path(6994, 7012, 9) + path(7012, 7048, 13) + [7056.0] * 32)


def _live(monkeypatch, brain=None, edit=True):
    clock = Clock(et(9, 25))
    store = make_store(monkeypatch, clock)
    store.add_level(TD, {"symbol": "SPY", "price": 702.6, "name": "spy pm high"}, TD)
    a, b = spx_levels(store)
    live = TickRunner(CFG, store, brain=brain, today=lambda: TD, now=clock)
    d = Driver(live, clock, et(9, 31))
    d.run(SPOTS[:60], spy=700.5)
    if edit:
        clock.t = d.ts - timedelta(seconds=4)
        store.update_level(b["id"], {"price": 7052.0, "name": "L7052"}, TD)
        store.add_level(TD, {"symbol": "SPX", "price": 6950.0, "name": "low"}, TD)
    d.run(SPOTS[60:], spy=700.5)
    d.ts = et(16, 0, 10)
    d.tick(7050.0, spy=700.5)                            # recorded close tick
    return store, live


def test_replay_reproduces_live_transitions_exactly(monkeypatch):
    store, live = _live(monkeypatch)
    live_rows = store.list_rows("level_state", TD)
    assert any(r["state"] == "held_up" for r in live_rows)
    res = replay.run_replay(store, CFG, TD)
    assert res.ticks == len(store.list_ticks(TD))
    assert res.transitions == live_rows
    assert sum(n for _, n in res.per_tick) == len(live_rows)
    assert {k: v["state"] for k, v in res.final_states.items()} == \
        {c.id: live.level_map.tracker(c.id).state for c in live.level_map.clusters}


def test_replay_with_brain_matches_live_outputs(monkeypatch):
    store, live = _live(monkeypatch, brain=StubBrain())
    res = replay.run_replay(store, CFG, TD, brain=StubBrain())
    assert res.brain is True
    assert res.legs == store.list_rows("legs", TD)
    assert res.picks == store.list_rows("picks", TD)
    assert res.day_summary == store.get_day_summary(TD)
    sink_beliefs = len(res.calls)
    assert sink_beliefs >= 1


def test_replay_writes_nothing_live(monkeypatch):
    store, _ = _live(monkeypatch)
    before = (len(store.ticks), {k: len(v) for k, v in store.rows.items()},
              len(store.audit), len(store.levels))
    sink = MemoryStore()
    replay.run_replay(store, CFG, TD, sink=sink)
    after = (len(store.ticks), {k: len(v) for k, v in store.rows.items()},
             len(store.audit), len(store.levels))
    assert before == after and sink.ticks == [] and sink.rows["level_state"]


def test_final_level_set_differs_from_audit_replay(monkeypatch):
    store, _ = _live(monkeypatch)
    exact = replay.run_replay(store, CFG, TD)
    final_only = replay.run_replay(store, CFG, TD, audit_levels=False)
    assert final_only.transitions != exact.transitions   # audit reconstruction matters


def test_config_b_diff(monkeypatch):
    store, _ = _live(monkeypatch)
    a = replay.run_replay(store, CFG, TD)
    b = replay.run_replay(store, CFG.with_overrides(hold_min=2.0), TD)
    d = replay.diff_results(a, b)
    assert d["transitions"]["only_a"] or d["transitions"]["only_b"]
    same = replay.diff_results(a, replay.run_replay(store, CFG, TD))
    assert all(not v["only_a"] and not v["only_b"] for v in same.values())


def test_print_and_safe_config(monkeypatch, capsys):
    store, _ = _live(monkeypatch)
    res = replay.run_replay(store, CFG, TD)
    replay.print_result(res, "[A]")
    replay.print_diff(replay.diff_results(res, res))
    out = capsys.readouterr().out
    assert "final level states" in out and "ticks=" in out
    sc = replay.safe_config(CFG.with_overrides(lvl_db_url="postgres://u:pw@h/db",
                                               internal_feed_token="tok"))
    assert "lvl_db_url" not in sc and "internal_feed_token" not in sc
    assert "lvl_replay_db_url" not in sc and sc["hold_min"] == CFG.hold_min


def test_cli_refuses_without_replay_dsn(tmp_path, capsys):
    cfgp = tmp_path / "le.config"
    cfgp.write_text("LVL_REPLAY_DB_URL=\n")
    assert replay.main(["--date", "2026-11-20", "--config", str(cfgp)]) == 2
    assert "LVL_REPLAY_DB_URL" in capsys.readouterr().err


def test_load_brain_absent_is_none():
    import importlib.util
    if importlib.util.find_spec("level_engine.brain") is None:
        assert replay.load_brain(CFG) is None
