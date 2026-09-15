"""Which Matrix transitions just happened — and publishing them, once each.

`matrix_events.py` is the transport (mirrors `simmer_events.py`); this module is
the *decision* layer above it: it remembers what was last published per symbol
and fires only on a genuine change. Both entry points are best-effort and NEVER
raise — an events outage must not stall a poll or the grading sweep, which is the
same posture `emailer.py` and `simmer_events.py` hold.

Two entry points, matching where each signal's truth already lives
(docs/matrix_events_update.md §2). Deliberately NOT a separate watcher loop: a
third clock would re-derive the same state a moment later and drift out of sync
with the logic that owns it.

    on_snapshot(...)    called from the poller after a persisted poll
                        → pick_selected, session_open, grid_digest
    on_evaluation(...)  called at the end of the evaluator sweep
                        → bias_aligned / bias_diverged, win_rate_notable, daily_recap

The six states and their sources are the table in §2 of that doc. Nothing here
re-computes an opinion: it reads what the engine and the grader already decided
and asks only "is this different from what we last published?".
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import matrix_events

log = logging.getLogger("edgelane.matrix.signals")

_SESSION_TZ = ZoneInfo("America/New_York")      # same trading day boundary as evaluator.py

# A digest targets ~2x/week, so it needs BOTH a real change and a cooldown —
# cadence alone would post a grid nobody's looking at, change alone could post
# several times a day on a choppy session.
_DIGEST_MIN_HOURS = 60.0          # ~2.5 days
_DIGEST_MIN_CHANGED = 3           # of the 8 strategy cards


class MatrixSignalState:
    """Last-published markers, per symbol. In-memory by design: these gate
    *publishing*, not correctness, so a restart re-firing one chip is harmless
    (the deterministic event_id makes it a downstream no-op within the same UTC
    day). Mirrors how `EvaluatorState` holds the regime counters."""

    def __init__(self) -> None:
        self.last_pick_key: dict[str, str] = {}
        self.last_trust_state: dict[str, str] = {}
        self.last_win_tier: dict[str, str] = {}
        self.last_regime_alert: dict[str, bool] = {}
        self.session_open_date: dict[str, str] = {}     # ET date already announced
        self.last_digest_at: dict[str, str] = {}        # UTC iso
        self.last_digest_grid: dict[str, str] = {}      # grid signature
        self.last_recap_date: dict[str, str] = {}       # ET date already recapped
        # Rolling best/worst composite pick seen today, for daily_recap. Kept in
        # memory because bias_decisions persists the pick but not its composite
        # score — tracking the extremes as they stream past avoids a schema change.
        self.day_date: dict[str, str] = {}
        self.day_best: dict[str, dict] = {}
        self.day_worst: dict[str, dict] = {}

    def reset(self) -> None:
        for d in (self.last_pick_key, self.last_trust_state, self.last_win_tier,
                  self.last_regime_alert, self.session_open_date, self.last_digest_at,
                  self.last_digest_grid, self.last_recap_date, self.day_date,
                  self.day_best, self.day_worst):
            d.clear()


state = MatrixSignalState()


# Publishing is handed to the BACKGROUND; the poll only decides whether there is
# something to say. `publish_transition` waits on a Pub/Sub ack (up to 10s), and
# the poll path is the wrong place to wait for that: `_poll_all` awaits each
# symbol in turn and the UI only sees a snapshot once poll_symbol returns, so a
# degraded topic would stretch the poll cycle and stall the page behind it.
#
# Safe to fire and forget because the last-published markers above are updated
# from the DECISION, not from the publish result — a dropped message can't cause
# a re-fire storm, and the deterministic event_id dedupes any retry downstream.
_INFLIGHT: set[asyncio.Task] = set()


def _fire(symbol: str, state_name: str, expiry: Any = None,
          attrs: dict[str, str] | None = None) -> str:
    """Queue one publish and return immediately. Returns the state name, so
    callers report what was HANDED OFF — not what was confirmed sent."""
    async def _run() -> None:
        try:
            await matrix_events.publish_transition(
                symbol, state_name, expiry, extra_attributes=attrs)
        except Exception:                      # publish_transition swallows its own,
            log.exception("[matrix-signals] publish task failed")   # this is belt-and-braces
    task = asyncio.create_task(_run())
    _INFLIGHT.add(task)                        # hold a ref; asyncio only weakrefs tasks
    task.add_done_callback(_INFLIGHT.discard)
    return state_name


async def drain(timeout: float = 10.0) -> None:
    """Await any in-flight publishes. For tests and orderly shutdown — never
    called on the poll path, which is the whole point."""
    pending = set(_INFLIGHT)
    if pending:
        await asyncio.wait(pending, timeout=timeout)


def _et_date() -> str:
    return datetime.now(_SESSION_TZ).date().isoformat()


def _pick_key(pick: dict) -> str:
    """Identity of an engine pick: the structure a reader would recognize as
    'the same call'. Composite score drifts every poll, so it is deliberately
    NOT part of the key — otherwise every poll would look like a new pick."""
    strikes = pick.get("strikes")
    if isinstance(strikes, (list, tuple)):
        strikes = "/".join(str(s) for s in strikes)
    return "|".join(str(x) for x in (
        pick.get("strategy") or "", pick.get("label") or "", strikes or "",
    ))


def _pick_summary(pick: dict) -> dict[str, str]:
    """Compact attributes carried alongside a pick chip."""
    verdict = pick.get("composite_verdict") or {}
    return {
        "strategy": str(pick.get("strategy") or ""),
        "label": str(pick.get("label") or ""),
        "composite_score": str(pick.get("composite_score") if pick.get("composite_score") is not None else ""),
        "verdict": str(verdict.get("label") or "") if isinstance(verdict, dict) else "",
        "structure": str(pick.get("structure_text") or ""),
    }


def _grid_signature(strategies: dict) -> str:
    """What the 8-card grid is 'saying' right now — each strategy's best label +
    health + verdict. Excludes prices so a penny of drift is not a change."""
    parts: list[str] = []
    for key in sorted(strategies or {}):
        best = (strategies.get(key) or {}).get("best") or {}
        if not best:
            parts.append(f"{key}:-")
            continue
        verdict = best.get("composite_verdict") or {}
        parts.append("{}:{}:{}:{}".format(
            key, best.get("label") or "", best.get("health") or "",
            (verdict.get("label") or "") if isinstance(verdict, dict) else ""))
    return "|".join(parts)


def _changed_cards(a: str, b: str) -> int:
    """How many of the 8 cards differ between two grid signatures."""
    if not a or not b:
        return _DIGEST_MIN_CHANGED          # no baseline ⇒ treat as worth sending
    pa, pb = a.split("|"), b.split("|")
    if len(pa) != len(pb):
        return _DIGEST_MIN_CHANGED
    return sum(1 for x, y in zip(pa, pb) if x != y)


def _walls_worth_showing(bias: dict) -> bool:
    """A session_open chip needs at least one real wall to talk about; §2 says
    skip silently rather than post an empty frame."""
    return any(bias.get(k) is not None for k in
               ("call_wall_strike", "put_wall_strike", "vex_wall_strike", "tex_wall_strike"))


def _track_day_extremes(sym: str, pick: dict, today: str) -> None:
    """Remember the day's best and worst composite pick for the recap."""
    if state.day_date.get(sym) != today:
        state.day_date[sym] = today
        state.day_best.pop(sym, None)
        state.day_worst.pop(sym, None)
    score = pick.get("composite_score")
    try:
        score = float(score)
    except (TypeError, ValueError):
        return
    row = dict(_pick_summary(pick), composite_score=str(score))
    best, worst = state.day_best.get(sym), state.day_worst.get(sym)
    if best is None or score > float(best.get("composite_score") or -1e9):
        state.day_best[sym] = row
    if worst is None or score < float(worst.get("composite_score") or 1e9):
        state.day_worst[sym] = row


