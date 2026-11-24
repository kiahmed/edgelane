"""tick.py — one deterministic tick: session gating, ratio lock + converted
rows, level rebuild on edits, filtered chain, level-state persistence, the
brain seam (session close, pick-on-change, legs), warm start."""
from __future__ import annotations

from datetime import timedelta

from level_engine.context import BrainOutput
from level_engine.tick import CHAIN_COLS, TickRunner, feed_from_tick_row

from ._feeds import (CFG, TD, Clock, Driver, StubBrain, et, feed, make_store, path,
                     spx_levels)

UP_AND_HOLD = path(6990, 7010, 9) + [7010.0] * 35      # cross 7000, hold > 10 min


def _runner(monkeypatch, brain=None, t0=None):
    clock = Clock(t0 or et(9, 30))
    store = make_store(monkeypatch, clock)
    r = TickRunner(CFG, store, brain=brain, today=lambda: TD, now=clock)
    return r, store, clock


def test_tick_recorded_with_filtered_chain(monkeypatch):
    r, store, clock = _runner(monkeypatch)
    spx_levels(store)                                   # 7000, 7050; EM 20 → [6960, 7090]
    out = Driver(r, clock, et(9, 31)).tick(7001.0)
    assert out is not None and out["trade_date"] == TD
    ticks = store.list_ticks(TD)
    assert len(ticks) == 1
    t = ticks[0]
    assert t["spx"] == 7001.0 and t["ts"] == et(9, 31) and t["bias"]["direction"] == "up"
    strikes = sorted({c["strike"] for c in t["chain"]})
    assert strikes[0] == 6960.0 and strikes[-1] == 7090.0
    assert all(set(c) == set(CHAIN_COLS) for c in t["chain"])
    assert t["zone"]["kind"] in ("in_band", "between") and t["zone"]["meta"]["expected_move"] == 20.0
    # init transitions for both clusters → lvl_level_state with the spec columns
    rows = store.list_rows("level_state", TD)
    assert len(rows) == 2
    assert set(rows[0]) == {"trade_date", "level_id", "ts", "state", "since", "crosses",
                            "last_held_side", "detail"}
    assert rows[0]["detail"]["prev_state"] is None and rows[0]["detail"]["event"] == "init"


def test_chain_window_without_levels_and_em_fallback(monkeypatch):
    r, store, clock = _runner(monkeypatch)
    d = Driver(r, clock, et(9, 31))
    d.tick(7000.0, em=None)                             # no levels, EM → 1% = 70 → ±140
    strikes = sorted({c["strike"] for c in store.list_ticks(TD)[0]["chain"]})
    assert strikes[0] == 6860.0 and strikes[-1] == 7140.0


def test_duplicate_and_older_ts_ignored(monkeypatch):
    r, store, clock = _runner(monkeypatch)
    spx_levels(store)
    f = feed(et(9, 31), 7001.0)
    assert r.process(f) is not None
    assert r.process(f) is None
    assert r.process(feed(et(9, 30, 40), 7002.0)) is None
    assert len(store.list_ticks(TD)) == 1


def test_outside_session_no_tick(monkeypatch):
    r, store, clock = _runner(monkeypatch)
    spx_levels(store)
    assert r.process(feed(et(9, 29, 40), 7001.0)) is None            # pre-market
    assert r.process(feed(et(9, 31), 7001.0, market_open=False)) is None   # holiday / closed
    assert store.list_ticks(TD) == [] and r.status["session"] == "closed"
    # a day that never opened: a feed after the close does nothing either
    assert r.process(feed(et(16, 0, 20), 7001.0)) is None
    assert store.list_ticks(TD) == []


