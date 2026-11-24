"""Scenario mapping (§7.2). Pure function of a stepped LevelMap and spot.

Call `map_scenario(level_map, spot)` AFTER `level_map.step(...)` for the tick.

The §7.2 table is phrased relative to the level that had the event, as seen
when it happened ("upper → crossed_up"). After that event spot is on the far
side, so the same cluster is now the zone's `lower` (or in-band). The mapping is
therefore implemented by EVENT DIRECTION on the event-bearing level E, which is
position-independent and reproduces every table row:

  E state                               lean   up target        down target
  crossed_up / held_up / broken_up      up     next above E     E
  failed_down (support held)            up     next above E     E
  crossed_down / held_down / broken_down down  E                next below E
  failed_up (resistance held)           down   E                next below E
  contested (either bracketing level)   range  zone upper       zone lower
  no event on a bracketing level        none   zone upper       zone lower
  above_all / below_all                 done   None             None

(Check: "upper → failed_up | upper | lower | down": E=upper, next below upper =
lower. "lower → failed_down | upper | lower | up": E=lower, next above = upper.)
The state machine keeps state consistent with spot's side (any side change
leaves an event state via cross/fail/knife), so E is always a bracketing level
on the correct side for its event.

Decisions (spec ambiguities, resolved conservatively)
-----------------------------------------------------
* Bracketing levels = zone.lower and zone.upper (one cluster when in_band).
  Events on non-bracketing levels are ignored.
* In-band zone (lower == upper == X) with no directional event (none or
  contested): targets are X's NEIGHBOURS (next above X / next below X) — a
  target equal to the level spot sits on is meaningless (§9: "range between
  neighbours").
* Several events: contested on either always wins (range). Otherwise the most
  recent event wins, by the tracker's `since` (time its current state was
  entered); same tick → larger |spot − level| wins; still tied with opposite
  leans → lean 'none' with the zone's default targets (no call on a coin flip).
* `pending` is True only when the winning event is crossed_* (hold pending).
* Invalidation: invalidation_up (level below which an up thesis is wrong) =
  down_target; invalidation_down = up_target. Set for every lean except done.
* above_all / below_all: lean 'done', targets and invalidations None;
  reference_level = the extreme level passed (its event state, if any, is
  reported in reference_event).
* No clusters: lean 'none', everything None.
* A tracker that has not stepped yet (new cluster) carries no event.
"""
from __future__ import annotations

from dataclasses import dataclass

from .levels import Cluster
from .state_machine import (BROKEN_DOWN, BROKEN_UP, CONTESTED, CROSSED_DOWN, CROSSED_STATES,
                            CROSSED_UP, EVENT_STATES, FAILED_DOWN, FAILED_UP, HELD_DOWN, HELD_UP,
                            LevelMap, Zone)

UP_EVENTS = (CROSSED_UP, HELD_UP, BROKEN_UP, FAILED_DOWN)
DOWN_EVENTS = (CROSSED_DOWN, HELD_DOWN, BROKEN_DOWN, FAILED_UP)


def _c(x: Cluster | None) -> dict | None:
    return None if x is None else {"id": x.id, "price": x.price, "name": x.name, "strike": x.strike}


@dataclass
class Scenario:
    zone: Zone
    up_target: Cluster | None
    down_target: Cluster | None
    lean: str                       # 'up' | 'down' | 'range' | 'none' | 'done'
    reference_level: Cluster | None
    reference_event: str | None
    pending: bool
    invalidation_up: Cluster | None
    invalidation_down: Cluster | None

    def to_dict(self) -> dict:
        return {"zone": self.zone.to_dict(), "lean": self.lean,
                "up_target": _c(self.up_target), "down_target": _c(self.down_target),
                "reference_level": _c(self.reference_level),
                "reference_event": self.reference_event, "pending": self.pending,
                "invalidation_up": _c(self.invalidation_up),
                "invalidation_down": _c(self.invalidation_down)}


def _lean_of(state: str) -> str:
    return "up" if state in UP_EVENTS else "down"


def map_scenario(level_map: LevelMap, spot: float) -> Scenario:
    zone = level_map.zone(spot)
    if zone.kind == "none":
        return Scenario(zone, None, None, "none", None, None, False, None, None)
    if zone.kind in ("above_all", "below_all"):
        ref = zone.lower if zone.kind == "above_all" else zone.upper
        t = level_map.tracker(ref.id)
        ev = t.state if t is not None and t.state in EVENT_STATES else None
        return Scenario(zone, None, None, "done", ref, ev, False, None, None)

    bracket = [zone.lower] if zone.kind == "in_band" else [zone.lower, zone.upper]
    if zone.kind == "in_band":
        x = zone.lower
        dflt_up, dflt_down = level_map.next_above(x), level_map.next_below(x)
    else:
        dflt_up, dflt_down = zone.upper, zone.lower

    events = []
    for c in bracket:
        t = level_map.tracker(c.id)
        if t is not None and t.state in EVENT_STATES and t.since is not None:
            events.append((c, t))

    def key(ct):
        c, t = ct
        return (t.since, abs(spot - c.price))

    def build(up, down, lean, ref, ev, pending):
        return Scenario(zone, up, down, lean, ref, ev, pending,
                        invalidation_up=down, invalidation_down=up)

    contested = [ct for ct in events if ct[1].state == CONTESTED]
    if contested:
        c, _ = max(contested, key=key)
        return build(dflt_up, dflt_down, "range", c, CONTESTED, False)
    if not events:
        return build(dflt_up, dflt_down, "none", None, None, False)

    ranked = sorted(events, key=key, reverse=True)
    e, t = ranked[0]
    lean = _lean_of(t.state)
    if len(ranked) > 1:
        e2, t2 = ranked[1]
        if key(ranked[1]) == key(ranked[0]) and _lean_of(t2.state) != lean:
            return build(dflt_up, dflt_down, "none", None, None, False)
    if lean == "up":
        up, down = level_map.next_above(e), e
    else:
        up, down = e, level_map.next_below(e)
    return build(up, down, lean, e, t.state, t.state in CROSSED_STATES)