async def on_snapshot(snap: dict, settings: Any = None) -> list[str]:
    """Poller-side transitions. Returns the states published (for tests/logs).

    Never raises: a failure here must not cost the poll its snapshot."""
    published: list[str] = []
    try:
        sym = str(snap.get("symbol") or "").upper()
        if not sym:
            return published
        expiry = snap.get("expiration")
        pick = snap.get("engine_pick") or {}
        bias = snap.get("bias") or {}
        today = _et_date()

        # 1. pick_selected — the engine's top call changed.
        if pick:
            _track_day_extremes(sym, pick, today)
            key = _pick_key(pick)
            if key and state.last_pick_key.get(sym) != key:
                state.last_pick_key[sym] = key
                published.append(_fire(sym, "pick_selected", expiry, _pick_summary(pick)))

        # 2. session_open — first persisted poll of a new ET day that has walls
        #    worth a chip.
        if state.session_open_date.get(sym) != today and _walls_worth_showing(bias):
            state.session_open_date[sym] = today
            attrs = {k: str(bias.get(k)) for k in
                     ("call_wall_strike", "put_wall_strike", "vex_wall_strike", "tex_wall_strike")
                     if bias.get(k) is not None}
            published.append(_fire(sym, "session_open", expiry, attrs))

        # 3. grid_digest — enough of the grid moved, and it has been long enough.
        sig = _grid_signature(snap.get("strategies") or {})
        if sig:
            last_at = state.last_digest_at.get(sym)
            due = True
            if last_at:
                try:
                    age_h = (datetime.now(timezone.utc)
                             - datetime.fromisoformat(last_at)).total_seconds() / 3600.0
                    due = age_h >= _DIGEST_MIN_HOURS
                except (TypeError, ValueError):
                    due = True
            if due and _changed_cards(state.last_digest_grid.get(sym, ""), sig) >= _DIGEST_MIN_CHANGED:
                state.last_digest_at[sym] = datetime.now(timezone.utc).isoformat()
                state.last_digest_grid[sym] = sig
                published.append(_fire(sym, "grid_digest", expiry))
    except Exception:
        log.exception("[matrix-signals] on_snapshot failed (ignored)")
    return published