def test_ratio_locks_at_lock_time_and_converted_persisted_once(monkeypatch):
    r, store, clock = _runner(monkeypatch)
    spy = store.add_level(TD, {"symbol": "SPY", "price": 702.0, "name": "PM high"}, TD)
    spx_levels(store, prices=(6900.0,))
    d = Driver(r, clock, et(9, 33))
    # live SPX/SPY = 7000/700.5 (≠ prevclose 6990/699)
    d.run([7000.0] * 12, spy=700.5)                      # 09:33:00 … 09:36:40
    ticks = store.list_ticks(TD)
    pre = [t for t in ticks if t["ts"] < et(9, 35)]
    post = [t for t in ticks if t["ts"] >= et(9, 35)]
    assert pre and all(not t["ratio_locked"] for t in pre)
    assert all(abs(t["ratio"] - 6990 / 699) < 1e-12 for t in pre)
    assert all(t["ratio_locked"] and abs(t["ratio"] - 7000 / 700.5) < 1e-12 for t in post)
    conv = [lv for lv in store.list_levels(TD) if lv["source"] == "converted"]
    assert len(conv) == 1 and conv[0]["converted_from"] == spy["id"]
    assert abs(conv[0]["ratio"] - 7000 / 700.5) < 1e-12
    assert conv[0]["price"] == round(702.0 * 7000 / 700.5, 2)
    converts = [a for a in store.list_audit(TD) if a["action"] == "convert"]
    assert len(converts) == 2                          # provisional once, locked once
    # level map follows the locked conversion
    assert any(abs(c.price - conv[0]["price"]) < 1e-9 for c in r.clusters)


def test_restart_after_lock_restores_stored_ratio(monkeypatch):
    r, store, clock = _runner(monkeypatch)
    spx_levels(store)
    Driver(r, clock, et(9, 34, 40)).run([7000.0] * 3, spy=700.5)
    r2 = TickRunner(CFG, store, today=lambda: TD, now=clock)
    # spy missing on the restart's first tick: the stored lock still applies
    out = r2.process({**feed(et(9, 40), 7003.0), "spy_spot": None})
    assert out["ratio_locked"] and abs(out["ratio"] - 7000 / 700.5) < 1e-12


def test_level_edit_mid_session_rebuilds(monkeypatch):
    r, store, clock = _runner(monkeypatch)
    a, b = spx_levels(store)
    d = Driver(r, clock, et(9, 31))
    outs = d.run([7020.0] * 3)
    assert outs[0]["levels_changed"] is True and not any(o["levels_changed"] for o in outs[1:])
    clock.t = d.ts - timedelta(seconds=5)
    store.update_level(b["id"], {"price": 7060.0}, TD)
    o = d.tick(7020.0)
    assert o["levels_changed"] is True
    assert [c.price for c in r.clusters] == [7000.0, 7060.0]
    assert d.tick(7020.0)["levels_changed"] is False
    # the edited level's tracker restarted (fresh 'init' row at the edit tick)
    inits = [x for x in store.list_rows("level_state", TD) if x["detail"]["event"] == "init"]
    assert len(inits) == 3
    # a new level → rebuild again
    store.add_level(TD, {"symbol": "SPX", "price": 7100.0, "name": "high"}, TD)
    assert d.tick(7020.0)["levels_changed"] is True and len(r.clusters) == 3


def test_transitions_persisted(monkeypatch):
    r, store, clock = _runner(monkeypatch)
    spx_levels(store)
    Driver(r, clock, et(9, 31)).run(UP_AND_HOLD)
    states = [x["state"] for x in store.list_rows("level_state", TD)
              if x["detail"]["name"] == "L7000"]
    assert states[0] == "away_below" and "crossed_up" in states and states[-1] == "held_up"


def test_feed_status_reflected_no_tick(monkeypatch):
    r, store, clock = _runner(monkeypatch)
    r.set_feed_status({"state": "stale", "error": None})
    assert r.status["feed"] == "stale" and r.status["ticks"] == 0


