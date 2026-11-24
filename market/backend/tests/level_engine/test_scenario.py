"""scenario.py — §7.2 table + §9 acceptance cases at the scenario level.

Map: W 6980, L 7000, U 7020, Y 7040 (20 s ticks, default LevelConfig).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from level_engine import state_machine as sm
from level_engine.config import LevelConfig
from level_engine.levels import Cluster
from level_engine.scenario import map_scenario

CFG = LevelConfig()
T0 = datetime(2026, 11, 20, 14, 40, tzinfo=timezone.utc)


def cl(price, name):
    return Cluster(id=f"c:{name}", price=price, name=name, kind=None, level_ids=(name,),
                   names=(name,), strike=price, low=price, high=price)


class Sim:
    def __init__(self, prices=((6980.0, "W"), (7000.0, "L"), (7020.0, "U"), (7040.0, "Y"))):
        self.m = sm.LevelMap([cl(p, n) for p, n in prices], CFG)
        self.ts, self.prev, self.spot = T0, None, None

    def tick(self, spot):
        self.m.step(self.ts, spot, self.prev)
        self.prev = self.spot = spot
        self.ts += timedelta(seconds=20)
        return map_scenario(self.m, spot)

    def run(self, spots):
        sc = None
        for s in spots:
            sc = self.tick(s)
        return sc

    def stay(self, spot, seconds):
        return self.run([spot] * int(seconds // 20))

    def state(self, name):
        return self.m.tracker(f"c:{name}").state


def names(sc):
    n = lambda c: c.name if c is not None else None
    return (sc.lean, n(sc.up_target), n(sc.down_target), n(sc.invalidation_up),
            n(sc.invalidation_down))


# ---- §9: opens inside a zone and drifts ----
def test_opens_inside_zone_and_drifts_no_event():
    s = Sim()
    sc = s.run([7010, 7008, 7012, 7009, 7011] * 10)
    assert sc.zone.kind == "between" and sc.reference_level is None
    assert names(sc) == ("none", "U", "L", "L", "U") and not sc.pending


# ---- §9: reclaim upper and hold 10 min ----
def test_reclaim_upper_and_hold_is_up_to_next():
    s = Sim()
    s.run([7010, 7014, 7017, 7019])
    sc = s.tick(7023)                                     # cross U (Δ4)
    assert s.state("U") == sm.CROSSED_UP
    # U is now the zone's LOWER — the event is still read as "upper → crossed_up"
    assert sc.zone.lower.name == "U" and sc.zone.upper.name == "Y"
    assert names(sc) == ("up", "Y", "U", "U", "Y") and sc.pending
    assert sc.reference_level.name == "U" and sc.reference_event == sm.CROSSED_UP
    sc = s.stay(7024, 600)
    assert s.state("U") == sm.HELD_UP
    assert names(sc) == ("up", "Y", "U", "U", "Y") and not sc.pending


def test_wick_into_band_keeps_pending_up():
    s = Sim()
    s.run([7010, 7014, 7017, 7019, 7023, 7024])
    sc = s.tick(7021)                                     # wick back into U's band
    assert sc.zone.kind == "in_band" and sc.zone.lower is sc.zone.upper
    assert names(sc) == ("up", "Y", "U", "U", "Y") and sc.pending


# ---- §9: cross upper then fall back within 10 min ----
def test_cross_upper_then_fail_is_down_to_lower():
    s = Sim()
    s.run([7010, 7014, 7017, 7019, 7023])
    s.stay(7024, 200)
    s.tick(7021)
    sc = s.tick(7017)                                     # back below U (Δ4)
    assert s.state("U") == sm.FAILED_UP
    assert sc.zone.kind == "between" and sc.zone.upper.name == "U"
    assert names(sc) == ("down", "U", "L", "L", "U") and not sc.pending
    assert sc.reference_event == sm.FAILED_UP


# ---- §9: knife through lower ----
def test_knife_through_lower_is_down_to_next_below():
    s = Sim()
    s.run([7010, 7006, 7003])
    sc = s.tick(6995)                                     # Δ8 through L in one tick
    assert s.state("L") == sm.BROKEN_DOWN
    assert sc.zone.upper.name == "L" and sc.zone.lower.name == "W"
    assert names(sc) == ("down", "L", "W", "W", "L")


def test_lower_failed_down_is_up():
    s = Sim()
    s.run([7010, 7006, 7003, 7001, 6997])                 # crossed_down L
    assert s.state("L") == sm.CROSSED_DOWN
    sc = s.tick(6997)
    assert names(sc) == ("down", "L", "W", "W", "L") and sc.pending
    sc = s.run([6999, 7003])
    assert s.state("L") == sm.FAILED_DOWN
    assert names(sc) == ("up", "U", "L", "L", "U")


def test_lower_held_down_and_broken_up():
    s = Sim()
    s.run([7010, 7006, 7003, 7001, 6997])
    sc = s.stay(6996, 600)
    assert s.state("L") == sm.HELD_DOWN
    assert names(sc) == ("down", "L", "W", "W", "L")
    s.run([6999, 7003])                                   # crossed_up from held_down
    sc = s.stay(7004, 600)
    assert s.state("L") == sm.BROKEN_UP                   # resistance taken
    assert names(sc) == ("up", "U", "L", "L", "U")


# ---- §9: zigzag 3x → contested → range ----
def test_zigzag_contested_is_range():
    s = Sim()
    s.run([6990, 6997, 6999, 7003, 7001, 6997, 6999])
    sc = s.tick(7003)
    assert s.state("L") == sm.CONTESTED
    assert names(sc) == ("range", "U", "L", "L", "U")    # spot between L and U
    sc = s.tick(7001)                                     # in L's band → neighbours
    assert sc.zone.kind == "in_band"
    assert names(sc) == ("range", "U", "W", "W", "U") and sc.reference_event == sm.CONTESTED


def test_contested_on_either_bracketing_level_wins():
    s = Sim()
    s.run([6990, 6997, 6999, 7003, 7001, 6997, 6999, 7003])   # L contested
    s.run([7006, 7010, 7014, 7017, 7019, 7023])               # U crossed_up (more recent)
    s.run([7021, 7017])                                       # U failed_up → spot in L/U
    assert s.state("L") == sm.CONTESTED and s.state("U") == sm.FAILED_UP
    sc = s.tick(7016)
    assert names(sc)[0] == "range"


# ---- both bracketing levels have events: most recent wins ----
def test_most_recent_event_wins():
    s = Sim()
    s.run([6990, 6997, 6999, 7003])                       # L crossed_up
    s.stay(7004, 600)                                     # L held_up (lean up → U)
    assert s.state("L") == sm.HELD_UP
    s.run([7008, 7012, 7016, 7019, 7023])                 # U crossed_up
    s.run([7021, 7017])                                   # U failed_up (most recent)
    sc = s.tick(7016)
    assert s.state("L") == sm.HELD_UP and s.state("U") == sm.FAILED_UP
    assert names(sc) == ("down", "U", "L", "L", "U") and sc.reference_level.name == "U"


def _same_tick_setup(final_spot):
    s = Sim(prices=((7000.0, "L"), (7010.0, "U"), (7030.0, "Y")))
    s.run([6990, 6997, 6999])
    s.tick(7003)                                          # L crossed_up at tick k
    s.run([7006, 7008, 7013])                             # U crossed_up (Δ5, not a knife)
    assert s.state("L") == sm.CROSSED_UP and s.state("U") == sm.CROSSED_UP
    s.stay(7013, 600 - 4 * 20)                            # until the tick L's hold completes
    sc = s.tick(final_spot)                               # L → held_up, U → one-tick failed_up (lean down)
    assert s.state("L") == sm.HELD_UP and s.state("U") == sm.FAILED_UP
    assert s.m.tracker("c:L").since == s.m.tracker("c:U").since
    return sc


def test_same_tick_tie_larger_d_wins():
    sc = _same_tick_setup(7004.5)                         # |d| L 4.5 < U 5.5 → U wins
    assert names(sc) == ("down", "U", "L", "L", "U")


def test_same_tick_exact_tie_opposite_leans_is_none():
    sc = _same_tick_setup(7005.0)                         # |d| 5 == 5
    assert names(sc) == ("none", "U", "L", "L", "U") and sc.reference_level is None


# ---- §9: passes the highest level → done ----
def test_passes_highest_level_is_done():
    s = Sim()
    s.run([7030, 7034, 7037, 7039])
    sc = s.tick(7043)
    assert sc.zone.kind == "above_all"
    assert sc.lean == "done" and sc.up_target is None and sc.down_target is None
    assert sc.reference_level.name == "Y" and sc.reference_event == sm.CROSSED_UP
    assert sc.invalidation_up is None and sc.invalidation_down is None
    sc = Sim().tick(6950)
    assert sc.zone.kind == "below_all" and sc.lean == "done"


def test_no_levels_is_none():
    sc = map_scenario(sm.LevelMap([], CFG), 7000)
    assert sc.zone.kind == "none" and sc.lean == "none" and sc.up_target is None


def test_edge_targets_none_when_no_next_level():
    s = Sim()
    s.run([7030, 7034, 7037, 7039, 7043])                 # Y crossed_up
    sc = s.tick(7041)                                     # back in Y's band
    assert sc.zone.kind == "in_band" and names(sc) == ("up", None, "Y", "Y", None)


# ---- §9: levels edited mid-session ----
def test_rebuild_mid_session_keeps_event_scenario():
    s = Sim()
    s.run([7010, 7014, 7017, 7019, 7023])                 # U crossed_up
    res = s.m.rebuild([cl(7000.0, "L"), cl(7020.0, "U"), cl(7060.0, "Z")])
    assert res["kept"] == ["c:L", "c:U"] and res["removed"] == ["c:W", "c:Y"]
    sc = s.tick(7024)
    assert s.state("U") == sm.CROSSED_UP
    assert names(sc) == ("up", "Z", "U", "U", "Z") and sc.pending


def test_to_dict_shape():
    s = Sim()
    sc = s.run([7010, 7014, 7017, 7019, 7023])
    d = sc.to_dict()
    assert d["lean"] == "up" and d["pending"] is True
    assert d["up_target"]["id"] == "c:Y" and d["invalidation_up"]["price"] == 7020.0
    assert d["zone"]["kind"] == "between" and d["reference_event"] == sm.CROSSED_UP
