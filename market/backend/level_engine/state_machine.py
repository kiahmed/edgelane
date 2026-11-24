"""Per-level state machine (§7.1) + LevelMap + Zone. Pure, stdlib only.

Each tracker follows one Cluster. `step(ts, spot, prev_spot)` applies the §7.1
rules IN ORDER (first match wins) and returns a transition dict when the state
changes (else None). Timers accumulate by actual timestamp deltas, so a replay
over the same ticks is exact.

    d = spot − L;  side(d) = +1 if d > BUFFER, −1 if d < −BUFFER, else 0.

Rule order as implemented (per tick, after crediting timers — see below):
  1. contested — a new cross this tick makes crosses-in-window >= CONTEST_CROSSES
     → contested. While contested: exit only when side stays ±1 continuously for
     HOLD_MIN → held_up / held_down (by side). Knife / fail / cross / settle do
     not apply while contested.
  2. knife     — a side cross this tick with |spot − prev_spot| > KNIFE_PTS and
     sign(d) flipped between prev and now → broken_up / broken_down, from any
     non-contested state; clears last_held_side.
  5. fail      — in crossed_up and side becomes −1 → failed_up (mirror).
  3. cross     — side changes to ±1 from the other nonzero side → crossed_*;
     hold timer starts at 0.
  4/6. hold    — in crossed_*: timer >= HOLD_MIN*60 (and side currently on the
     crossed side) → held_*; or broken_* when last_held_side is the opposite side
     (rule 6). last_held_side := crossed side.
  7. settle    — held_* / failed_* / broken_*: |d| > TEST_PTS continuously for
     HOLD_MIN → away_* (|d| > TEST so it is always away_*, never testing).
  8. baseline  — away_above (d > TEST) / away_below (d < −TEST) / testing,
     only from away_* / testing.

Decisions (spec ambiguities, resolved conservatively)
-----------------------------------------------------
* FAIL BEFORE CROSS. From crossed_up, side going to −1 is also a side cross; if
  rule 3 ran before rule 5, failed_* could never happen. Fail therefore pre-empts
  cross while in crossed_* (rule 4 text: "side going to the origin side →
  failed_*"). The knife comes before fail EXCEPT for a crossed_* level moving
  back to its origin side: with defaults (KNIFE=5, BUFFER=2.5) any one-tick
  jump from side +1 straight to −1 is > 5 pts, so without this exemption a
  reclaim that fails within one tick would be recorded as broken_* (a strong
  signal the other way) instead of failed_*. Knives through away/testing/held
  levels are unaffected.
* CROSS = change of the last NONZERO side (+1 ↔ −1), possibly via the band.
  Side memory starts from the first tick's side; a level that starts inside the
  band has no side memory, so its first exit is NOT a cross (origin unknown).
* Crosses are counted for contested in every state (incl. while contested and
  knives — a knife is always also a side cross by definition below). Contested
  is ENTERED only on the tick of a new cross (so exiting contested after a hold
  does not immediately re-enter while old crosses are still in the window).
  Window is inclusive: a cross at exactly ts − CONTEST_WINDOW_MIN counts.
* KNIFE requires: (a) a side cross this tick, (b) |Δspot| > KNIFE_PTS,
  (c) sign(d_prev) and sign(d_now) both nonzero and opposite, (d) the tick
  interval <= 2*TICK_SEC (a move across a feed gap was not observed as "one
  tick" → treated as an ordinary cross). prev_spot None → the tracker's own last
  spot is used.
* Knife clears last_held_side (spec). A rule-6 break or contested-exit hold SETS
  last_held_side to the new side (the level flips role: lost support becomes
  resistance), so a later reversal-and-hold is again a break.
* Contested exit is literal §7.1: held_* (not broken_*) even if last_held_side
  was opposite; scenario mapping treats held_* and broken_* identically. Its
  continuity is strict: any tick in the band or on the other side resets it
  (GRACE applies only to rule 4).
* TIMER ACCOUNTING: each interval (prev tick → this tick) is credited to the
  side at the START of the interval (the previous tick's side). Credited dt is
  capped at 2*TICK_SEC so a feed outage cannot complete a hold/settle unseen.
  Hold timer (crossed_*): dt on the crossed side → +dt; dt in band → band run
  += dt, and once the band run > GRACE_SEC the timer resets to 0 (stays 0 while
  in band); returning to the crossed side ends the band run. A held_* is only
  declared on a tick whose side is the crossed side (not from inside the band).
* Settle timer: credited when |d| > TEST at interval start; reset whenever
  |d| <= TEST at a tick.
* All timers (hold_sec, band_sec, settle_sec) reset to 0 on every state change
  (the transition's `detail` records the values reached just before it).
  hold_sec is meaningful only in crossed_* (hold) and contested (exit run).
* Ticks with ts <= the tracker's last ts are ignored (return None). Naive
  datetimes raise ValueError.
* The first step of a tracker (new, or re-created) emits a transition with
  prev_state None and event 'init' so lvl_level_state has a starting row.
* last_event / last_event_ts = most recent DIRECTIONAL event state (crossed_*,
  held_*, failed_*, broken_*) and when it was entered; survives settle.
* LevelMap.rebuild keeps a tracker only if the cluster id is unchanged AND the
  cluster price is unchanged (|Δ| < 1e-6). Same id with a new price (level edit,
  provisional→locked ratio) → fresh tracker (re-initialised on its next step):
  state computed against the old price would be wrong. Name/kind-only changes
  keep the tracker and swap in the new Cluster.
* Zone in-band: the cluster with |spot − price| <= BUFFER; if two qualify (only
  possible at exactly 2*BUFFER spacing) the smaller |d| wins, tie → lower price.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import LevelConfig
from .levels import Cluster

AWAY_ABOVE = "away_above"
AWAY_BELOW = "away_below"
TESTING = "testing"
CROSSED_UP = "crossed_up"
CROSSED_DOWN = "crossed_down"
HELD_UP = "held_up"
HELD_DOWN = "held_down"
FAILED_UP = "failed_up"
FAILED_DOWN = "failed_down"
BROKEN_UP = "broken_up"
BROKEN_DOWN = "broken_down"
CONTESTED = "contested"

STATES = (AWAY_ABOVE, AWAY_BELOW, TESTING, CROSSED_UP, CROSSED_DOWN, HELD_UP, HELD_DOWN,
          FAILED_UP, FAILED_DOWN, BROKEN_UP, BROKEN_DOWN, CONTESTED)
BASELINE_STATES = (AWAY_ABOVE, AWAY_BELOW, TESTING)
CROSSED_STATES = (CROSSED_UP, CROSSED_DOWN)
SETTLE_STATES = (HELD_UP, HELD_DOWN, FAILED_UP, FAILED_DOWN, BROKEN_UP, BROKEN_DOWN)
DIRECTIONAL_EVENTS = CROSSED_STATES + SETTLE_STATES
EVENT_STATES = DIRECTIONAL_EVENTS + (CONTESTED,)

_CROSSED = {1: CROSSED_UP, -1: CROSSED_DOWN}
_HELD = {1: HELD_UP, -1: HELD_DOWN}
_FAILED = {1: FAILED_UP, -1: FAILED_DOWN}
_BROKEN = {1: BROKEN_UP, -1: BROKEN_DOWN}

KNIFE_MAX_DT_TICKS = 2.0     # knife only within one observed tick interval
MAX_CREDIT_TICKS = 2.0       # cap on seconds credited per interval
PRICE_EPS = 1e-6


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("ts must be timezone-aware")
    return ts


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class LevelTracker:
    def __init__(self, cluster: Cluster, cfg: LevelConfig) -> None:
        self.cluster = cluster
        self.cfg = cfg
        self.state: str | None = None
        self.since: datetime | None = None
        self.d: float | None = None
        self.side: int = 0
        self.last_held_side: int | None = None
        self.hold_sec: float = 0.0
        self.band_sec: float = 0.0
        self.settle_sec: float = 0.0
        self.crossed_dir: int = 0
        self.last_event: str | None = None
        self.last_event_ts: datetime | None = None
        self._last_nz: int = 0
        self._last_ts: datetime | None = None
        self._last_spot: float | None = None
        self._cross_times: list[datetime] = []

    # ---- helpers ----
    @property
    def level_id(self) -> str:
        return self.cluster.id

    @property
    def initialised(self) -> bool:
        return self._last_ts is not None

    def _side_of(self, d: float) -> int:
        b = self.cfg.buffer_pts
        return 1 if d > b else (-1 if d < -b else 0)

    def _baseline(self, d: float) -> str:
        t = self.cfg.test_pts
        return AWAY_ABOVE if d > t else (AWAY_BELOW if d < -t else TESTING)

    def crosses_in_window(self, ts: datetime) -> int:
        w = self.cfg.contest_window_min * 60.0
        return sum(1 for t in self._cross_times if 0 <= (ts - t).total_seconds() <= w)

    def _prune(self, ts: datetime) -> None:
        w = self.cfg.contest_window_min * 60.0
        self._cross_times = [t for t in self._cross_times if (ts - t).total_seconds() <= w]

    def _transition(self, ts, prev_state, new_state, event, spot) -> dict:
        timers = {"hold_sec": self.hold_sec, "band_sec": self.band_sec,
                  "settle_sec": self.settle_sec}        # values reached, pre-reset
        self.state = new_state
        self.since = ts
        self.hold_sec = self.band_sec = self.settle_sec = 0.0
        if new_state not in CROSSED_STATES:
            self.crossed_dir = 0
        if new_state in DIRECTIONAL_EVENTS:
            self.last_event, self.last_event_ts = new_state, ts
        return {
            "level_id": self.cluster.id,
            "ts": ts,
            "state": new_state,
            "prev_state": prev_state,
            "since": ts,
            "crosses": self.crosses_in_window(ts),
            "last_held_side": self.last_held_side,
            "detail": {"event": event, "d": round(self.d, 4), "side": self.side,
                       **timers, "spot": spot,
                       "price": self.cluster.price, "name": self.cluster.name},
        }

    # ---- the step ----
    def step(self, ts: datetime, spot: float, prev_spot: float | None) -> dict | None:
        _aware(ts)
        cfg = self.cfg
        L = self.cluster.price
        d = spot - L
        side = self._side_of(d)

        if self._last_ts is None:                      # first tick: init, no cross
            self.d, self.side = d, side
            self._last_nz = side
            self._last_ts, self._last_spot = ts, spot
            return self._transition(ts, None, self._baseline(d), "init", spot)
        if ts <= self._last_ts:
            return None

        raw_dt = (ts - self._last_ts).total_seconds()
        dt = min(raw_dt, MAX_CREDIT_TICKS * cfg.tick_sec)
        old_side, old_d = self.side, self.d
        ref_prev = prev_spot if prev_spot is not None else self._last_spot
        st = self.state
        hold_s = cfg.hold_min * 60.0

        # -- credit the interval to the side at its START --
        if st in CROSSED_STATES:
            if old_side == self.crossed_dir:
                self.hold_sec += dt
            elif old_side == 0:
                self.band_sec += dt
                if self.band_sec > cfg.grace_sec:
                    self.hold_sec = 0.0
        elif st == CONTESTED:
            if old_side != 0:
                self.hold_sec += dt
        elif st in SETTLE_STATES:
            if abs(old_d) > cfg.test_pts:
                self.settle_sec += dt

        # -- side memory / cross detection --
        cross_dir = 0
        if side != 0:
            if self._last_nz != 0 and side != self._last_nz:
                cross_dir = side
                self._cross_times.append(ts)
            self._last_nz = side
        self._prune(ts)

        knife = (cross_dir != 0 and ref_prev is not None
                 and raw_dt <= KNIFE_MAX_DT_TICKS * cfg.tick_sec
                 and abs(spot - ref_prev) > cfg.knife_pts
                 and _sign(ref_prev - L) == -cross_dir and _sign(d) == cross_dir)

        self.d, self.side = d, side
        self._last_ts, self._last_spot = ts, spot

        # Rule 1 — contested
        if cross_dir and st != CONTESTED and self.crosses_in_window(ts) >= cfg.contest_crosses:
            return self._transition(ts, st, CONTESTED, "contested", spot)
        if st == CONTESTED:
            if side == 0 or side != old_side:
                self.hold_sec = 0.0
            if side != 0 and self.hold_sec >= hold_s:
                self.last_held_side = side
                return self._transition(ts, st, _HELD[side], "contested_exit", spot)
            return None
        # Rule 2 — knife. NOT for a crossed_* level snapping back to its origin
        # side: that is a failed reclaim (rule 5), however big the tick — else a
        # one-tick fail (> 2×BUFFER by construction) would read as broken_*.
        if knife and not (st in CROSSED_STATES and cross_dir == -self.crossed_dir):
            self.last_held_side = None
            return self._transition(ts, st, _BROKEN[cross_dir], "knife", spot)
        # Rule 5 — fail (pre-empts rule 3 while crossed_*)
        if st in CROSSED_STATES and side == -self.crossed_dir:
            return self._transition(ts, st, _FAILED[self.crossed_dir], "fail", spot)
        # Rule 3 — cross
        if cross_dir:
            tr = self._transition(ts, st, _CROSSED[cross_dir], "cross", spot)
            self.crossed_dir = cross_dir
            return tr
        # Rule 4 / 6 — hold timer
        if st in CROSSED_STATES:
            if side == self.crossed_dir:
                self.band_sec = 0.0
                if self.hold_sec >= hold_s:
                    cd = self.crossed_dir
                    if self.last_held_side == -cd:
                        new, ev = _BROKEN[cd], "break"
                    else:
                        new, ev = _HELD[cd], "hold"
                    self.last_held_side = cd
                    return self._transition(ts, st, new, ev, spot)
            return None
        # Rule 7 — settle
        if st in SETTLE_STATES:
            if abs(d) <= cfg.test_pts:
                self.settle_sec = 0.0
            elif self.settle_sec >= hold_s:
                return self._transition(ts, st, self._baseline(d), "settle", spot)
            return None
        # Rule 8 — baseline
        b = self._baseline(d)
        if b != st:
            return self._transition(ts, st, b, "baseline", spot)
        return None

    # ---- persistence ----
    def snapshot(self) -> dict:
        return {
            "cluster_id": self.cluster.id, "price": self.cluster.price,
            "state": self.state, "since": _iso(self.since), "d": self.d, "side": self.side,
            "last_held_side": self.last_held_side, "hold_sec": self.hold_sec,
            "band_sec": self.band_sec, "settle_sec": self.settle_sec,
            "crossed_dir": self.crossed_dir, "last_event": self.last_event,
            "last_event_ts": _iso(self.last_event_ts), "last_nz": self._last_nz,
            "last_ts": _iso(self._last_ts), "last_spot": self._last_spot,
            "cross_times": [_iso(t) for t in self._cross_times],
        }

    @classmethod
    def restore(cls, cluster: Cluster, cfg: LevelConfig, snap: dict) -> "LevelTracker":
        t = cls(cluster, cfg)
        if snap.get("cluster_id") != cluster.id or abs(float(snap.get("price", 0)) - cluster.price) > PRICE_EPS:
            return t                    # different level → fresh tracker
        t.state = snap.get("state")
        t.since = _dt(snap.get("since"))
        t.d = snap.get("d")
        t.side = int(snap.get("side") or 0)
        lhs = snap.get("last_held_side")
        t.last_held_side = int(lhs) if lhs is not None else None
        t.hold_sec = float(snap.get("hold_sec") or 0.0)
        t.band_sec = float(snap.get("band_sec") or 0.0)
        t.settle_sec = float(snap.get("settle_sec") or 0.0)
        t.crossed_dir = int(snap.get("crossed_dir") or 0)
        t.last_event = snap.get("last_event")
        t.last_event_ts = _dt(snap.get("last_event_ts"))
        t._last_nz = int(snap.get("last_nz") or 0)
        t._last_ts = _dt(snap.get("last_ts"))
        t._last_spot = snap.get("last_spot")
        t._cross_times = [_dt(x) for x in snap.get("cross_times") or []]
        return t


@dataclass
class Zone:
    lower: Cluster | None
    upper: Cluster | None
    kind: str          # 'between' | 'in_band' | 'above_all' | 'below_all' | 'none'

    def to_dict(self) -> dict:
        def c(x):
            return None if x is None else {"id": x.id, "price": x.price, "name": x.name,
                                           "strike": x.strike}
        return {"kind": self.kind, "lower": c(self.lower), "upper": c(self.upper)}


class LevelMap:
    def __init__(self, clusters: list[Cluster], cfg: LevelConfig) -> None:
        self.cfg = cfg
        self.clusters: list[Cluster] = sorted(clusters, key=lambda c: (c.price, c.id))
        self.trackers: dict[str, LevelTracker] = {c.id: LevelTracker(c, cfg) for c in self.clusters}

    def step(self, ts: datetime, spot: float, prev_spot: float | None) -> list[dict]:
        _aware(ts)
        out = []
        for c in self.clusters:
            tr = self.trackers[c.id].step(ts, spot, prev_spot)
            if tr is not None:
                out.append(tr)
        return out

    def rebuild(self, clusters: list[Cluster]) -> dict:
        """Swap in a new cluster set (§9 levels edited mid-session)."""
        new_sorted = sorted(clusters, key=lambda c: (c.price, c.id))
        kept, added, reset = [], [], []
        trackers: dict[str, LevelTracker] = {}
        for c in new_sorted:
            old = self.trackers.get(c.id)
            if old is not None and abs(old.cluster.price - c.price) < PRICE_EPS:
                old.cluster = c
                trackers[c.id] = old
                kept.append(c.id)
            else:
                trackers[c.id] = LevelTracker(c, self.cfg)
                (reset if old is not None else added).append(c.id)
        removed = [i for i in self.trackers if i not in trackers]
        self.clusters, self.trackers = new_sorted, trackers
        return {"kept": kept, "added": added, "reset": reset, "removed": removed}

    def tracker(self, cluster_id: str) -> LevelTracker | None:
        return self.trackers.get(cluster_id)

    def next_above(self, c: Cluster | None) -> Cluster | None:
        if c is None:
            return None
        return next((x for x in self.clusters if x.price > c.price), None)

    def next_below(self, c: Cluster | None) -> Cluster | None:
        if c is None:
            return None
        return next((x for x in reversed(self.clusters) if x.price < c.price), None)

    def zone(self, spot: float) -> Zone:
        if not self.clusters:
            return Zone(None, None, "none")
        b = self.cfg.buffer_pts
        inband = [c for c in self.clusters if abs(spot - c.price) <= b]
        if inband:
            c = min(inband, key=lambda x: (abs(spot - x.price), x.price))
            return Zone(c, c, "in_band")
        lower = next((x for x in reversed(self.clusters) if x.price < spot), None)
        upper = next((x for x in self.clusters if x.price > spot), None)
        if upper is None:
            return Zone(lower, None, "above_all")
        if lower is None:
            return Zone(None, upper, "below_all")
        return Zone(lower, upper, "between")

    def snapshot(self) -> dict:
        return {"trackers": {i: t.snapshot() for i, t in self.trackers.items()}}

    @classmethod
    def restore(cls, clusters: list[Cluster], cfg: LevelConfig, snap: dict) -> "LevelMap":
        m = cls(clusters, cfg)
        saved = (snap or {}).get("trackers") or {}
        for c in m.clusters:
            if c.id in saved:
                m.trackers[c.id] = LevelTracker.restore(c, cfg, saved[c.id])
        return m
