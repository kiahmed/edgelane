"""Brain — board/Evidence, the §8.5 manager math, determinism, restart, strategy seam."""
from __future__ import annotations

import math
import re
from datetime import timedelta

import pytest

from level_engine.brain import WEIGHTS, Evidence, create_brain
from level_engine.brain.manager import Beliefs

from ._brain_sim import CFG, Sim, drift, reclaim_upper


# ---- board ----
def test_evidence_weight_is_clipped_and_scenario_checked():
    assert Evidence("a", "up", 3.0, "k").weight == 1.0
    assert Evidence("a", "down", -7, "k").weight == -1.0
    assert Evidence("a", "range", float("nan"), "k").weight == 0.0
    with pytest.raises(ValueError):
        Evidence("a", "sideways", 0.1, "k")
    row = Evidence("a", "up", 0.25, "k", {"x": 1}, "why", 2).to_row()
    assert row == {"agent": "a", "scenario": "up", "weight": 0.25, "kind": "k",
                   "detail": {"x": 1}, "reason": "why", "pass": 2}


def test_weights_table_is_bounded():
    flat = [v for v in WEIGHTS.values() if isinstance(v, (int, float))]
    flat += list(WEIGHTS["wall_strength"].values())
    assert all(0 <= v <= 1 for v in flat if v is not WEIGHTS["min_pace_min"])


# ---- §8.5 manager math, exactly ----
def test_manager_update_matches_spec_formula_each_tick():
    s = Sim()
    reclaim_upper(s, hold_sec=300)
    prev = None
    for o in s.outs:
        lo = o.belief["logodds"]
        if prev is not None:
            lam = 0.5 ** (lo["dt_sec"] / (CFG.evidence_half_life_min * 60))
            assert lo["lambda"] == pytest.approx(lam, abs=1e-9)
            k = lo["applied_pass"]
            for sc in ("up", "down", "range"):
                ev = sum(e["weight"] for e in o.evidence if e["scenario"] == sc
                         and e["kind"] != "lean"
                         and (e["agent"] == "statemachine" or e["pass"] == k))
                lean = CFG.lean_logodds if o.decision["lean"] == sc else 0.0
                exp = lam * prev[sc] + lean + CFG.evidence_scale * ev
                assert lo[sc] == pytest.approx(exp, abs=1e-5)
            z = sum(math.exp(lo[x]) for x in ("up", "down", "range"))
            assert o.decision["p"]["up"] == pytest.approx(math.exp(lo["up"]) / z, abs=1e-5)
        prev = lo


def test_decay_uses_actual_dt_not_nominal_tick():
    s = Sim()
    s.run(drift(7010.0, 3))
    s.ts += timedelta(seconds=40)                       # a 60 s gap
    o = s.tick(7010.0)
    assert o.belief["logodds"]["dt_sec"] == 60.0
    assert o.belief["logodds"]["lambda"] == pytest.approx(0.5 ** (60 / 900), abs=1e-9)


def test_non_advancing_tick_returns_none():
    s = Sim()
    s.tick(7010.0)
    s.ts -= timedelta(seconds=CFG.tick_sec)             # same ts again
    assert s.tick(7010.0) is None


class _Ctx:
    def __init__(self, ts, sc, cfg=CFG):
        self.cfg, self.ts, self.scenario, self.level_map = cfg, ts, sc, None


def test_flip_needs_margin_and_dwell():
    s = Sim()
    reclaim_upper(s, hold_sec=120)
    brain, ctx0 = s.brain, s.ctxs[-1]
    brain.beliefs.call, brain.beliefs.call_since = "up", ctx0.ts
    ctx = _Ctx(ctx0.ts + timedelta(minutes=2), ctx0.scenario)
    strong_down = {"up": 0.1, "down": 0.8, "range": 0.1}
    assert brain._decide(ctx, False, strong_down) == ("up", "dwell")
    ctx.ts = ctx0.ts + timedelta(minutes=CFG.min_dwell_min)
    assert brain._decide(ctx, False, strong_down) == ("down", "ok")
    weak = {"up": 0.3, "down": 0.55, "range": 0.15}       # lead 0.25 < FLIP_MARGIN
    assert brain._decide(ctx, False, weak) == ("up", "ok")
    assert brain._decide(ctx, True, strong_down) == ("up", "thin")   # thin keeps the call


def test_beliefs_drivers_ranked_by_contribution():
    b = Beliefs(CFG)
    b.add("up", 0.2, "a", "x", "small")
    b.add("down", -0.9, "b", "y", "big")
    b.add("up", 0.5, "c", "z", "mid")
    assert [r for _, _, r in b.drivers()] == ["big", "mid", "small"]


