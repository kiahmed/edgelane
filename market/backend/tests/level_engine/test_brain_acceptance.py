"""Brain — §9 acceptance cases at the DECISION level.

Real LevelMap + map_scenario driven by synthetic 20 s SPX paths
(W 6980, L 7000, U 7020, Y 7040; default LevelConfig), no strategy (shadow legs).
"""
from __future__ import annotations

from level_engine import state_machine as sm
from level_engine.config import LevelConfig

from ._brain_sim import CFG, Sim, bias, drift, reclaim_upper


def _names(d):
    n = lambda x: x["name"] if x else None
    return d["call"], n(d["target_level"]), n(d["invalidation_level"])


# ---- opens inside a zone and drifts → wait, no pick, no leg ----
def test_opens_inside_zone_drifts_waits_without_leg():
    s = Sim()
    s.run(drift(7010.0, 75))                            # 25 min of in-zone wiggle
    assert set(s.calls()) == {"wait"}
    assert s.legs_opened() == []
    assert all(o.pick is None for o in s.outs)
    assert s.last.decision["lean"] == "none"


def test_long_drift_becomes_a_range_lean_only_after_a_full_window():
    s = Sim()
    s.run(drift(7010.0, 150))                           # 50 min
    first_range = s.calls().index("range")
    assert first_range * CFG.tick_sec >= CFG.detail_window_min * 60 - 2 * CFG.tick_sec
    stalls = s.evidence(kind="stall")
    assert stalls and all(e["scenario"] == "range" and e["agent"] == "down_watcher" for e in stalls)
    assert _names(s.last.decision) == ("range", "U", "L")


# ---- reclaim upper, hold 10 min, premium at next-up building → up ----
def test_reclaim_hold_premium_building_calls_up_to_next():
    s = Sim()
    reclaim_upper(s, hold_sec=600)
    assert s.state("U") == sm.HELD_UP
    assert _names(s.last.decision) == ("up", "Y", "U")
    assert s.last.decision["lean"] == "up" and s.last.decision["confidence"] > 0.9
    pb = [e for e in s.last.evidence if e["kind"] == "premium_build" and e["scenario"] == "up"]
    assert pb and pb[0]["weight"] > 0 and pb[0]["detail"]["opt"] == "call" \
        and pb[0]["detail"]["strike"] == 7040.0 and pb[0]["detail"]["trend"] == "building"
    leg = s.legs_opened()[-1]
    assert (leg["call"], leg["target_level_id"], leg["invalidation_level_id"]) == ("up", "c:Y", "c:U")
    assert "lean up: U held_up" in s.last.belief["trace"]


# ---- cross upper, fall back within 10 min → failed_up, lean down ----
def test_cross_then_fall_back_leans_down_and_flips_after_dwell():
    s = Sim()
    reclaim_upper(s, hold_sec=120)
    assert s.last.decision["call"] == "up"
    s.run([7021, 7019, 7016])
    assert s.state("U") == sm.FAILED_UP
    d = s.last.decision
    assert d["lean"] == "down" and d["call"] == "up"     # flip not yet allowed
    p_down_before = d["p"]["down"]
    s.stay(7011, 600)
    calls = s.calls()
    assert calls[-1] == "down" and _names(s.last.decision) == ("down", "L", "U")
    assert s.last.decision["p"]["down"] > p_down_before


# ---- knife through lower → broken_down → down to next below ----
def test_knife_through_lower_calls_down_to_next_below():
    s = Sim()
    s.run(drift(7006.0, 30))
    s.tick(7005)
    s.tick(6993)                                        # Δ12 > KNIFE_PTS through L
    assert s.state("L") == sm.BROKEN_DOWN
    s.stay(6992, 120)
    assert _names(s.last.decision) == ("down", "W", "L")
    assert s.legs_opened()[-1]["target_level_id"] == "c:W"


# ---- zigzag 3× across one level in 30 min → contested → range ----
def test_zigzag_is_contested_range_between_neighbours():
    s = Sim()
    s.run(drift(7010.0, 10))
    s.run([7014, 7017, 7019, 7023, 7024, 7020, 7016, 7015, 7019, 7023, 7024])
    assert s.state("U") == sm.CONTESTED
    s.stay(7023, 300)
    s.run([7020, 7016])
    s.stay(7016, 300)
    assert _names(s.last.decision) == ("range", "Y", "L")       # around U, not [U, Y]
    assert any(e["kind"] == "contested" and e["weight"] == 0.6 for e in s.last.evidence)
    rng = [x for x in s.legs_opened() if x["call"] == "range"]
    assert rng and (rng[0]["target_level_id"], rng[0]["invalidation_level_id"]) == ("c:Y", "c:L")
    assert s.mem.rows["legs"][-1]["result"] is None             # chop around U stays inside


# ---- derivatives say up, price flat → discounted, no call change ----
def test_derivatives_up_with_flat_price_are_discounted():
    s = Sim(bias_dict=bias(flow_call_m=50, flow_put_m=10, net_dex=-1e6))
    s.stay(7010.0, 1500)                                # 25 min flat
    assert set(s.calls()) == {"wait"}
    ag = s.evidence(kind="derivatives_agreement")
    assert ag and all(e["detail"]["mode"] == "discount" and e["scenario"] == "up" for e in ag)
    o = s.last
    up = sum(e["weight"] for e in o.evidence if e["scenario"] == "up" and e["pass"] == 0)
    assert abs(up) < 1e-6                               # flow + dex fully cancelled
    assert abs(o.belief["logodds"]["up"] - o.belief["logodds"]["down"]) < 1e-6