def test_session_close_tick_with_stub_brain(monkeypatch):
    brain = StubBrain()
    r, store, clock = _runner(monkeypatch, brain=brain)
    spx_levels(store)
    d = Driver(r, clock, et(15, 40))
    d.run(UP_AND_HOLD[:20])                             # 15:40 … 15:46:20
    d.ts = et(16, 0, 5)
    close = d.tick(7004.0)
    assert close["session_close"] is True
    assert brain.ctxs[-1].session_close is True
    assert all(not c.session_close for c in brain.ctxs[:-1])
    assert brain.resets == [TD]
    assert d.tick(7004.0) is None                       # no more ticks today
    assert r.status["session"] == "closed"
    ticks = store.list_ticks(TD)
    assert ticks[-1]["zone"]["meta"]["session_close"] is True
    assert store.get_day_summary(TD)["close"] == 7004.0
    # persisted brain outputs: one belief + evidence per tick, picks on change only
    n = len(brain.ctxs)
    assert len(store.list_rows("beliefs", TD)) == n == len(store.list_rows("evidence", TD))
    pids = [p["pick_id"] for p in store.list_rows("picks", TD)]
    assert len(pids) == len(set(pids)) and 1 <= len(pids) < n
    assert r.latest_decision["session_close"] is True
    assert len(r.trace_tail) == n and r.trace_tail[-1]["trace"] == "spot 7004.0"
    # the brain saw the RECORDED feed view (filtered chain, no engine_pick)
    assert brain.ctxs[0].feed["contracts"] == ticks[0]["chain"]
    assert "engine_pick" not in brain.ctxs[0].feed


def test_market_open_false_after_open_closes(monkeypatch):
    brain = StubBrain()
    r, store, clock = _runner(monkeypatch, brain=brain)
    spx_levels(store)
    d = Driver(r, clock, et(12, 58))
    d.run(UP_AND_HOLD)                                  # opens a leg on held_up
    assert store.list_rows("legs", TD)
    out = d.tick(7011.0, market_open=False)             # early close
    assert out["session_close"] is True and brain.ctxs[-1].session_close
    assert store.list_rows("legs", TD)[0]["result"] == "unresolved"
    assert d.tick(7012.0) is None


def test_maybe_close_fallback(monkeypatch):
    brain = StubBrain()
    r, store, clock = _runner(monkeypatch, brain=brain)
    spx_levels(store)
    d = Driver(r, clock, et(15, 58))
    d.run([7020.0] * 3)
    n_ticks = len(store.list_ticks(TD))
    assert r.maybe_close(None, et(15, 59, 30)) is None
    out = r.maybe_close(None, et(16, 1))
    assert out["session_close"] and brain.ctxs[-1].session_close
    assert brain.ctxs[-1].ts == brain.ctxs[-2].ts and brain.ctxs[-1].transitions == []
    assert len(store.list_ticks(TD)) == n_ticks          # no tick row for the fallback
    assert r.maybe_close(None, et(16, 2)) is None


def test_brain_exception_does_not_lose_tick(monkeypatch):
    def bad(ctx):
        raise RuntimeError("boom")
    r, store, clock = _runner(monkeypatch, brain=bad)
    spx_levels(store)
    out = Driver(r, clock, et(9, 31)).tick(7001.0)
    assert out is not None and len(store.list_ticks(TD)) == 1
    assert r.latest_decision["status"] == "no_decision"


def test_phase1_minimal_decision(monkeypatch):
    r, store, clock = _runner(monkeypatch)
    spx_levels(store)
    Driver(r, clock, et(9, 31)).tick(7020.0)
    dec = r.latest_decision
    assert dec["call"] is None and dec["status"] == "no_brain"
    assert dec["zone"]["kind"] == "between" and dec["scenario"]["lean"] == "none"


