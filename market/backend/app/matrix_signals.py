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
                        → bias_aligned / bias_diverged, win_rate_notable, daily_recap,
                          pick_result (the outcome of each pick it announced)

The six states and their sources are the table in §2 of that doc. Nothing here
re-computes an opinion: it reads what the engine and the grader already decided
and asks only "is this different from what we last published?".
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
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

# The bar a rolling win rate must clear before a "recovery" is worth announcing.
# A losing streak ending is not by itself notable: two consecutive wins clear the
# regime pause even when the 20-pick window still reads 10% (the 2026-09-22
# incident — see docs/matrix_events_update.md). The post DISPLAYS the rolling
# number, so the rolling number is what has to be good. 50% of graded picks won
# (neutrals count against it) means wins at least match losses-plus-pushes.
_NOTABLE_MIN_WIN_PCT = 50.0

# pick_result: how long to keep waiting for a posted pick's outcome before
# giving up (a run that never closes, or closes after the session and is never
# graded). Generous — the cost of waiting is nothing; the cost of a wrong
# result is credibility.
_RESULT_MAX_WAIT_HOURS = 8.0
# Once a run has closed, how long to wait for its final poll to be graded
# before settling on the last grade it DID get (the grader lags ~3 min).
_RESULT_GRADE_GRACE_MIN = 15.0


class MatrixSignalState:
    """Last-published markers, per symbol. In-memory by design: these gate
    *publishing*, not correctness, so a restart re-firing one chip is harmless
    (the deterministic event_id makes it a downstream no-op within the same UTC
    day). Mirrors how `EvaluatorState` holds the regime counters."""

    def __init__(self) -> None:
        self.last_pick_key: dict[str, str] = {}      # last ANNOUNCED pick
        # The pick currently on screen and how many consecutive polls it has
        # held. A pick must survive `pick_min_dwell_polls` before it is
        # announced — the engine's top pick flickers, and a 30-second flicker is
        # not a call worth posting (and is not graded either).
        self.cur_pick_key: dict[str, str] = {}
        self.cur_pick_polls: dict[str, int] = {}
        # True once a pick has been SUPPRESSED (broken structure / bias out of
        # sync). Resuming needs more than the flag clearing — see _recovery_earned.
        self.pick_blocked: dict[str, bool] = {}
        # When the current run began (UTC) — where a posted pick's outcome is
        # looked up from once the run ends.
        self.cur_pick_since: dict[str, datetime] = {}
        # Picks announced via pick_selected whose outcome hasn't been reported
        # yet. Each closes the loop with one pick_result.
        self.pending_results: dict[str, list[dict]] = {}
        # Last published win rate, carried onto the pick chip as its takeaway.
        self.last_win_rate: dict[str, float | None] = {}
        self.last_graded: dict[str, int] = {}
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
        for d in (self.last_pick_key, self.cur_pick_key, self.cur_pick_polls,
                  self.pick_blocked, self.last_win_rate, self.last_graded,
                  self.cur_pick_since, self.pending_results,
                  self.last_trust_state, self.last_win_tier,
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
          attrs: dict[str, str] | None = None, disc: str | None = None) -> str:
    """Queue one publish and return immediately. Returns the state name, so
    callers report what was HANDED OFF — not what was confirmed sent.

    `disc` distinguishes repeat occurrences of a state within one day (see
    matrix_events.event_id). Omit it for states that happen once a day."""
    async def _run() -> None:
        try:
            await matrix_events.publish_transition(
                symbol, state_name, expiry, discriminator=disc, extra_attributes=attrs)
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
    """Identity of an engine pick — the SAME notion the win rate uses.

    The poller persists `pick_legs = json.dumps(pick["legs"])` and the grader
    cuts episodes wherever that value changes (db._EPISODE_CTE), so keying on
    the legs here means a chip and a graded episode describe the same thing.
    Falls back to strategy/label/strikes when a snapshot carries no legs.

    Composite score is deliberately excluded: it drifts every poll, so folding
    it in would make every single poll look like a brand-new pick."""
    legs = pick.get("legs")
    if legs:
        try:
            return json.dumps(legs)
        except (TypeError, ValueError):
            pass
    strikes = pick.get("strikes")
    if isinstance(strikes, (list, tuple)):
        strikes = "/".join(str(s) for s in strikes)
    return "|".join(str(x) for x in (
        pick.get("strategy") or "", pick.get("label") or "", strikes or "",
    ))