def test_derivatives_up_with_rising_price_are_amplified():
    s = Sim(bias_dict=bias(flow_call_m=50, flow_put_m=10, net_dex=-1e6))
    s.run(drift(7008.0, 15))
    s.ramp(7008, 7016, 300)
    modes = {e["detail"]["mode"] for e in s.outs[-1].evidence if e["kind"] == "derivatives_agreement"}
    assert modes == {"amplify"}
    assert s.last.decision["call"] == "up"


def test_derivatives_wall_pull_and_pin_posts():
    b = bias(put_wall=(7020.0, "high"), call_wall=(7015.0, "medium"))
    s = Sim(bias_dict=b)
    s.stay(7010.0, 60)
    wp = {e["detail"]["wall"]: e for e in s.last.evidence if e["kind"] == "wall_pull"}
    assert wp["call"]["scenario"] == "up" and wp["call"]["weight"] < 0     # barrier on the up path
    assert wp["put"]["scenario"] == "up" and wp["put"]["weight"] > 0       # magnet at the target
    far = Sim(bias_dict=bias(put_wall=(7040.0, "high")))                  # beyond U: ignored
    far.stay(7010.0, 60)
    assert not [e for e in far.last.evidence if e["kind"] == "wall_pull"]
    s2 = Sim(bias_dict=bias(put_wall=(7010.0, "high")))
    s2.stay(7010.0, 60)
    assert any(e["kind"] == "pin" and e["scenario"] == "range" for e in s2.last.evidence)


# ---- up call open, put premium builds + retouch origin → flip only past margin + dwell ----
def test_up_call_retouch_and_put_premium_accumulate_down_then_flip():
    s = Sim()
    reclaim_upper(s, hold_sec=600)
    s.ramp(7028, 7021, 120)
    i0 = len(s.outs)
    s.run([7020, 7019, 7020, 7021, 7019, 7018.5] * 30)
    outs = s.outs[i0:]
    assert any(e["kind"] == "retouch" and e["weight"] < 0 for e in outs[0].evidence)
    assert any(e["kind"] == "premium_build" and e["scenario"] == "down" and e["weight"] > 0
               for e in outs[10].evidence)
    gaps = [o.belief["logodds"]["up"] - o.belief["logodds"]["down"] for o in outs]
    assert gaps[30] < gaps[0]                          # down evidence accumulates
    flips = [k for k in range(1, len(outs))
             if outs[k - 1].decision["call"] == "up" and outs[k].decision["call"] == "down"]
    assert len(flips) == 1
    f = outs[flips[0]]
    assert f.decision["p"]["down"] - f.decision["p"]["up"] >= CFG.flip_margin
    for o in outs[:flips[0]]:                          # before: margin not met (or dwell)
        assert o.decision["p"]["down"] - o.decision["p"]["up"] < CFG.flip_margin \
            or o.decision["status"] == "dwell"
    flipped = [u for u in f.leg_updates
               if u.get("result") == "loss" and u.get("close_reason") == "flipped"]
    assert flipped                                      # the up leg closes: loss / flipped


# ---- passes the highest level → done, no new call ----
def test_passing_highest_level_is_done_with_no_new_leg():
    s = Sim()
    reclaim_upper(s)
    s.ramp(7028, 7050, 300)
    n_legs = len(s.legs_opened())
    s.stay(7050, 300)
    d = s.last.decision
    assert d["call"] == "done" and d["status"] == "done" and d["lean"] == "done"
    assert d["target_level"] is None and d["pick"] is None
    assert len(s.legs_opened()) == n_legs
    assert s.mem.rows["legs"][-1]["result"] == "win"   # the up leg hit Y on the way


# ---- thin evidence → re-pass, bounded by MAX_REPASS ----
def test_thin_evidence_repasses_bounded_and_keeps_call():
    s = Sim()
    s.run(drift(7010.0, 30))
    thin = [o for o in s.outs if o.belief["logodds"]["thin"]]
    assert thin and all(o.decision["status"] == "thin" for o in thin)
    assert max(e["pass"] for o in s.outs for e in o.evidence) == CFG.max_repass
    o = thin[-1]
    windows = sorted({e["detail"]["window_min"] for e in o.evidence if e["kind"] == "progress"})
    assert windows == [30.0, 60.0, 120.0]
    assert o.belief["logodds"]["applied_pass"] == CFG.max_repass


def test_max_repass_zero_means_single_pass():
    cfg = LevelConfig(max_repass=0)
    s = Sim(cfg=cfg)
    s.run(drift(7010.0, 20))
    assert {e["pass"] for o in s.outs for e in o.evidence} == {0}


def test_reclaim_is_not_thin_and_watchers_read_each_other():
    s = Sim()
    s.run(drift(7010.0, 95))                           # long enough for full windows
    o = s.last
    stall = [e for e in o.evidence if e["kind"] == "stall"]
    assert stall and "up_pace_ratio" in stall[0]["detail"] and "down_pace_ratio" in stall[0]["detail"]