def test_new_trade_date_resets(monkeypatch):
    brain = StubBrain()
    r, store, clock = _runner(monkeypatch, brain=brain)
    spx_levels(store)
    Driver(r, clock, et(15, 50)).run([7020.0] * 3)
    nxt = TD + timedelta(days=3)
    spx_levels(store, prices=(7100.0,), d=nxt)
    clock.t = et(9, 31, 2, d=nxt)
    out = r.process(feed(et(9, 31, d=nxt), 7090.0))
    assert out["trade_date"] == nxt and brain.resets == [TD, nxt]
    # the previous day was closed (fallback) before the reset
    assert store.get_day_summary(TD) is not None
    assert [c.price for c in r.clusters] == [7100.0]


def test_warm_start_restores_live_state(monkeypatch):
    clock = Clock(et(9, 30))
    store = make_store(monkeypatch, clock)
    spy = store.add_level(TD, {"symbol": "SPY", "price": 703.0, "name": "spy lvl"}, TD)
    a, b = spx_levels(store)
    live = TickRunner(CFG, store, brain=StubBrain(), today=lambda: TD, now=clock)
    d = Driver(live, clock, et(9, 31))
    d.run(UP_AND_HOLD[:25], spy=700.5)
    clock.t = d.ts - timedelta(seconds=3)
    store.update_level(b["id"], {"price": 7045.0}, TD)  # mid-session edit
    d.run(UP_AND_HOLD[25:], spy=700.5)
    n_ticks, n_states = len(store.list_ticks(TD)), len(store.list_rows("level_state", TD))
    n_beliefs = len(store.list_rows("beliefs", TD))

    warm = TickRunner(CFG, store, brain=StubBrain(), today=lambda: TD, now=clock)
    assert warm.warm_start(TD) == n_ticks
    # nothing persisted by the warm start
    assert len(store.list_ticks(TD)) == n_ticks
    assert len(store.list_rows("level_state", TD)) == n_states
    assert len(store.list_rows("beliefs", TD)) == n_beliefs
    assert warm.level_map.snapshot() == live.level_map.snapshot()
    assert warm.ratio.ratio == live.ratio.ratio and warm.ratio.locked
    assert [c.to_dict() for c in warm.clusters] == [c.to_dict() for c in live.clusters]
    assert warm.latest_decision == live.latest_decision
    assert warm.trace_tail == live.trace_tail
    # both continue identically
    t = d.ts
    for i, s in enumerate(path(7010, 6985, 12)):
        ts = t + timedelta(seconds=20 * i)
        clock.t = ts + timedelta(seconds=2)
        o1 = live.process(feed(ts, s, spy=700.5))
        o2 = warm.process({**feed(ts, s, spy=700.5)})
        assert o1["transitions"] == o2["transitions"] and o2["levels_changed"] is False


def test_feed_from_tick_row_round_trip(monkeypatch):
    r, store, clock = _runner(monkeypatch)
    spx_levels(store)
    Driver(r, clock, et(9, 31)).tick(7001.0)
    row = store.list_ticks(TD)[0]
    f = feed_from_tick_row(row, extras={"engine_pick": {"x": 1}})
    assert f["spot"] == 7001.0 and f["ts"] == et(9, 31) and f["market_open"] is True
    assert f["contracts"] == row["chain"] and f["engine_pick"] == {"x": 1}
    assert f["spx_prevclose"] == 6990.0 and f["levels_as_of"]


def test_brain_output_persistence_fields(monkeypatch):
    seen = {}

    def brain(ctx):
        seen["mem"] = ctx.day_memory("beliefs")
        return BrainOutput(belief={"call": "wait", "trace": "t"},
                           evidence=[{"agent": "a", "scenario": "up", "weight": 0.2,
                                      "ts": "ignored", "trade_date": "ignored"}])
    r, store, clock = _runner(monkeypatch, brain=brain)
    spx_levels(store)
    d = Driver(r, clock, et(9, 31))
    d.run([7020.0] * 3)
    ev = store.list_rows("evidence", TD)
    assert ev[0]["trade_date"] == TD and ev[0]["ts"] == et(9, 31)
    # day_memory shows only rows strictly before this tick
    assert len(seen["mem"]) == 2
