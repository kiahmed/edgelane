"""state_machine.py — §7.1 rules, timers, LevelMap / Zone, snapshot/restore.

Ticks are simulated every 20 s with explicit tz-aware timestamps. One level at
L = 7000 (BUFFER 2.5, TEST 6, HOLD 10 min, GRACE 60 s, KNIFE 5, 3 crosses/30 min).
A "normal" cross goes through the band (…6997 → 6999 → 7003…) so the one-tick
move stays <= KNIFE_PTS.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from level_engine import state_machine as sm
from level_engine.config import LevelConfig
from level_engine.levels import Cluster, build_clusters, ResolvedLevel

CFG = LevelConfig()
T0 = datetime(2026, 11, 20, 14, 40, tzinfo=timezone.utc)      # 09:40 ET
L = 7000.0


def cluster(price=L, id=None, name="L"):
    return Cluster(id=id or f"c:{int(price)}", price=price, name=name, kind=None,
                   level_ids=(name,), names=(name,), strike=price, low=price, high=price)


class Sim:
    def __init__(self, obj, t0=T0, dt=20):
        self.obj, self.ts, self.dt, self.prev = obj, t0, dt, None
        self.log: list = []          # (ts, transition) for every transition

    def tick(self, spot, dt=None):
        r = self.obj.step(self.ts, spot, self.prev)
        rs = r if isinstance(r, list) else ([r] if r else [])
        for x in rs:
            self.log.append(x)
        at = self.ts
        self.prev = spot
        self.ts = self.ts + timedelta(seconds=self.dt if dt is None else dt)
        return rs, at

    def run(self, spots):
        for s in spots:
            self.tick(s)

    def stay(self, spot, seconds):
        for _ in range(int(seconds // self.dt)):
            self.tick(spot)

    def states(self):
        return [x["state"] for x in self.log]


def tracker():
    t = sm.LevelTracker(cluster(), CFG)
    return t, Sim(t)


def cross_up(s):
    """From below: 6997 (side −1) → 6999 (band) → 7003 (cross up). Returns cross ts."""
    s.run([6997, 6999])
    _, at = s.tick(7003)
    return at


# ---------------------------------------------------------------- init/baseline
def test_init_and_baseline():
    t, s = tracker()
    rs, _ = s.tick(7010)
    assert rs[0]["prev_state"] is None and rs[0]["state"] == sm.AWAY_ABOVE
    assert rs[0]["detail"]["event"] == "init" and rs[0]["level_id"] == "c:7000"
    s.tick(7004)
    assert t.state == sm.TESTING
    s.tick(7001)
    assert t.state == sm.TESTING and t.side == 0
    s.tick(6990)  # 7001 → 6990: band → −1 with last nonzero +1: a cross, Δ11 but sign flip → knife
    assert t.state == sm.BROKEN_DOWN


def test_start_in_band_has_no_side_memory():
    t, s = tracker()
    s.run([7001, 7004, 6996])     # first exit (+1) is not a cross; then −1 IS a cross
    assert s.states()[0] == sm.TESTING
    assert t.state == sm.BROKEN_DOWN   # +1 → −1, Δ8 > 5, sign flip → knife
    t, s = tracker()
    s.run([7001, 7003.5, 7005])
    assert t.state == sm.TESTING and t.crosses_in_window(s.ts) == 0


def test_baseline_only_from_baseline_states():
    t, s = tracker()
    s.run([6985, 6996, 6985])
    assert s.states() == [sm.AWAY_BELOW, sm.TESTING, sm.AWAY_BELOW]


# ---------------------------------------------------------------- cross / hold
def test_cross_up_then_hold_10min_is_held_up():
    t, s = tracker()
    s.tick(6990)
    tc = cross_up(s)
    assert t.state == sm.CROSSED_UP and t.since == tc and t.last_event == sm.CROSSED_UP
    s.stay(7004, 580)
    assert t.state == sm.CROSSED_UP and t.hold_sec == 580
    rs, at = s.tick(7004)
    assert at == tc + timedelta(seconds=600)
    assert rs[0]["state"] == sm.HELD_UP and rs[0]["prev_state"] == sm.CROSSED_UP
    assert rs[0]["detail"]["event"] == "hold" and rs[0]["detail"]["hold_sec"] == 600
    assert t.last_held_side == 1 and rs[0]["last_held_side"] == 1


def test_timer_uses_ts_deltas_not_tick_count():
    t = sm.LevelTracker(cluster(), CFG)
    s = Sim(t, dt=30)
    s.tick(6990)
    tc = cross_up(s)
    s.stay(7004, 570)
    assert t.state == sm.CROSSED_UP
    rs, at = s.tick(7004)
    assert rs[0]["state"] == sm.HELD_UP and at - tc == timedelta(seconds=600)


def test_cross_then_fall_back_is_failed_up():
    t, s = tracker()
    s.tick(6990)
    cross_up(s)
    s.stay(7004, 200)
    s.run([7001])                  # band (timer pauses)
    rs, _ = s.tick(6997)           # back to origin side, Δ4 (not a knife)
    assert rs[0]["state"] == sm.FAILED_UP and rs[0]["detail"]["event"] == "fail"
    assert t.last_held_side is None and t.last_event == sm.FAILED_UP


def test_one_tick_fail_over_knife_pts_is_fail_not_knife():
    # Review fix: a reclaim that snaps back to its origin side in ONE tick is a
    # failed reclaim (rule 5), not a knife — with KNIFE=5 / BUFFER=2.5 every
    # direct +1 → −1 tick exceeds KNIFE_PTS by construction.
    t, s = tracker()
    s.tick(6990)
    cross_up(s)
    rs, _ = s.tick(6996)           # 7003 → 6996: Δ7 > KNIFE, sign flip
    assert rs[0]["state"] == sm.FAILED_UP and rs[0]["detail"]["event"] == "fail"
    assert t.last_held_side is None


def test_knife_still_breaks_away_and_held_levels():
    t, s = tracker()
    s.run([7010, 7004])            # away_above → testing (side +1)
    rs, _ = s.tick(6994)           # Δ10 through L in one tick
    assert rs[0]["state"] == sm.BROKEN_DOWN and rs[0]["detail"]["event"] == "knife"
    # a crossed_down level knifed UPWARD is still a knife only if the move
    # isn't back to origin — here origin is +1, so it's a fail, by design:
    t, s = tracker()
    s.run([7010, 7004, 7001, 6996])  # crossed_down via the band
    assert t.state == sm.CROSSED_DOWN
    rs, _ = s.tick(7004)           # back to origin side in one tick, Δ8
    assert rs[0]["state"] == sm.FAILED_DOWN


def test_wick_30s_in_band_pauses_not_resets():
    t, s = tracker()
    s.tick(6990)
    tc = cross_up(s)
    s.stay(7004, 200)              # ticks at tc+20..tc+200, hold credited to tc+200
    assert t.hold_sec == 200
    s.run([7001, 7001])            # ticks tc+220, tc+240 in band
    assert t.hold_sec == 220 and t.state == sm.CROSSED_UP   # paused (last above-interval credited)
    s.tick(7004)                   # back above at tc+260; band credited 40 s ≤ GRACE
    assert t.hold_sec == 220 and t.band_sec == 0
    s.stay(7004, 360)              # tc+280 … tc+620 → hold 220+360 = 580
    assert t.state == sm.CROSSED_UP
    rs, at = s.tick(7004)
    assert rs[0]["state"] == sm.HELD_UP and at == tc + timedelta(seconds=640)


def test_band_stay_longer_than_grace_resets_timer():
    t, s = tracker()
    s.tick(6990)
    tc = cross_up(s)
    s.stay(7004, 300)
    s.run([7001] * 4)              # band ticks; band intervals credited 20,40,60 so far
    assert t.hold_sec == 320 and t.band_sec == 60          # 60 is not > GRACE yet
    s.tick(7004)                   # 4th band interval → 80 s > 60 → reset
    assert t.hold_sec == 0 and t.state == sm.CROSSED_UP
    s.stay(7004, 580)
    assert t.state == sm.CROSSED_UP
    s.tick(7004)
    assert t.state == sm.HELD_UP


def test_band_stay_exactly_grace_does_not_reset():
    t, s = tracker()
    s.tick(6990)
    cross_up(s)
    s.stay(7004, 300)
    s.run([7001] * 3)
    s.tick(7004)                   # band credited 60 s == GRACE → not reset
    assert t.hold_sec == 320


def test_held_not_declared_from_inside_band():
    t, s = tracker()
    s.tick(6990)
    tc = cross_up(s)
    s.stay(7004, 580)
    s.tick(7001)                   # tc+600: hold reaches 600 but spot is in band
    assert t.state == sm.CROSSED_UP and t.hold_sec == 600
    rs, at = s.tick(7004)
    assert rs[0]["state"] == sm.HELD_UP and at == tc + timedelta(seconds=620)


def test_gap_credit_capped_and_no_knife_across_gap():
    t, s = tracker()
    s.tick(6990)
    cross_up(s)
    s.tick(7004, dt=600)           # 10-minute feed gap
    s.tick(7004)
    assert t.state == sm.CROSSED_UP and t.hold_sec == 20 + 40
    # knife-sized move across a gap is an ordinary fail, not a knife
    t, s = tracker()
    s.tick(6990)
    cross_up(s)
    s.tick(7003, dt=60)
    rs, _ = s.tick(6994)
    assert rs[0]["state"] == sm.FAILED_UP


def test_knife_needs_sign_flip_this_tick():
    t, s = tracker()
    s.run([7010, 7004, 6999])      # +1 then band below L (d −1)
    rs, _ = s.tick(6993)           # Δ6 but sign(d) already negative → ordinary cross
    assert rs[0]["state"] == sm.CROSSED_DOWN


# ---------------------------------------------------------------- knife / break / settle
def test_knife_down_clears_last_held_side():
    t, s = tracker()
    s.tick(6990)
    cross_up(s)
    s.stay(7004, 600)
    assert t.state == sm.HELD_UP and t.last_held_side == 1
    rs, _ = s.tick(6994)           # Δ10 through L
    assert rs[0]["state"] == sm.BROKEN_DOWN and t.last_held_side is None
    assert rs[0]["last_held_side"] is None
    s.stay(6994, 31 * 60)          # age the crosses out of the contest window
    # after a knife-cleared memory, a cross up + hold is a plain held_up
    s.run([6999, 7003])
    assert t.state == sm.CROSSED_UP
    s.stay(7004, 600)
    assert t.state == sm.HELD_UP


def test_break_of_held_level_and_role_flip():
    t, s = tracker()
    s.tick(6990)
    cross_up(s)
    s.stay(7004, 600)
    assert t.state == sm.HELD_UP
    s.run([7001, 6997])            # crossed_down (from held_up, Δ4)
    assert t.state == sm.CROSSED_DOWN
    s.stay(6996, 600)
    assert t.state == sm.BROKEN_DOWN and t.last_held_side == -1
    assert s.log[-1]["detail"]["event"] == "break"
    s.stay(6985, 31 * 60)          # settles to away_below; memory survives settle
    assert t.state == sm.AWAY_BELOW and t.last_held_side == -1
    s.run([6997, 6999, 7003])
    assert t.state == sm.CROSSED_UP
    s.stay(7004, 600)
    assert t.state == sm.BROKEN_UP and t.last_held_side == 1


def test_failed_down_keeps_support_memory():
    t, s = tracker()
    s.tick(6990)
    cross_up(s)
    s.stay(7004, 600)
    s.stay(7004, 31 * 60)          # age the first cross out of the contest window
    s.run([7001, 6997])
    assert t.state == sm.CROSSED_DOWN
    s.run([6999, 7003])
    assert t.state == sm.FAILED_DOWN and t.last_held_side == 1


def test_settle_after_test_pts_for_hold_min():
    t, s = tracker()
    s.tick(6990)
    cross_up(s)
    s.stay(7004, 600)
    assert t.state == sm.HELD_UP
    s.tick(7008)
    s.stay(7010, 300)
    s.tick(7005)                   # back within TEST → settle timer resets
    assert t.state == sm.HELD_UP and t.settle_sec == 0
    s.tick(7010)
    s.stay(7010, 580)
    assert t.state == sm.HELD_UP
    rs, _ = s.tick(7010)
    assert rs[0]["state"] == sm.AWAY_ABOVE and rs[0]["detail"]["event"] == "settle"
    assert t.last_held_side == 1 and t.last_event == sm.HELD_UP


def test_held_stays_while_near():
    t, s = tracker()
    s.tick(6990)
    cross_up(s)
    s.stay(7004, 600)
    s.stay(7005, 3600)
    assert t.state == sm.HELD_UP


# ---------------------------------------------------------------- contested
def zigzag(s):
    s.tick(6990)
    cross_up(s)                    # cross 1 → crossed_up
    s.run([7001, 6997])            # cross 2 → failed_up
    s.run([6999, 7003])            # cross 3 → contested


def test_zigzag_three_times_is_contested():
    t, s = tracker()
    zigzag(s)
    assert s.states()[-3:] == [sm.CROSSED_UP, sm.FAILED_UP, sm.CONTESTED]
    assert t.state == sm.CONTESTED and s.log[-1]["crosses"] == 3


def test_contested_ignores_knife_and_exits_on_hold():
    t, s = tracker()
    zigzag(s)
    s.tick(6996)                   # Δ7 knife-sized — no effect while contested
    assert t.state == sm.CONTESTED
    s.run([6999, 7003])
    s.stay(7004, 580)
    s.tick(7001)                   # band touch resets the strict exit run
    assert t.state == sm.CONTESTED and t.hold_sec == 0
    s.tick(7004)
    s.stay(7004, 580)
    assert t.state == sm.CONTESTED
    rs, _ = s.tick(7004)
    assert rs[0]["state"] == sm.HELD_UP and rs[0]["detail"]["event"] == "contested_exit"
    assert t.last_held_side == 1
    # old crosses still in window: no immediate re-entry
    assert t.crosses_in_window(s.ts) >= 3
    s.tick(7004)
    assert t.state == sm.HELD_UP
    # a new cross inside the window re-enters contested
    s.run([7001, 6997])
    assert t.state == sm.CONTESTED


def test_crosses_outside_window_do_not_count():
    t, s = tracker()
    s.tick(6990)
    cross_up(s)
    s.run([7001, 6997])            # failed_up (2 crosses)
    s.stay(6985, 31 * 60)
    s.run([6997, 6999, 7003])
    assert t.state == sm.CROSSED_UP and t.crosses_in_window(s.ts) == 1


# ---------------------------------------------------------------- misc
def test_old_or_duplicate_ts_ignored_and_naive_rejected():
    t = sm.LevelTracker(cluster(), CFG)
    t.step(T0, 7010, None)
    assert t.step(T0, 6900, 7010) is None and t.state == sm.AWAY_ABOVE
    assert t.step(T0 - timedelta(seconds=5), 6900, 7010) is None
    with pytest.raises(ValueError):
        t.step(datetime(2026, 11, 20, 15, 0), 7010, 7010)


def test_snapshot_restore_round_trip_is_exact():
    t, s = tracker()
    s.tick(6990)
    zigzag(s)
    s.stay(7004, 200)
    snap = json.loads(json.dumps(t.snapshot()))
    t2 = sm.LevelTracker.restore(cluster(), CFG, snap)
    s2 = Sim(t2, t0=s.ts)
    s2.prev = s.prev
    s.log.clear()
    path = [7004] * 25 + [7001, 6997, 6999, 7003] + [7004] * 40
    s.run(path)
    s2.run(path)
    strip = lambda log: [(x["ts"], x["state"], x["prev_state"], x["crosses"]) for x in log]
    assert strip(s.log) == strip(s2.log) and s.log
    assert t2.snapshot() == t.snapshot()


def test_restore_rejects_other_price():
    t, s = tracker()
    s.run([6990])
    t2 = sm.LevelTracker.restore(cluster(price=7001.0, id="c:7000"), CFG, t.snapshot())
    assert t2.state is None and not t2.initialised


# ---------------------------------------------------------------- LevelMap / Zone
def map4():
    cs = [cluster(6980.0, "c:W", "W"), cluster(7000.0, "c:L", "L"),
          cluster(7020.0, "c:U", "U"), cluster(7040.0, "c:Y", "Y")]
    return sm.LevelMap(list(reversed(cs)), CFG)


def test_zone_kinds():
    m = map4()
    z = m.zone(7010)
    assert (z.kind, z.lower.name, z.upper.name) == ("between", "L", "U")
    z = m.zone(7021)
    assert (z.kind, z.lower.name, z.upper.name) == ("in_band", "U", "U")
    z = m.zone(7022.5)
    assert z.kind == "in_band"                               # |d| == BUFFER is in band
    assert m.zone(7050).kind == "above_all" and m.zone(7050).lower.name == "Y"
    assert m.zone(6970).kind == "below_all" and m.zone(6970).upper.name == "W"
    assert sm.LevelMap([], CFG).zone(7000).kind == "none"
    d = m.zone(7010).to_dict()
    assert d["kind"] == "between" and d["lower"]["id"] == "c:L" and d["upper"]["price"] == 7020.0


def test_map_step_returns_transitions_for_all_levels():
    m = map4()
    s = Sim(m)
    rs, _ = s.tick(7010)
    assert [r["level_id"] for r in rs] == ["c:W", "c:L", "c:U", "c:Y"]
    rs, _ = s.tick(7010)
    assert rs == []


def test_rebuild_keeps_unchanged_trackers():
    m = map4()
    s = Sim(m)
    s.run([6990, 6997, 6999, 7003])
    kept = m.tracker("c:L")
    assert kept.state == sm.CROSSED_UP
    new = [cluster(7000.0, "c:L", "L renamed"), cluster(7020.0, "c:U", "U"),
           cluster(7031.0, "c:Y", "Y"), cluster(7060.0, "c:N", "N")]   # W removed, Y moved, N added
    res = m.rebuild(new)
    assert res == {"kept": ["c:L", "c:U"], "added": ["c:N"], "reset": ["c:Y"],
                   "removed": ["c:W"]}
    assert m.tracker("c:L") is kept and kept.cluster.name == "L renamed"
    assert kept.state == sm.CROSSED_UP and not m.tracker("c:N").initialised
    rs, _ = s.tick(7004)
    inits = {r["level_id"] for r in rs if r["prev_state"] is None}
    assert inits == {"c:N", "c:Y"}
    assert m.tracker("c:L").state == sm.CROSSED_UP and m.tracker("c:L").hold_sec == 20


def test_map_snapshot_restore():
    m = map4()
    s = Sim(m)
    s.run([6990, 6997, 6999, 7003, 7004])
    snap = json.loads(json.dumps(m.snapshot()))
    m2 = sm.LevelMap.restore(m.clusters, CFG, snap)
    assert m2.snapshot() == m.snapshot()


def test_clusters_from_levels_feed_the_map():
    lv = [ResolvedLevel("a", 7000.0, "A", None, "entered", "SPX", ("A",), ("a",)),
          ResolvedLevel("b", 7003.0, "B", None, "entered", "SPX", ("B",), ("b",))]
    m = sm.LevelMap(build_clusters(lv, CFG.cluster_pts), CFG)
    assert len(m.clusters) == 1 and m.clusters[0].price == 7001.5