def _dwell(settings: Any) -> int:
    """Polls a pick must hold before it counts. One knob, shared with the win
    rate (see config.pick_min_dwell_polls)."""
    try:
        return max(1, int(getattr(settings, "pick_min_dwell_polls", 1) or 1))
    except (TypeError, ValueError):
        return 1


# A pick_selected chip is a claim that the tool found something worth seeing.
# These are the states where that claim would be false. `health` comes from
# strategy_engine (lowercase: healthy | thin | directional | broken |
# capital_trap); "broken" and "capital_trap" are the two it scores as
# disqualifying, and "do not trade" is the composite verdict below the skip
# threshold. Compared case-insensitively — the UI renders them upper-case.
_UNPOSTABLE_HEALTH = {"broken", "capital_trap", "do_not_trade"}
_UNPOSTABLE_VERDICT = ("do not trade", "skip")


def _recovery_earned(sym: str) -> bool:
    """Has the engine actually proven itself since it was last suppressed?

    Deliberately NOT just "the flag flipped back to good": a bias that
    re-syncs on one poll can un-sync on the next, and announcing on that edge
    is how you end up posting the same broken idea repeatedly. Mirrors the
    earned-recovery rule `win_rate_notable` already uses — the pause must be
    off AND there must be a real graded win behind it."""
    from .evaluator import state as evaluator_state
    if evaluator_state.regime_alert_active_by_symbol.get(sym, False):
        return False
    return int(evaluator_state.consec_wins_by_symbol.get(sym, 0) or 0) >= 1


def _pick_block_reason(sym: str, pick: dict) -> str | None:
    """Why this pick must NOT be announced, or None if it may be.

    pick_selected is not a raw signal feed. It exists to show the tool is
    sharp, so a structurally-new pick is not automatically a postable one: a
    re-strike of a losing idea is still a losing idea (see the 2026-09-17
    incident in docs/matrix_events_update.md)."""
    health = str(pick.get("health") or "").strip().lower().replace(" ", "_")
    if health in _UNPOSTABLE_HEALTH:
        return f"health={health}"
    verdict = pick.get("composite_verdict") or {}
    label = str((verdict.get("label") if isinstance(verdict, dict) else "") or "").lower()
    if any(bad in label for bad in _UNPOSTABLE_VERDICT):
        return f"verdict={label}"
    # Bias must be IN SYNC. Unknown counts as not-in-sync: before the grader has
    # an opinion there is no performance to show, so there is nothing to claim.
    trust = state.last_trust_state.get(sym)
    if trust != "in_sync":
        return f"bias={trust or 'unknown'}"
    if state.pick_blocked.get(sym) and not _recovery_earned(sym):
        return "awaiting-confirming-win"
    return None