def test_trace_lists_drivers_in_order_of_magnitude():
    s = Sim()
    reclaim_upper(s)
    mags = [abs(float(m)) for m in re.findall(r"\d\) \w+ ([+-][\d.]+) ", s.last.belief["trace"])]
    assert len(mags) >= 3 and mags == sorted(mags, reverse=True)
    assert s.last.decision["reason"] in s.last.belief["trace"]


# ---- determinism / replay ----
def _scripted(sim):
    reclaim_upper(sim, hold_sec=300)
    sim.run([7021, 7019, 7016, 7014])
    sim.stay(7011, 400)
    sim.tick(7012, session_close=True)
    return sim


def test_same_inputs_twice_identical_outputs():
    a, b = _scripted(Sim()), _scripted(Sim())
    assert a.dump() == b.dump()


def test_reset_day_rebuilds_identically():
    s = _scripted(Sim())
    first = s.dump()
    t = Sim()
    t.brain = s.brain                                   # reuse the same brain object
    t.brain.reset_day(None)
    _scripted(t)
    assert t.dump() == first


# ---- restart from day memory ----
def test_restart_restores_call_beliefs_and_open_leg():
    s = Sim()
    reclaim_upper(s, hold_sec=600)
    last = s.last
    leg_id = s.mem.rows["legs"][-1]["id"]
    s.brain = create_brain(CFG, strategy=None)          # process restart, same store
    o = s.tick(7028.2)
    assert o.decision["call"] == "up"
    assert o.belief["logodds"]["up"] > 0.9 * last.belief["logodds"]["up"]
    assert s.brain.grader.current.id == leg_id
    s.ramp(7028.2, 7041, 120)
    assert s.leg(leg_id)["result"] == "win"
    n = int(leg_id.rsplit(":", 1)[1])
    assert all(int(r["id"].rsplit(":", 1)[1]) <= n + 1 for r in s.mem.rows["legs"])


# ---- strategy seam ----
class FakeStrategy:
    def __init__(self, boom=False):
        self.calls, self.boom = [], boom

    def pick(self, ctx, call, up_t, dn_t, inv):
        self.calls.append(call)
        if self.boom:
            raise RuntimeError("boom")
        return {"pick_id": f"p-{call}", "strategy": "bull_call", "spread_type": "debit",
                "net_premium": 2.0, "legs": [{"strike": 7025.0, "side": "call", "long_short": 1}],
                "target_level": {"price": 0}, "confidence": None}

    @staticmethod
    def reprice(pick, contracts):
        c = next(c for c in contracts if c["strike"] == 7025.0 and c["side"] == "call")
        return (c["bid"] + c["ask"]) / 2


def test_pick_is_augmented_and_graded_with_pnl():
    fs = FakeStrategy()
    s = Sim(strategy=(fs.pick, fs.reprice))
    s.run(drift(7010.0, 10))
    assert all(o.pick is None for o in s.outs)          # wait → no pick, strategy not called
    assert fs.calls == []
    reclaim_upper(s, hold_sec=300)
    p = s.last.pick
    assert p["source"] == "levels" and p["pick_id"] == "p-up" and p["call"] == "up"
    assert p["target_level"] == {"id": "c:Y", "price": 7040.0, "name": "Y"}
    assert p["invalidation_level"]["name"] == "U"
    assert p["confidence"] == s.last.decision["confidence"] and p["reason"]
    assert s.last.decision["pick"] == p
    leg = s.mem.rows["legs"][-1]
    assert leg["pick"]["pick_id"] == "p-up" and leg["strategy_pnl"] > 0   # call gained value


def test_strategy_exception_means_no_pick_not_a_crash():
    fs = FakeStrategy(boom=True)
    s = Sim(strategy=(fs.pick, fs.reprice))
    reclaim_upper(s, hold_sec=120)
    assert fs.calls and s.last.pick is None and s.last.decision["call"] == "up"
    assert s.legs_opened()                              # shadow leg still opens


def test_no_pick_on_done():
    fs = FakeStrategy()
    s = Sim(strategy=(fs.pick, fs.reprice))
    reclaim_upper(s)
    s.ramp(7028, 7050, 300)
    n = len(fs.calls)
    s.stay(7050, 120)
    assert s.last.decision["call"] == "done" and s.last.pick is None and len(fs.calls) == n


def test_real_strategy_module_is_wired_when_present():
    s = Sim(strategy="auto")
    if s.brain._pick_fn is None:
        pytest.skip("level_engine.strategy not importable")
    reclaim_upper(s, hold_sec=300)
    p = s.last.pick
    assert p is not None and p["source"] == "levels" and p["pick_id"]
    assert p["confidence"] == s.last.decision["confidence"] and p["reason"]
    assert s.mem.rows["legs"][-1]["pick"]["pick_id"] == p["pick_id"] or s.legs_opened()
