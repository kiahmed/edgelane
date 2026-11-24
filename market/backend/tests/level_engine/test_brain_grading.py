"""Brain — §12 event-based grading and §13 day summary."""
from __future__ import annotations

from level_engine import state_machine as sm

from ._brain_sim import CFG, TD, Sim, drift, reclaim_upper


def _leg(s, call=None, i=-1):
    legs = [r for r in s.mem.rows["legs"] if call is None or r["call"] == call]
    return legs[i]


def test_up_leg_wins_at_target_with_time_to_target():
    s = Sim()
    reclaim_upper(s)
    leg_id = _leg(s, "up")["id"]
    s.ramp(7028, 7041, 120)
    r = s.leg(leg_id)
    assert r["result"] == "win" and r["close_reason"] == "target"
    # binary §12: hit as soon as spot is within HIT_TOL_PTS of Y (7040)
    assert 7040 - CFG.hit_tol_pts <= r["exit_spot"] < 7040
    assert 90.0 < r["progress_pct"] < 100.0
    assert r["time_to_target_min"] > 10 and r["closed_at"] is not None
    assert r["pick"] is None and r["strategy_pnl"] is None       # phase-2 shadow leg


def test_up_leg_loses_at_invalidation_with_mae():
    s = Sim()
    reclaim_upper(s, hold_sec=120)
    leg_id = _leg(s, "up")["id"]
    s.run([7021, 7019])
    assert s.leg(leg_id)["result"] is None                    # inside U's band: not yet
    s.tick(7016)                                              # < U − BUFFER
    r = s.leg(leg_id)
    assert r["result"] == "loss" and r["mae_pts"] == 7.0 and r["progress_pct"] < 0
    assert r["close_reason"] == "invalidation" and r["exit_spot"] == 7016.0
    assert r["time_to_target_min"] is None


def test_flipped_leg_without_a_hit_is_a_loss():
    s = Sim()
    reclaim_upper(s, hold_sec=600)
    up_id = _leg(s, "up")["id"]
    s.ramp(7028, 7021, 120)
    s.run([7020, 7019, 7020, 7021, 7019, 7018.5] * 30)
    r = s.leg(up_id)
    assert r["result"] == "loss" and r["close_reason"] == "flipped"
    assert r["progress_pct"] is not None and r["exit_spot"] is not None


def test_leg_at_session_close_without_hit_is_a_loss_and_no_open_on_close_tick():
    s = Sim()
    reclaim_upper(s)
    leg_id = _leg(s, "up")["id"]
    out = s.tick(7029, session_close=True)
    r = s.leg(leg_id)
    assert r["result"] == "loss" and r["close_reason"] == "session_close"
    assert r["exit_spot"] == 7029.0
    assert out.leg_open is None and out.decision["status"] == "session_close"
    assert out.day_summary is not None


def test_range_leg_wins_when_still_inside_at_close():
    s = Sim()
    s.run(drift(7010.0, 10))
    s.run([7014, 7017, 7019, 7023, 7024, 7020, 7016, 7015, 7019, 7023, 7024])
    s.stay(7023, 300)
    rid = _leg(s, "range")["id"]
    s.run([7020, 7016])
    s.stay(7016, 120)
    s.tick(7017, session_close=True)
    r = s.leg(rid)
    assert r["result"] == "win" and r["progress_pct"] is None and r["mae_pts"] == 0.0
    assert r["close_reason"] == "zone_held" and r["exit_spot"] == 7017.0


def test_range_leg_loses_when_price_leaves_the_zone():
    s = Sim()
    s.run(drift(7010.0, 10))
    s.run([7014, 7017, 7019, 7023, 7024, 7020, 7016, 7015, 7019, 7023, 7024])
    s.stay(7023, 300)
    rid = _leg(s, "range")["id"]
    s.ramp(7023, 7046, 240)                                   # out through Y's band
    r = s.leg(rid)
    assert r["result"] == "loss" and r["mae_pts"] > 0 and r["close_reason"] == "zone_exit"


def test_leg_ids_are_deterministic_per_day():
    s = Sim()
    reclaim_upper(s)
    s.ramp(7028, 7041, 120)
    ids = [r["id"] for r in s.mem.rows["legs"]]
    assert ids == [f"{TD.isoformat()}:{i}" for i in range(1, len(ids) + 1)]


def test_no_new_leg_after_a_loss_until_the_call_changes():
    s = Sim()
    reclaim_upper(s, hold_sec=120)
    s.run([7021, 7019, 7016])                                 # up leg → loss, call still up
    n = len(s.legs_opened())
    s.run([7016, 7015])
    assert s.calls()[-1] == "up" and len(s.legs_opened()) == n


# ---- §13 day summary ----
def test_day_summary_levels_outcome_and_leg_stats():
    s = Sim()
    reclaim_upper(s, hold_sec=600)                            # U held as support
    s.ramp(7028, 7041, 120)                                   # up leg wins at Y
    s.ramp(7041, 7034, 60)                                    # Y: crossed then failed back
    s.stay(7032, 120)
    out = s.tick(7031, session_close=True)
    ds = out.day_summary
    lo = ds["levels_outcome"]
    assert lo["c:U"]["outcome"] == "held_support"
    assert lo["c:W"]["outcome"] == "untested"
    assert lo["c:Y"]["outcome"] in ("failed", "held_resistance", "contested")
    assert ds["close"] == 7031 and ds["trade_date"] == TD
    assert ds["final_zone"]["kind"] == "between"
    t = ds["legs"]["total"]
    assert t["n"] == len(s.mem.rows["legs"]) and t["win"] >= 1
    assert ds["legs"]["by_call"]["up"]["hit_rate"] is not None
    assert "Y" in ds["legs"]["by_level"]
    assert "legs:" in ds["notes"]


def test_day_summary_broken_and_contested():
    s = Sim()
    s.run(drift(7006.0, 30))
    s.tick(7005)
    s.tick(6993)                                              # knife through L
    s.stay(6992, 60)
    out = s.tick(6992, session_close=True)
    assert out.day_summary["levels_outcome"]["c:L"]["outcome"] == "broken"

    z = Sim()
    z.run(drift(7010.0, 10))
    z.run([7014, 7017, 7019, 7023, 7024, 7020, 7016, 7015, 7019, 7023, 7024])
    assert z.state("U") == sm.CONTESTED
    out = z.tick(7023, session_close=True)
    assert out.day_summary["levels_outcome"]["c:U"]["outcome"] == "contested"


def test_day_summary_only_once():
    s = Sim()
    s.run(drift(7010.0, 5))
    assert s.tick(7010, session_close=True).day_summary is not None
    assert s.tick(7010, session_close=True).day_summary is None