def _pick_summary(pick: dict, sym: str = "") -> dict[str, str]:
    """Compact attributes carried alongside a pick chip.

    Includes the current win rate so the post has a TAKEAWAY — "here is the
    call, and here is how this engine has been doing" — rather than being a
    bare signal with nothing to judge it by."""
    verdict = pick.get("composite_verdict") or {}
    wr = state.last_win_rate.get(sym)
    extra = {
        "win_rate": ("" if wr is None else f"{float(wr):.0f}"),
        "graded": str(state.last_graded.get(sym, "") or ""),
        "trust_state": str(state.last_trust_state.get(sym) or ""),
    } if sym else {}
    return {**extra,
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



def _as_utc(ts: Any) -> datetime | None:
    """A DuckDB TIMESTAMP back as an aware UTC datetime.

    DuckDB stores an aware datetime as NAIVE LOCAL time (the process's TZ), so
    a naive value must be read back as local — not stamped as UTC. In the
    container (TZ=UTC) the two coincide; on a developer host they differ by
    the UTC offset, which silently skewed every age check here by hours."""
    if ts is None:
        return None
    return ts.astimezone(timezone.utc)        # naive ⇒ interpreted as local


def _resolve_pick_results(sym: str, db: Any) -> list[str]:
    """Report the outcome of each announced pick whose run has ended.

    This is the loop the rest of the feed can't close on its own: a
    pick_selected post says "the engine likes this", and pick_result says how
    it actually went — graded exactly the way the win rate grades it (the
    run's LAST grade before the engine moved on, db._EPISODE_CTE), so the two
    can never disagree. Its event_id suffix is the same pick hash as the
    announcement, so the poster can thread the result under the original post.

    Neutrals are not posted: "it didn't move past the bid/ask noise" carries
    no takeaway. Losses ARE posted — a feed that only reports its wins is
    marketing, and the whole value of the self-eval is that it's believable.
    """
    published: list[str] = []
    pending = state.pending_results.get(sym) or []
    if not pending:
        return published
    now = datetime.now(timezone.utc)
    keep: list[dict] = []
    for p in pending:
        age_h = (now - p["announced_at"]).total_seconds() / 3600.0
        try:
            # A little before the run start: the run's first decision row is
            # written just before on_snapshot stamps `since`.
            run = db.fetch_pick_run(sym, p["key"], p["since"] - timedelta(seconds=90))
        except Exception:
            log.exception("[matrix-signals] fetch_pick_run failed for %s", sym)
            keep.append(p)
            continue

        if run is None or not run.get("closed"):
            if age_h < _RESULT_MAX_WAIT_HOURS:
                keep.append(p)          # still on screen — no result yet
            else:
                log.info("[matrix-signals] %s pick_result dropped (run never "
                         "closed within %.0fh)", sym, _RESULT_MAX_WAIT_HOURS)
            continue

        final = run.get("final")
        last_ts = _as_utc(run.get("last_ts"))
        waited_min = ((now - last_ts).total_seconds() / 60.0) if last_ts else 1e9
        if not run.get("last_graded") and waited_min < _RESULT_GRADE_GRACE_MIN:
            keep.append(p)              # closed, final poll not graded yet
            continue
        if not final:
            log.info("[matrix-signals] %s pick_result dropped (run ended ungraded)", sym)
            continue

        result = str(final.get("result") or "")
        if result not in ("win", "loss"):
            log.info("[matrix-signals] %s pick_result not posted (%s — no takeaway)",
                     sym, result or "ungraded")
            continue

        first_ts = _as_utc(run.get("first_ts"))
        held_min = ""
        if first_ts is not None and last_ts is not None:
            held_min = f"{(last_ts - first_ts).total_seconds() / 60.0:.0f}"
        attrs = dict(p.get("summary") or {})
        attrs.update({
            "result": result,
            "entry_premium": str(final.get("entry_net_premium") or ""),
            "exit_premium": str(final.get("eval_net_premium") or ""),
            "favorable_delta": str(final.get("favorable_delta") or ""),
            "held_minutes": held_min,
            "announced_at": p["announced_at"].isoformat(),
        })
        published.append(_fire(sym, "pick_result", p.get("expiry"), attrs,
                               disc=matrix_events.discriminator(p["key"])))
    state.pending_results[sym] = keep
    return published

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
            if key:
                # Count how long this exact pick has held.
                if state.cur_pick_key.get(sym) != key:
                    state.cur_pick_key[sym] = key
                    state.cur_pick_polls[sym] = 1
                    state.cur_pick_since[sym] = datetime.now(timezone.utc)
                else:
                    state.cur_pick_polls[sym] = state.cur_pick_polls.get(sym, 0) + 1
                # Announce once it has earned it, and only once per run —
                # and only when the pick is worth showing at all.
                if (state.cur_pick_polls[sym] >= _dwell(settings)
                        and state.last_pick_key.get(sym) != key):
                    reason = _pick_block_reason(sym, pick)
                    if reason:
                        # Suppressed. The RUN bookkeeping above still advanced,
                        # but `last_pick_key` deliberately does NOT — it means
                        # "last announced", so leaving it alone lets this very
                        # pick be announced later if it recovers, instead of
                        # being swallowed as already-said.
                        state.pick_blocked[sym] = True
                        log.info("[matrix-signals] %s pick_selected suppressed (%s)",
                                 sym, reason)
                    else:
                        state.last_pick_key[sym] = key
                        state.pick_blocked[sym] = False
                        summary = _pick_summary(pick, sym)
                        published.append(_fire(
                            sym, "pick_selected", expiry, summary,
                            disc=matrix_events.discriminator(key)))
                        # Close the loop later: report how THIS pick ended.
                        state.pending_results.setdefault(sym, []).append({
                            "key": key, "expiry": expiry, "summary": summary,
                            "since": state.cur_pick_since.get(sym)
                                     or datetime.now(timezone.utc),
                            "announced_at": datetime.now(timezone.utc),
                        })

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
        dwell = int(getattr(settings, "pick_min_dwell_polls", 1))

        for sym in symbols:
            snap = (getattr(poller_state, "latest_by_symbol", {}) or {}).get(sym) or {}
            expiry = snap.get("expiration")
            try:
                stats = db.fetch_accuracy(sym, window, min_dwell=dwell)
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
                    published.append(_fire(sym, ev, expiry, disc=matrix_events.discriminator(
                        f"{prev_state}>{cur_state}"), attrs={
                        "trust_state": cur_state,
                        "previous_state": prev_state,
                        "win_rate": str(trust.get("win_rate") or ""),
                        "graded": str(graded),
                        "hint": str(trust.get("hint_text") or ""),
                    }))
            state.last_trust_state[sym] = cur_state
            state.last_win_rate[sym] = trust.get("win_rate")
            state.last_graded[sym] = graded

            # 6. win_rate_notable — only when earned (§3). Either rule suffices.
            alert = bool(evaluator_state.regime_alert_active_by_symbol.get(sym, False))
            prev_alert = state.last_regime_alert.get(sym)
            tier = _tier(pct, graded, green, red)
            prev_tier = state.last_win_tier.get(sym)

            # Recovery = the pause just lifted AND the number the post will show
            # is genuinely good on a real sample. Without the pct floor, 2 wins
            # in a row could announce a 10% record as "notable".
            lifted = prev_alert is True and alert is False
            recovered = (lifted and graded >= min_graded
                         and pct >= _NOTABLE_MIN_WIN_PCT)
            crossed_green = (prev_tier is not None and prev_tier != "green"
                             and tier == "green" and graded >= min_graded)
            if lifted and not recovered:
                log.info("[matrix-signals] %s win_rate_notable suppressed "
                         "(recovery at %.0f%% over %d graded — below the %.0f%% bar)",
                         sym, pct, graded, _NOTABLE_MIN_WIN_PCT)
            if recovered or crossed_green:
                reason = "recovery" if recovered else "win_streak"
                published.append(_fire(sym, "win_rate_notable", expiry,
                                       disc=matrix_events.discriminator(reason), attrs={
                    "reason": reason,
                    "win_rate": f"{pct:.0f}",
                    "graded": str(graded),
                    "wins": str(stats.get("wins") or 0),
                    "losses": str(stats.get("losses") or 0),
                    "consec_wins": str(evaluator_state.consec_wins_by_symbol.get(sym, 0)),
                }))
            state.last_regime_alert[sym] = alert
            state.last_win_tier[sym] = tier

            # 8. pick_result — how each posted pick actually ended.
            published.extend(_resolve_pick_results(sym, db))

            # 7. daily_recap — once per ET day, headlined by the day's BEST pick.
            #    Fires on the first sweep of a NEW day, recapping the day just
            #    finished (that is when the extremes are final).
            #
            #    `headline="best"` is explicit: the post leads with the best
            #    pick, and the worst rides along as context rather than as the
            #    story. Leading with the worst would sit badly beside the
            #    pick_selected gate, which refuses to announce weak picks live —
            #    but dropping it entirely would be cherry-picking, and the whole
            #    point of the self-eval is that the number is believable. Note
            #    these are composite SCORES, so "worst" is the weakest idea the
            #    engine surfaced, not its biggest loss.
            recap_for = state.day_date.get(sym)
            if (recap_for and recap_for != today
                    and state.last_recap_date.get(sym) != recap_for):
                best = state.day_best.get(sym) or {}
                worst = state.day_worst.get(sym) or {}
                state.last_recap_date[sym] = recap_for
                # No best ⇒ no headline ⇒ nothing to post. Skip silently, the
                # way session_open skips a day with no walls worth showing.
                if best:
                    attrs = {"session_date": recap_for, "headline": "best"}
                    attrs.update({f"best_{k}": v for k, v in best.items()})
                    attrs.update({f"worst_{k}": v for k, v in worst.items()})
                    published.append(_fire(sym, "daily_recap", expiry, attrs))
    except Exception:
        log.exception("[matrix-signals] on_evaluation failed (ignored)")
    return published
