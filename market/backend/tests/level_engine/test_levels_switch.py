"""Levels switch (contract C1–C3): binary grading (§12), R/R health (§8.4
revised), the full strategy grid in the decision, store recent / open legs."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pytest

from app.strategy_engine import composite_score
from level_engine import strategy as st
from level_engine.brain.day_summary import binary_result, leg_stats
from level_engine.brain.grading import Grader
from level_engine.config import ConfigError, LevelConfig, from_mapping
from level_engine.store import MemoryStore

from . import test_strategy as ts
from ._brain_sim import Sim, drift, reclaim_upper

CFG = LevelConfig()
TD = date(2026, 11, 20)
T0 = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


# ============================================================================
# config
# ============================================================================
def test_hit_tol_config_default_and_override():
    assert CFG.hit_tol_pts == 2.0
    assert from_mapping({"HIT_TOL_PTS": "1.5"}).hit_tol_pts == 1.5
    with pytest.raises(ConfigError):
        from_mapping({"HIT_TOL_PTS": "-1"})


# ============================================================================
# binary grading — Grader driven directly (exact spots)
# ============================================================================
@dataclass(frozen=True)
class Lvl:
    id: str
    price: float
    name: str


@dataclass
class Ctx:
    spot: float
    ts: datetime
    cfg: LevelConfig = CFG
    trade_date: date = TD
    feed: dict = field(default_factory=dict)
    session_close: bool = False


U, Y, L = Lvl("c:U", 7020.0, "U"), Lvl("c:Y", 7040.0, "Y"), Lvl("c:L", 7000.0, "L")


class G:
    def __init__(self, call="up", target=Y, inv=U, spot=7025.0, cfg=CFG):
        self.g, self.cfg, self.ts = Grader(cfg), cfg, T0
        row, _ = self.g.on_call(self.ctx(spot), call, target, inv, None)
        assert row is not None
        self.id = row["id"]
        self.row = row

    def ctx(self, spot, **kw):
        self.ts += timedelta(seconds=20)
        return Ctx(spot=spot, ts=self.ts, cfg=self.cfg, **kw)

    def mark(self, spot):
        return self.g.mark(self.ctx(spot))

    @property
    def leg(self):
        return self.g.legs[0]                 # the leg opened in __init__


def test_leg_row_carries_levels_and_binary_fields():
    g = G()
    assert g.row["target_level"] == {"price": 7040.0, "name": "Y"}
    assert g.row["invalidation_level"] == {"price": 7020.0, "name": "U"}
    assert g.row["close_reason"] is None and g.row["exit_spot"] is None


def test_hit_within_tolerance_is_a_win():
    g = G()
    assert g.mark(7037.9) == [] or g.leg.open                        # 2.1 short: open
    assert g.leg.open
    upd = g.mark(7038.0)                                             # exactly HIT_TOL_PTS
    assert g.leg.result == "win" and g.leg.close_reason == "target"
    assert g.leg.exit_spot == 7038.0 and g.leg.time_to_target_min is not None
    assert upd[0]["result"] == "win" and upd[0]["close_reason"] == "target"
    assert upd[0]["exit_spot"] == 7038.0


def test_down_hit_within_tolerance_mirrors():
    g = G(call="down", target=L, inv=U, spot=7015.0)
    g.mark(7002.5)
    assert g.leg.open
    g.mark(7001.9)
    assert g.leg.result == "win" and g.leg.close_reason == "target"


def test_near_miss_then_flip_is_a_loss():
    g = G()
    g.mark(7037.5)                                                   # 2.5 short of Y: no hit
    assert g.leg.open
    _, upd = g.g.on_call(g.ctx(7030.0), "down", L, Y, None)
    assert g.leg.result == "loss" and g.leg.close_reason == "flipped"
    assert g.leg.exit_spot == 7030.0 and g.leg.time_to_target_min is None
    assert upd[0]["close_reason"] == "flipped"


def test_near_miss_then_session_close_is_a_loss():
    g = G()
    g.mark(7037.99)
    upd = g.g.close_session(g.ctx(7036.0, session_close=True))
    assert g.leg.result == "loss" and g.leg.close_reason == "session_close"
    assert upd[0]["exit_spot"] == 7036.0


def test_invalidation_is_a_loss_with_reason():
    g = G()
    g.mark(7017.4)                                                   # U − 2.6 < −BUFFER
    assert g.leg.result == "loss" and g.leg.close_reason == "invalidation"


def test_tolerance_is_configurable():
    cfg = CFG.with_overrides(hit_tol_pts=0.0)
    g = G(cfg=cfg)
    g.mark(7039.9)
    assert g.leg.open
    g.mark(7040.0)
    assert g.leg.result == "win"


def test_range_held_and_exit():
    g = G(call="range", target=U, inv=L, spot=7010.0)
    g.mark(7019.0)
    _, upd = g.g.on_call(g.ctx(7012.0), "up", Y, U, None)
    assert g.leg.result == "win" and g.leg.close_reason == "zone_held"
    assert g.leg.exit_spot == 7012.0

    g2 = G(call="range", target=U, inv=L, spot=7010.0)
    g2.mark(7023.0)                                                   # U + 3 > band
    assert g2.leg.result == "loss" and g2.leg.close_reason == "zone_exit"

    g3 = G(call="range", target=U, inv=L, spot=7010.0)
    g3.g.close_session(g3.ctx(7011.0, session_close=True))
    assert g3.leg.result == "win" and g3.leg.close_reason == "zone_held"


def test_restore_reads_close_reason_exit_spot_and_level_columns():
    g = G()
    g.mark(7038.0)
    row = {**g.row, **g.leg.fields()}
    h = Grader(CFG)
    h.restore([row], clusters=[], current_call="up")                # levels not in clusters
    leg = h.legs[0]
    assert leg.close_reason == "target" and leg.exit_spot == 7038.0
    assert leg.target_price == 7040.0 and leg.inv_name == "U"


def test_day_summary_is_binary_and_tolerates_old_rows():
    g = G()
    g.mark(7038.0)
    old = Grader(CFG)
    old.restore([{"id": f"{TD}:7", "call": "down", "opened_at": T0, "entry_spot": 7010.0,
                  "result": "flipped"},
                 {"id": f"{TD}:8", "call": "up", "opened_at": T0, "entry_spot": 7010.0,
                  "result": "unresolved"}], [], None)
    stats = leg_stats(g.g.legs + old.legs)
    t = stats["total"]
    assert set(t) == {"n", "win", "loss", "open", "hit_rate"}           # no flipped / unresolved
    assert (t["n"], t["win"], t["loss"], t["hit_rate"]) == (3, 1, 2, round(1 / 3, 4))
    assert stats["by_reason"] == {"flipped": 1, "session_close": 1, "target": 1}
    assert binary_result("unresolved") == ("loss", "session_close")
    assert binary_result("win", "target") == ("win", "target")


def test_real_brain_legs_are_binary():
    s = Sim()
    reclaim_upper(s, hold_sec=600)
    s.ramp(7028, 7021, 120)
    s.run([7020, 7019, 7020, 7021, 7019, 7018.5] * 30)
    s.tick(7019, session_close=True)
    assert s.mem.rows["legs"]
    for r in s.mem.rows["legs"]:
        assert r["result"] in ("win", "loss")
        assert r["close_reason"] in ("target", "invalidation", "flipped", "session_close",
                                     "zone_held", "zone_exit")
        assert r["exit_spot"] is not None


# ============================================================================
# R/R health (§8.4 revised)
# ============================================================================
@pytest.mark.parametrize("x,h", [(None, None), (-0.5, "capital_trap"), (0.2999, "capital_trap"),
                                 (0.30, "thin"), (0.5999, "thin"), (0.60, "healthy"),
                                 (1.9, "healthy")])
def test_rr_health_bands(x, h):
    assert st.rr_health(x) == h


def test_finish_sets_rr_health_before_composite_and_keeps_legacy():
    base = {"health": "broken", "ev": 5.0, "ev_adjusted": 5.0, "liquidity": "high",
            "pop_pct": 40.0, "limit_premiums": None}
    good = st.finish({**base, "rr": {"near": {"cost": 100.0, "value": 190.0, "pnl": 90.0,
                                              "target": 7280.0}, "extreme": None}})
    bad = st.finish({**base, "rr": {"near": {"cost": 100.0, "value": 110.0, "pnl": 10.0,
                                             "target": 7280.0}, "extreme": None}})
    assert good["rr_profit"] == 0.9 and good["health"] == "healthy"
    assert bad["rr_profit"] == 0.1 and bad["health"] == "capital_trap"
    assert good["health_legacy"] == bad["health_legacy"] == "broken"
    assert good["composite_score"] == composite_score(good) > bad["composite_score"]
    ext_only = st.finish({**base, "rr": {"near": None, "extreme": {"cost": 50.0, "value": 85.0,
                                                                   "pnl": 35.0, "target": 1}}})
    assert ext_only["rr_profit"] == 0.7 and ext_only["health"] == "healthy"
    assert st.finish({**base, "rr": {"near": None, "extreme": None}})["health"] is None


def test_every_candidate_has_rr_health_and_no_dte_veto():
    p, _ = ts.up_pick()
    grid = p["strategies"]
    cands = [c for e in grid.values() for c in e["all_widths"]]
    assert cands
    for c in cands:
        assert c["rr"] is not None and "rr_profit" in c and "health_legacy" in c
        assert c["health"] == st.rr_health(st.rr_profit_of(c["rr"]))
        assert c["health"] != "broken"                                # no DTE veto
        assert c["composite_score"] == composite_score(c)              # ranked on R/R badge
        r = c["rr"]["near"]
        if r is not None:
            assert c["rr_profit"] == round((r["value"] - r["cost"]) / r["cost"], 4)
    # the legacy scorer's 0DTE debit rule survives as health_legacy only
    bc = grid["bull_call"]["best"]
    assert bc["health_legacy"] == "broken" and bc["health"] in ("healthy", "thin", "capital_trap")
    assert p["health"] == grid[p["strategy"]]["best"]["health"]
    assert p["health_legacy"] == grid[p["strategy"]]["best"]["health_legacy"]
    assert p["rr_profit"] == grid[p["strategy"]]["best"]["rr_profit"]
    for c in p["candidates"]:
        assert {"health", "health_legacy", "rr_profit"} <= set(c)


# ============================================================================
# grid (C1)
# ============================================================================
GRID_KEYS = {"strategy", "name", "short", "type", "side", "is_recommended", "best", "all_widths"}
EXTRA_KEYS = {"type", "limit_premiums", "pick_id", "rr", "rr_line", "rr_profit", "reach",
              "health_legacy"}


@pytest.mark.parametrize("call", ["up", "down", "range"])
def test_grid_shape_per_contract(call):
    targets = {"up": (ts.BY["C"], ts.BY["B"], ts.BY["B"]),
               "down": (ts.BY["C"], ts.BY["B"], ts.BY["C"]),
               "range": (ts.BY["C"], ts.BY["B"], None)}[call]
    p = st.StrategyAgent().pick(ts.ctx(), call, *targets)
    grid = p["strategies"]
    assert list(grid) == list(dict.fromkeys(
        s for s in st.CANDIDATES_BY_CALL[call] if s in grid))        # candidate order
    assert sum(e["is_recommended"] for e in grid.values()) == 1
    assert grid[p["strategy"]]["is_recommended"]
    assert grid[p["strategy"]]["best"]["pick_id"] == p["pick_id"]
    for key, e in grid.items():
        assert set(e) == GRID_KEYS and e["strategy"] == key
        assert (e["name"], e["short"]) == st.NAMES[key]
        assert e["type"] in ("debit", "credit") and e["side"] in ("bull", "bear", "neutral")
        assert e["all_widths"][0] is e["best"]
        for c in e["all_widths"]:
            assert set(ts.LEGACY_KEYS) <= set(c) and EXTRA_KEYS <= set(c)
            assert c["width_preset"] == "levels" and c["strategy"] == key
            assert c["spread_type"] == c["type"]
            assert all(leg.get("symbol") for leg in c["legs"])        # orderable (OCC)
            assert c["predicted_direction"] == {"up": "up", "down": "down", "range": "flat"}[call]
        if call == "range":
            assert e["side"] == "neutral"
    if call == "up":
        assert grid["bull_call"]["side"] == "bull" and grid["straddle"]["side"] == "neutral"
        lc = grid["long_call"]
        assert [c["reach"] for c in lc["all_widths"]] == ["near", "extreme"]
        assert lc["best"]["reach"] == "near"
    if call == "down":
        assert grid["bear_put"]["side"] == "bear"
        assert [c["reach"] for c in grid["long_put"]["all_widths"]] == ["near", "extreme"]
    if call == "range":
        assert grid["iron_condor"]["type"] == "credit"
    # every grid pick_id is unique and covers the compact candidates + the pick
    ids = [c["pick_id"] for e in grid.values() for c in e["all_widths"]]
    assert len(ids) == len(set(ids))
    assert set(ids) == {p["pick_id"]} | {c["pick_id"] for c in p["candidates"]}


def test_brain_moves_grid_to_decision_and_never_persists_it():
    s = Sim(strategy="auto")
    s.run(drift(7010.0, 5))
    assert s.last.decision["call"] == "wait" and s.last.decision["strategies"] == {}
    reclaim_upper(s)
    picked = [o for o in s.outs if o and o.decision["pick"]]
    assert picked
    for o in picked:
        grid = o.decision["strategies"]
        assert grid and grid[o.decision["pick"]["strategy"]]["is_recommended"]
        assert "strategies" not in o.decision["pick"] and "strategies" not in (o.pick or {})
    for o in s.outs:
        if o and o.decision["pick"] is None:
            assert o.decision["strategies"] == {}
    assert all("strategies" not in (r.get("pick") or {}) for r in s.mem.rows["legs"])
    s.ramp(7028, 7050, 300)
    s.stay(7050, 300)
    assert s.last.decision["call"] == "done" and s.last.decision["strategies"] == {}


# ============================================================================
# store: recent / open legs
# ============================================================================
def _leg(td, n, opened, closed=None, result=None):
    return {"id": f"{td.isoformat()}:{n}", "trade_date": td, "call": "up", "opened_at": opened,
            "closed_at": closed, "result": result}


def test_memory_store_recent_and_open_legs():
    m = MemoryStore()
    d0, d1 = TD - timedelta(days=1), TD
    t = lambda d, h: datetime(d.year, d.month, d.day, h, tzinfo=timezone.utc)  # noqa: E731
    m.insert("legs", _leg(d0, 1, t(d0, 15), t(d0, 16), "win"))
    m.insert("legs", _leg(d0, 2, t(d0, 17), None, None))                   # stale open, old day
    m.insert("legs", _leg(d1, 1, t(d1, 15), t(d1, 18), "loss"))
    m.insert("legs", _leg(d1, 2, t(d1, 16), t(d1, 17), "win"))
    m.insert("legs", _leg(d1, 10, t(d1, 19), None, None))
    assert [r["id"] for r in m.list_recent_legs(50)] == [f"{d1}:1", f"{d1}:2", f"{d0}:1"]
    assert [r["id"] for r in m.list_recent_legs(1)] == [f"{d1}:1"]
    assert m.list_recent_legs(0) == []
    assert m.get_open_leg(d1)["id"] == f"{d1}:10"
    assert m.get_open_leg(d0)["id"] == f"{d0}:2"
    assert m.get_open_leg(TD + timedelta(days=1)) is None
    assert m.get_open_leg()["id"] == f"{d1}:10"
    m.list_recent_legs(5)[0]["result"] = "x"                                # copies
    assert m.list_recent_legs(5)[0]["result"] == "loss"
