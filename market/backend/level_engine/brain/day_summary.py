"""End of day (§13) — the lvl_day_summary row, built at session close.

levels_outcome: one entry per cluster, keyed by cluster id, derived from the
day's level-state transitions (lvl_level_state via ctx.day_memory plus the
brain's own record, deduplicated by (level_id, ts, state)), walked in time
order. The LAST significant event sets the outcome:

  held_up                     → held_support     (crossed up and held: support)
  held_down                   → held_resistance
  failed_up / failed_down     → failed           (a breakout / breakdown that failed)
  broken_up / broken_down     → broken           (knife, or a held level lost)
  contested                   → contested
  crossed_* only (no outcome) → contested        (crossed, never resolved — conservative)
  testing, no event           → held_support / held_resistance by the side price
                                was on at its last test (approached and turned back)
  nothing but away_* / init   → untested

legs: binary counts (§12: win / loss) and hit rate per call type and per
TARGET level name (which level names work as targets); hit_rate = wins /
(wins + losses), None when 0 closed. Old rows with a pre-binary result
('flipped' / 'unresolved') are counted as losses (they never hit the target);
`by_reason` counts close_reason (legacy results map to flipped /
session_close).

Decisions: a cluster seen only in transitions (removed by a mid-day edit) is
still reported, named from the transition detail. `notes` is a one-line plain
summary (no LLM).
"""
from __future__ import annotations

from datetime import datetime

from ..state_machine import (BROKEN_DOWN, BROKEN_UP, CONTESTED, CROSSED_DOWN, CROSSED_UP,
                             FAILED_DOWN, FAILED_UP, HELD_DOWN, HELD_UP, TESTING)

RESULTS = ("win", "loss")
LEGACY_RESULTS = {"flipped": ("loss", "flipped"), "unresolved": ("loss", "session_close")}


def binary_result(result: str | None, close_reason: str | None = None) -> tuple[str | None, str | None]:
    """(result, close_reason) in the binary scheme; legacy results mapped."""
    if result in LEGACY_RESULTS:
        r, why = LEGACY_RESULTS[result]
        return r, close_reason or why
    return result, close_reason


_OUTCOME = {HELD_UP: "held_support", HELD_DOWN: "held_resistance",
            FAILED_UP: "failed", FAILED_DOWN: "failed",
            BROKEN_UP: "broken", BROKEN_DOWN: "broken", CONTESTED: "contested"}


def _ts(v) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def merge_transitions(*sources) -> list[dict]:
    seen, out = set(), []
    for src in sources:
        for r in src or []:
            ts = _ts(r.get("ts"))
            if ts is None:
                continue
            k = (r.get("level_id"), ts.isoformat(), r.get("state"))
            if k in seen:
                continue
            seen.add(k)
            out.append({**r, "ts": ts})
    out.sort(key=lambda r: (r["ts"], str(r.get("level_id"))))
    return out


def levels_outcome(clusters, transitions: list[dict]) -> dict:
    info: dict[str, dict] = {}
    for c in clusters or []:
        info[c.id] = {"name": c.name, "price": c.price, "outcome": "untested",
                      "events": [], "last_state": None, "max_crosses": 0}
    tested_side: dict[str, int] = {}
    for r in transitions:
        lid, st = r.get("level_id"), r.get("state")
        det = r.get("detail") or {}
        e = info.setdefault(lid, {"name": det.get("name"), "price": det.get("price"),
                                  "outcome": "untested", "events": [], "last_state": None,
                                  "max_crosses": 0})
        e["last_state"] = st
        e["max_crosses"] = max(e["max_crosses"], int(r.get("crosses") or 0))
        if st in _OUTCOME:
            e["outcome"] = _OUTCOME[st]
            e["events"].append(st)
        elif st in (CROSSED_UP, CROSSED_DOWN):
            e["events"].append(st)
            e.setdefault("_pending", True)
        elif st == TESTING:
            d = det.get("d")
            if d:
                tested_side[lid] = 1 if float(d) > 0 else -1
    for lid, e in info.items():
        pending = e.pop("_pending", False)
        if e["outcome"] == "untested":
            if pending:
                e["outcome"] = "contested"
            elif lid in tested_side:
                e["outcome"] = "held_support" if tested_side[lid] > 0 else "held_resistance"
    return dict(sorted(info.items(), key=lambda kv: (kv[1]["price"] or 0.0, str(kv[0]))))


def _bucket() -> dict:
    return {"n": 0, **{r: 0 for r in RESULTS}, "open": 0, "hit_rate": None}


def _finish(b: dict) -> dict:
    closed = sum(b[r] for r in RESULTS)
    b["hit_rate"] = round(b["win"] / closed, 4) if closed else None
    return b


def leg_stats(legs) -> dict:
    total, by_call, by_level, by_reason = _bucket(), {}, {}, {}
    for leg in legs:
        res, why = binary_result(leg.result, getattr(leg, "close_reason", None))
        res = res if res in RESULTS else "open"
        for b in (total, by_call.setdefault(leg.call, _bucket()),
                  by_level.setdefault(leg.target_name or "?", _bucket())):
            b["n"] += 1
            b[res] += 1
        if res != "open":
            by_reason[why or "?"] = by_reason.get(why or "?", 0) + 1
    return {"total": _finish(total),
            "by_call": {k: _finish(v) for k, v in sorted(by_call.items())},
            "by_level": {k: _finish(v) for k, v in sorted(by_level.items())},
            "by_reason": dict(sorted(by_reason.items())),
            "legs": [leg.summary() for leg in legs]}


def build_day_summary(ctx, legs, transitions: list[dict]) -> dict:
    lo = levels_outcome(ctx.clusters, transitions)
    stats = leg_stats(legs)
    t = stats["total"]
    counts: dict[str, int] = {}
    for e in lo.values():
        counts[e["outcome"]] = counts.get(e["outcome"], 0) + 1
    notes = (f"{t['n']} legs: {t['win']} win, {t['loss']} loss"
             + (f", {t['open']} open" if t["open"] else "")
             + (f" (hit rate {t['hit_rate'] * 100:.0f}%)" if t["hit_rate"] is not None else "")
             + "; levels: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    return {"trade_date": ctx.trade_date, "close": float(ctx.spot),
            "final_zone": ctx.scenario.zone.to_dict(), "levels_outcome": lo,
            "legs": stats, "notes": notes}