async def on_evaluation(db: Any, poller_state: Any, settings: Any) -> list[str]:
    """Evaluator-side transitions, called at the end of the existing sweep.

    The sweep is the one place that already holds the before/after of the regime
    counters, so it can tell "did this just change" without re-deriving it
    (docs/matrix_events_update.md §3). Never raises."""
    from .evaluator import state as evaluator_state
    from .routes.accuracy import _trust_state, _tier

    published: list[str] = []
    try:
        symbols = list(getattr(poller_state, "latest_by_symbol", {}) or {})
        today = _et_date()
        min_graded = int(getattr(settings, "eval_min_graded", 10))
        green = float(getattr(settings, "pill_green_pct", 60.0))
        red = float(getattr(settings, "pill_red_pct", 40.0))
        window = int(getattr(settings, "eval_rolling_window", 20))

        for sym in symbols:
            snap = (getattr(poller_state, "latest_by_symbol", {}) or {}).get(sym) or {}
            expiry = snap.get("expiration")
            try:
                stats = db.fetch_accuracy(sym, window)
            except Exception:
                log.exception("[matrix-signals] fetch_accuracy failed for %s", sym)
                continue
            graded = int(stats.get("n") or 0)
            pct = float(stats.get("accuracy_pct") or 0.0)
            trust = _trust_state(sym, graded, pct, settings)

            # 4/5. bias_aligned | bias_diverged — the trust relationship flipped.
            #      in_sync is "aligned"; everything else (low_conf, calibrating,
            #      paused) is "diverged". Fires on the transition, not per poll.
            cur_state = str(trust.get("state") or "")
            prev_state = state.last_trust_state.get(sym)
            if prev_state is not None and cur_state != prev_state:
                was, now = prev_state == "in_sync", cur_state == "in_sync"
                if was != now:
                    ev = "bias_aligned" if now else "bias_diverged"
                    published.append(_fire(sym, ev, expiry, {
                        "trust_state": cur_state,
                        "previous_state": prev_state,
                        "win_rate": str(trust.get("win_rate") or ""),
                        "graded": str(graded),
                        "hint": str(trust.get("hint_text") or ""),
                    }))
            state.last_trust_state[sym] = cur_state

            # 6. win_rate_notable — only when earned (§3). Either rule suffices.
            alert = bool(evaluator_state.regime_alert_active_by_symbol.get(sym, False))
            prev_alert = state.last_regime_alert.get(sym)
            tier = _tier(pct, graded, green, red)
            prev_tier = state.last_win_tier.get(sym)

            recovered = prev_alert is True and alert is False
            crossed_green = (prev_tier is not None and prev_tier != "green"
                             and tier == "green" and graded >= min_graded)
            if recovered or crossed_green:
                reason = "recovery" if recovered else "win_streak"
                published.append(_fire(sym, "win_rate_notable", expiry, {
                    "reason": reason,
                    "win_rate": f"{pct:.0f}",
                    "graded": str(graded),
                    "wins": str(stats.get("wins") or 0),
                    "losses": str(stats.get("losses") or 0),
                    "consec_wins": str(evaluator_state.consec_wins_by_symbol.get(sym, 0)),
                }))
            state.last_regime_alert[sym] = alert
            state.last_win_tier[sym] = tier

            # 7. daily_recap — once per ET day, the day's best and worst pick.
            #    Fires on the first sweep of a NEW day, recapping the day just
            #    finished (that is when the extremes are final).
            recap_for = state.day_date.get(sym)
            if (recap_for and recap_for != today
                    and state.last_recap_date.get(sym) != recap_for):
                best = state.day_best.get(sym) or {}
                worst = state.day_worst.get(sym) or {}
                state.last_recap_date[sym] = recap_for
                if best or worst:
                    attrs = {"session_date": recap_for}
                    attrs.update({f"best_{k}": v for k, v in best.items()})
                    attrs.update({f"worst_{k}": v for k, v in worst.items()})
                    published.append(_fire(sym, "daily_recap", expiry, attrs))
    except Exception:
        log.exception("[matrix-signals] on_evaluation failed (ignored)")
    return published
