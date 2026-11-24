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
import copy
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import matrix_events, matrix_ledger

log = logging.getLogger("edgelane.matrix.signals")

_SESSION_TZ = ZoneInfo("America/New_York")      # same trading day boundary as evaluator.py

# A digest targets ~2x/week, so it needs BOTH a real change and a cooldown —
# cadence alone would post a grid nobody's looking at, change alone could post
# several times a day on a choppy session.
_DIGEST_MIN_HOURS = 60.0          # ~2.5 days
_DIGEST_MIN_CHANGED = 3           # of the 8 strategy cards

# THE quality bar for any post that puts a win rate in front of the world.
#
# Policy (set by the product owner, 2026-09-23): the engine only speaks when it
# has something worth showing. A win rate at or below this is not a showcase,
# so no event that DISPLAYS one — a pick, a bias flip, a recovery — goes out
# unless the rolling rate is strictly above it, on at least eval_min_graded
# picks. (pct is wins/n with neutrals in the denominator, so this is a real bar.)
# The 2026-09-22 incident (10% / 15% "notable" posts) and the 2026-09-23 one (a
# bias_aligned card at 45%) are both what happens without it.
_SHOW_MIN_WIN_PCT = 55.0

# A pick is only a CALL once it has held long enough that someone could have
# traded it. The grading dwell (pick_min_dwell_polls, ~48s) exists to drop
# flickers from the win rate; it is far too short for a public announcement —
# on 2026-09-29 a pick was announced, dropped within a minute, and its result
# went out as "Bear Call lost … Held 0 min". Matrix trades 1–20 minute holds, so
# a pick must stand this long before it is announced, and a result is only
# reported for a pick that did.
_ANNOUNCE_MIN_HOLD_MIN = 5.0

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
        self.min_graded: int = 10              # mirrors settings.eval_min_graded
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
        self.day_best_pick: dict[str, dict] = {}     # the full best pick, for its card
        # Levels mode only (MATRIX_DECISION_SOURCE=levels): the regime replayed
        # from level legs ({"alert", "consec_wins"}), set by each evaluation
        # pass. Present ⇒ _recovery_earned reads it instead of the legacy
        # evaluator's counters (which never move in levels mode).
        self.level_regime: dict[str, dict] = {}

    def reset(self) -> None:
        for d in (self.last_pick_key, self.cur_pick_key, self.cur_pick_polls,
                  self.pick_blocked, self.last_win_rate, self.last_graded,
                  self.cur_pick_since, self.pending_results,
                  self.last_trust_state, self.last_win_tier,
                  self.last_regime_alert, self.session_open_date, self.last_digest_at,
                  self.last_digest_grid, self.last_recap_date, self.day_date,
                  self.day_best, self.day_worst, self.day_best_pick, self.level_regime):
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
          attrs: dict[str, str] | None = None, disc: str | None = None,
          snap: dict | None = None, card_pick: dict | None = None) -> str:
    """Queue one event and return immediately. Returns the state name, so
    callers report what was HANDED OFF — not what was confirmed sent.

    `disc` distinguishes repeat occurrences of a state within one day (see
    matrix_events.event_id). Omit it for states that happen once a day.

    `snap` is the snapshot the event describes. It is FROZEN here, synchronously,
    before anything else can poll — then, in the background, the ledger row
    (data + card HTML, matrix_ledger) is written and only once it exists is the
    Pub/Sub message published. `card_pick` is for events about a PAST pick
    (pick_result, daily_recap): its card shows that pick, not today's."""
    frozen = matrix_ledger.freeze_snapshot(snap) if snap is not None else None
    frozen_pick = copy.deepcopy(card_pick) if card_pick else None
    event_at = datetime.now(timezone.utc)
    attrs = dict(attrs or {})

    async def _run() -> None:
        try:
            if frozen is not None and matrix_events.is_enabled():
                eid = matrix_events.event_id(symbol, state_name, None, disc)
                extra: dict[str, Any] = {}
                acc = await _level_accuracy_for(symbol)
                if acc is not None:          # levels mode: the level engine's record
                    extra["accuracy"] = acc
                row = await asyncio.to_thread(
                    matrix_ledger.build_row, event_id=eid, symbol=symbol,
                    state=state_name, expiry=expiry, attrs=attrs, snap=frozen,
                    card_pick=frozen_pick, event_at=event_at, **extra)
                if not await matrix_ledger.record(row):
                    # No row, no message: a poster woken for an event it can't
                    # read would only log an error.
                    log.error("[matrix-signals] %s %s not published — ledger write failed",
                              symbol, state_name)
                    return
                attrs.update({"ledger": "1", "event_at": event_at.isoformat()})
            await matrix_events.publish_transition(
                symbol, state_name, expiry, discriminator=disc, extra_attributes=attrs)
        except Exception:                      # publish_transition swallows its own,
            log.exception("[matrix-signals] publish task failed")   # this is belt-and-braces
    task = asyncio.create_task(_run())
    _INFLIGHT.add(task)                        # hold a ref; asyncio only weakrefs tasks
    task.add_done_callback(_INFLIGHT.discard)
    return state_name


async def _level_accuracy_for(symbol: str) -> tuple[dict, dict] | None:
    """Levels mode (SPX): the (trust, stats) a ledger row / card shows, from the
    level engine's grading. Engine mode: None (build_row reads the legacy
    grader itself, as before)."""
    from . import level_accuracy
    from .config import get_settings
    if not level_accuracy.applies(get_settings(), symbol):
        return None
    from .routes.matrix import _accuracy_view_async
    return await _accuracy_view_async(str(symbol).upper())


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
    it in would make every single poll look like a brand-new pick.

    A LEVEL pick (levels mode, `source == "levels"`) is keyed by its `pick_id`
    — the identity the level engine grades its legs under (§3.4), so a
    pick_result resolves against the same thing the chip announced."""
    if pick.get("source") == "levels" and pick.get("pick_id"):
        return f"pick_id:{pick.get('pick_id')}"
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


def _record_worth_showing(pct: Any, graded: Any, min_graded: int) -> bool:
    """Is this track record something to show the world? Strictly above the bar,
    on a real sample. Unknown ⇒ no."""
    try:
        return int(graded or 0) >= min_graded and float(pct) > _SHOW_MIN_WIN_PCT
    except (TypeError, ValueError):
        return False


def _recovery_earned(sym: str) -> bool:
    """Has the engine actually proven itself since it was last suppressed?

    Deliberately NOT just "the flag flipped back to good": a bias that
    re-syncs on one poll can un-sync on the next, and announcing on that edge
    is how you end up posting the same broken idea repeatedly. Mirrors the
    earned-recovery rule `win_rate_notable` already uses — the pause must be
    off AND there must be a real graded win behind it."""
    lvl = state.level_regime.get(sym)
    if lvl is not None:                  # levels mode: the level-leg regime
        if lvl.get("alert"):
            return False
        return int(lvl.get("consec_wins") or 0) >= 1
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
    since = state.cur_pick_since.get(sym)
    held = ((datetime.now(timezone.utc) - since).total_seconds() / 60.0) if since else 0.0
    if held < _ANNOUNCE_MIN_HOLD_MIN:
        return f"held {held:.1f}m < {_ANNOUNCE_MIN_HOLD_MIN:.0f}m"
    # The chip's takeaway is the engine's record — a pick announced beside a
    # losing record undercuts itself.
    wr, graded = state.last_win_rate.get(sym), state.last_graded.get(sym)
    if not _record_worth_showing(wr, graded, state.min_graded):
        return f"record={wr if wr is not None else 'unknown'}% over {graded or 0}"
    return None

def _engine_state(trust_state: Any) -> str:
    """paused | calibrating | active — `low_conf` is bias-derived, so on anything
    about the engine it counts as active."""
    st = str(trust_state or "")
    return st if st in ("paused", "calibrating") else ("active" if st else "")


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
        # The ENGINE's own state (paused | calibrating | active). Not the raw
        # trust state: its `low_conf` comes from the bias engine's confidence —
        # market data, which must not ride on a pick event.
        "engine_state": _engine_state(state.last_trust_state.get(sym)),
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
    """A session_open chip needs the walls it actually SHOWS: the call wall and
    the put wall. (It used to accept any of call/put/VEX/TEX — on 2026-09-30
    NDX's first poll had VEX/TEX but no call/put yet, so the open fired, froze
    empty walls into the ledger, and posted "today's session open" with no
    levels at all.) §2: skip silently rather than post an empty frame."""
    return (bias.get("call_wall_strike") is not None
            and bias.get("put_wall_strike") is not None)


# Walls can take a poll or two to resolve at the bell. session_open keeps
# checking until they do — but only for this long after the open, so a late
# resolve never produces a "session open" post in the middle of the afternoon.
_SESSION_OPEN_WINDOW_MIN = 60.0


def _within_open_window(settings: Any) -> bool:
    now = datetime.now(_SESSION_TZ)
    try:
        hh, mm = str(getattr(settings, "market_open", "09:30") or "09:30").split(":")
        opened = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    except (TypeError, ValueError):
        opened = now.replace(hour=9, minute=30, second=0, microsecond=0)
    return 0 <= (now - opened).total_seconds() / 60.0 <= _SESSION_OPEN_WINDOW_MIN


def _track_day_extremes(sym: str, pick: dict, today: str) -> None:
    """Remember the day's best and worst composite pick for the recap."""
    if state.day_date.get(sym) != today:
        state.day_date[sym] = today
        state.day_best.pop(sym, None)
        state.day_worst.pop(sym, None)
        state.day_best_pick.pop(sym, None)
    score = pick.get("composite_score")
    try:
        score = float(score)
    except (TypeError, ValueError):
        return
    row = dict(_pick_summary(pick), composite_score=str(score))
    best, worst = state.day_best.get(sym), state.day_worst.get(sym)
    if best is None or score > float(best.get("composite_score") or -1e9):
        state.day_best[sym] = row
        state.day_best_pick[sym] = copy.deepcopy(pick)
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


def _resolve_pick_results(sym: str, db: Any, snap: dict | None = None) -> list[str]:
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

        first_ts = _as_utc(run.get("first_ts"))
        held = ((last_ts - first_ts).total_seconds() / 60.0) if (first_ts and last_ts) else 0.0
        if held < _ANNOUNCE_MIN_HOLD_MIN:
            log.info("[matrix-signals] %s pick_result not posted (held %.1fm — not a trade)",
                     sym, held)
            continue

        result = str(final.get("result") or "")
        if result not in ("win", "loss"):
            log.info("[matrix-signals] %s pick_result not posted (%s — no takeaway)",
                     sym, result or "ungraded")
            continue

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
                               disc=matrix_events.discriminator(p["key"]),
                               snap=snap, card_pick=p.get("pick")))
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
        if settings is not None and "level_decision" not in snap:
            from . import level_accuracy
            if level_accuracy.applies(settings, sym):
                # Levels mode: every post follows the LEVEL pick / grid — the
                # level-view copy (the poller's cache is never mutated).
                snap = await _level_view_snap(settings, snap)
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
                    if reason and reason.startswith("held "):
                        pass                 # not yet a call — re-checked next poll
                    elif reason:
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
                            disc=matrix_events.discriminator(key), snap=snap))
                        # Close the loop later: report how THIS pick ended.
                        # The full pick is kept so the result's card shows THIS
                        # pick, not whatever the engine is showing by then.
                        entry = {
                            "key": key, "expiry": expiry, "summary": summary,
                            "pick": copy.deepcopy(pick),
                            "since": state.cur_pick_since.get(sym)
                                     or datetime.now(timezone.utc),
                            "announced_at": datetime.now(timezone.utc),
                        }
                        if pick.get("source") == "levels":
                            # Resolved against the level leg graded under it.
                            entry["pick_id"] = pick.get("pick_id")
                            entry["call"] = pick.get("call")
                        state.pending_results.setdefault(sym, []).append(entry)

        # 2. session_open — the first persisted poll of a new ET day whose call
        #    and put walls have resolved, within the first hour of the session.
        #    Not marked done until it fires, so an empty first poll just waits
        #    for the next one.
        if (state.session_open_date.get(sym) != today and _walls_worth_showing(bias)
                and _within_open_window(settings)):
            state.session_open_date[sym] = today
            # The market read: walls + the bias engine's direction. This is the
            # one event that is ABOUT direction, so it's the one that carries it.
            attrs = {k: str(bias.get(k)) for k in
                     ("call_wall_strike", "put_wall_strike", "vex_wall_strike", "tex_wall_strike",
                      "bias_label", "confidence")
                     if bias.get(k) is not None}
            try:
                ds = float(bias.get("directional_score"))
                attrs["bias_direction"] = "bearish" if ds < 0 else ("bullish" if ds > 0 else "neutral")
                attrs["bias_strength"] = f"{abs(ds):.0f}"
            except (TypeError, ValueError):
                pass
            if snap.get("expected_move") is not None:
                attrs["expected_move"] = str(snap.get("expected_move"))
            published.append(_fire(sym, "session_open", expiry, attrs, snap=snap))

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
                published.append(_fire(sym, "grid_digest", expiry, snap=snap))
    except Exception:
        log.exception("[matrix-signals] on_snapshot failed (ignored)")
    return published


def _trust_and_rate_events(sym: str, expiry: Any, snap: dict, trust: dict, graded: int,
                           pct: float, wins: Any, losses: Any, alert: bool,
                           consec_wins: Any, min_graded: int, green: float,
                           red: float) -> list[str]:
    """Signals 4/5 (bias_aligned | bias_diverged) and 6 (win_rate_notable) for
    one symbol, and the last-published markers they gate on. Shared by the
    legacy sweep and the levels pass — only the record they read differs."""
    from .routes.accuracy import _tier

    published: list[str] = []
    # 4/5. bias_aligned | bias_diverged — the trust relationship flipped.
    #      in_sync is "aligned"; everything else (low_conf, calibrating,
    #      paused) is "diverged". Fires on the transition, not per poll.
    cur_state = str(trust.get("state") or "")
    prev_state = state.last_trust_state.get(sym)
    if prev_state is not None and cur_state != prev_state:
        was, now = prev_state == "in_sync", cur_state == "in_sync"
        if was != now:
            ev = "bias_aligned" if now else "bias_diverged"
            # Not every flip is news. The engine posts a bias change only
            # when it says something true and worth showing:
            #   aligned  — bias agrees AND the record behind it clears
            #              the show bar (else "it agrees" means nothing)
            #   diverged — only a real PAUSE (a genuine loss streak) off a
            #              record we had been showing. low_conf/calibrating
            #              wobbles are noise, never posted.
            good = _record_worth_showing(trust.get("win_rate") if now else pct,
                                         graded, min_graded)
            if now:
                meaningful = good
                why = "record below the show bar"
            else:
                prior_good = _record_worth_showing(
                    state.last_win_rate.get(sym), state.last_graded.get(sym),
                    min_graded)
                meaningful = cur_state == "paused" and prior_good
                why = ("not a real pause" if cur_state != "paused"
                       else "no shown record to retract")
            if not meaningful:
                log.info("[matrix-signals] %s %s suppressed (%s>%s, %s)",
                         sym, ev, prev_state, cur_state, why)
                ev = None
        if was != now and ev:
            published.append(_fire(sym, ev, expiry, snap=snap,
                                   disc=matrix_events.discriminator(
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
    prev_alert = state.last_regime_alert.get(sym)
    tier = _tier(pct, graded, green, red)
    prev_tier = state.last_win_tier.get(sym)

    # Recovery = the pause just lifted AND the number the post will show
    # is genuinely good on a real sample. Without the pct floor, 2 wins
    # in a row could announce a 10% record as "notable".
    lifted = prev_alert is True and alert is False
    recovered = (lifted and graded >= min_graded
                 and pct > _SHOW_MIN_WIN_PCT)
    crossed_green = (prev_tier is not None and prev_tier != "green"
                     and tier == "green" and graded >= min_graded)
    if lifted and not recovered:
        log.info("[matrix-signals] %s win_rate_notable suppressed "
                 "(recovery at %.0f%% over %d graded — below the %.0f%% bar)",
                 sym, pct, graded, _SHOW_MIN_WIN_PCT)
    if recovered or crossed_green:
        reason = "recovery" if recovered else "win_streak"
        published.append(_fire(sym, "win_rate_notable", expiry, snap=snap,
                               disc=matrix_events.discriminator(reason), attrs={
            "reason": reason,
            "win_rate": f"{pct:.0f}",
            "graded": str(graded),
            "wins": str(wins or 0),
            "losses": str(losses or 0),
            "consec_wins": str(consec_wins),
        }))
    state.last_regime_alert[sym] = alert
    state.last_win_tier[sym] = tier
    return published


async def on_evaluation(db: Any, poller_state: Any, settings: Any) -> list[str]:
    """Evaluator-side transitions, called at the end of the existing sweep.

    The sweep is the one place that already holds the before/after of the regime
    counters, so it can tell "did this just change" without re-deriving it
    (docs/matrix_events_update.md §3). Never raises.

    Levels mode (MATRIX_DECISION_SOURCE=levels): the record is the level
    engine's binary leg grades — see `_on_evaluation_levels`."""
    from . import level_accuracy
    if level_accuracy.levels_mode(settings):
        return await _on_evaluation_levels(db, poller_state, settings)

    from .evaluator import state as evaluator_state
    from .routes.accuracy import _trust_state

    published: list[str] = []
    try:
        symbols = list(getattr(poller_state, "latest_by_symbol", {}) or {})
        today = _et_date()
        min_graded = int(getattr(settings, "eval_min_graded", 10))
        state.min_graded = min_graded
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

            alert = bool(evaluator_state.regime_alert_active_by_symbol.get(sym, False))
            published.extend(_trust_and_rate_events(
                sym, expiry, snap, trust, graded, pct,
                stats.get("wins"), stats.get("losses"), alert,
                evaluator_state.consec_wins_by_symbol.get(sym, 0),
                min_graded, green, red))

            # 8. pick_result — how each posted pick actually ended.
            published.extend(_resolve_pick_results(sym, db, snap))

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
                    published.append(_fire(sym, "daily_recap", expiry, attrs, snap=snap,
                                           card_pick=state.day_best_pick.get(sym)))
    except Exception:
        log.exception("[matrix-signals] on_evaluation failed (ignored)")
    return published


# ── levels mode (docs/level_engine.md §3.4, §12) ────────────────────────────

async def _level_view_snap(settings: Any, snap: dict) -> dict:
    """The level-view copy of a poller snapshot (pick + grid from the level
    engine). Already a level view → as is. Any failure → the snapshot with NO
    pick and an empty grid: levels mode never posts a legacy pick."""
    if "level_decision" in snap:
        return snap
    from . import level_view
    try:
        return await level_view.level_snapshot(settings, snap)
    except Exception:
        log.exception("[matrix-signals] level view failed — posting no pick")
        out = dict(snap)
        out["engine_pick"] = None
        out["strategies"] = {}
        out["level_decision"] = {"status": "unavailable"}
        return out


def _ts_utc(v: Any) -> datetime | None:
    from .level_accuracy import _parse_ts
    return _parse_ts(v)


def _match_level_leg(legs: list[dict], open_leg: dict | None, p: dict) -> tuple[str, dict | None]:
    """('closed', leg) | ('open', None) | ('none', None) for one announced
    level pick. By `pick_id` — the leg graded under the announced pick, closed
    after its run began. Fallback (the pick was re-struck mid-leg, so its id is
    not the leg's opening id): the leg of the same call that was live when the
    pick was announced."""
    pid = p.get("pick_id")
    since = (p.get("since") or p["announced_at"]) - timedelta(seconds=90)
    announced = p["announced_at"]
    if open_leg and pid and open_leg.get("pick_id") == pid:
        return "open", None
    hits = []
    for leg in legs:
        closed = _ts_utc(leg.get("closed_at"))
        if pid and leg.get("pick_id") == pid and closed and closed >= since:
            hits.append((closed, leg))
    if hits:
        return "closed", min(hits, key=lambda h: h[0])[1]
    call = p.get("call")
    if open_leg and call and open_leg.get("call") == call:
        opened = _ts_utc(open_leg.get("opened_at"))
        if opened and opened <= announced:
            return "open", None
    for leg in legs:
        opened, closed = _ts_utc(leg.get("opened_at")), _ts_utc(leg.get("closed_at"))
        if (call and leg.get("call") == call and opened and closed
                and opened <= announced <= closed):
            return "closed", leg
    return "none", None


def _resolve_level_pick_results(sym: str, stats: dict, snap: dict | None = None) -> list[str]:
    """Levels mode pick_result: each announced level pick, resolved by the
    level leg graded under its `pick_id` — binary win / loss (C2), with the
    leg's entry / exit spot, strategy P&L, hold and close reason."""
    from . import level_accuracy as la

    published: list[str] = []
    pending = state.pending_results.get(sym) or []
    if not pending:
        return published
    now = datetime.now(timezone.utc)
    legs = la.closed_legs(stats)
    ol = stats.get("open_leg") if isinstance(stats.get("open_leg"), dict) else None
    open_leg = la.normalize_leg(ol) if ol else None
    keep: list[dict] = []
    for p in pending:
        age_h = (now - p["announced_at"]).total_seconds() / 3600.0
        status, leg = _match_level_leg(legs, open_leg, p)
        if leg is None:
            if age_h < _RESULT_MAX_WAIT_HOURS:
                keep.append(p)          # still open / not graded yet
            else:
                log.info("[matrix-signals] %s pick_result dropped (no level leg "
                         "closed within %.0fh)", sym, _RESULT_MAX_WAIT_HOURS)
            continue
        opened, closed = _ts_utc(leg.get("opened_at")), _ts_utc(leg.get("closed_at"))
        held = ((closed - opened).total_seconds() / 60.0) if (opened and closed) else 0.0
        if held < _ANNOUNCE_MIN_HOLD_MIN:
            log.info("[matrix-signals] %s pick_result not posted (held %.1fm — not a trade)",
                     sym, held)
            continue
        result = str(leg.get("result") or "")
        if result not in ("win", "loss"):
            continue
        tgt = leg.get("target_level") if isinstance(leg.get("target_level"), dict) else {}
        pnl = leg.get("strategy_pnl")
        attrs = dict(p.get("summary") or {})
        attrs.update({
            "result": result,
            "entry_premium": "",
            "exit_premium": "",
            "favorable_delta": "" if pnl is None else str(pnl),
            "held_minutes": f"{held:.0f}",
            "announced_at": p["announced_at"].isoformat(),
            "source": "levels",
            "pick_id": str(p.get("pick_id") or ""),
            "leg_id": str(leg.get("id") or ""),
            "call": str(leg.get("call") or ""),
            "entry_spot": "" if leg.get("entry_spot") is None else str(leg.get("entry_spot")),
            "exit_spot": "" if leg.get("exit_spot") is None else str(leg.get("exit_spot")),
            "strategy_pnl": "" if pnl is None else str(pnl),
            "close_reason": str(leg.get("close_reason") or ""),
            "target_level": str(tgt.get("name") or ""),
            "target_price": "" if tgt.get("price") is None else str(tgt.get("price")),
        })
        published.append(_fire(sym, "pick_result", p.get("expiry"), attrs,
                               disc=matrix_events.discriminator(p["key"]),
                               snap=snap, card_pick=p.get("pick")))
    state.pending_results[sym] = keep
    return published


def _leg_rank(leg: dict) -> tuple:
    try:
        pnl = float(leg.get("strategy_pnl"))
    except (TypeError, ValueError):
        pnl = float("-inf")
    try:
        prog = float(leg.get("progress_pct"))
    except (TypeError, ValueError):
        prog = float("-inf")
    return (leg.get("result") == "win", pnl, prog)


def _leg_summary(leg: dict) -> dict[str, str]:
    pick = leg.get("pick") if isinstance(leg.get("pick"), dict) else None
    base = _pick_summary(pick or {
        "strategy": leg.get("strategy"), "label": leg.get("label"),
        "structure_text": leg.get("structure_text")})
    tgt = leg.get("target_level") if isinstance(leg.get("target_level"), dict) else {}
    pnl = leg.get("strategy_pnl")
    base.update({
        "result": str(leg.get("result") or ""),
        "strategy_pnl": "" if pnl is None else str(pnl),
        "close_reason": str(leg.get("close_reason") or ""),
        "target_level": str(tgt.get("name") or ""),
        "pick_id": str(leg.get("pick_id") or ""),
    })
    return base


async def _level_daily_recap(sym: str, settings: Any, snap: dict, expiry: Any,
                             today: str) -> list[str]:
    """daily_recap from the finished day's level legs: headlined by the best
    leg (a win, then strategy P&L), the worst riding along. Engine
    unreachable → retried next pass (not marked done)."""
    from . import level_accuracy as la

    recap_for = state.day_date.get(sym)
    if not (recap_for and recap_for != today and state.last_recap_date.get(sym) != recap_for):
        return []
    day = await la.level_day(settings, recap_for)
    if day is None:
        return []
    state.last_recap_date[sym] = recap_for
    legs = [la.normalize_leg(r) for r in (day.get("legs") or []) if isinstance(r, dict)]
    legs = [l for l in legs if l.get("result") in ("win", "loss")]
    if not legs:
        return []
    best = max(legs, key=_leg_rank)
    worst = min(legs, key=_leg_rank)
    wins = sum(1 for l in legs if l["result"] == "win")
    attrs = {"session_date": recap_for, "headline": "best", "source": "levels",
             "wins": str(wins), "losses": str(len(legs) - wins),
             "win_rate": f"{100.0 * wins / len(legs):.0f}"}
    attrs.update({f"best_{k}": v for k, v in _leg_summary(best).items()})
    attrs.update({f"worst_{k}": v for k, v in _leg_summary(worst).items()})
    card = best.get("pick") if isinstance(best.get("pick"), dict) else state.day_best_pick.get(sym)
    return [_fire(sym, "daily_recap", expiry, attrs, snap=snap, card_pick=card)]


async def _on_evaluation_levels(db: Any, poller_state: Any, settings: Any) -> list[str]:
    """Levels-mode evaluation pass (SPX only — the level engine's symbol):
    trust / win-rate transitions from the level stats, pick_result by pick_id,
    daily_recap from the day's legs. Engine unreachable → nothing changes
    (no transition is inferred from an outage). Never raises."""
    from . import level_accuracy as la

    published: list[str] = []
    try:
        sym = la.LEVELS_SYMBOL
        raw = (getattr(poller_state, "latest_by_symbol", {}) or {}).get(sym)
        if not raw:
            return published
        min_graded = int(getattr(settings, "eval_min_graded", 10))
        state.min_graded = min_graded
        green = float(getattr(settings, "pill_green_pct", 60.0))
        red = float(getattr(settings, "pill_red_pct", 40.0))
        stats = await la.level_stats(settings)
        if stats is None:
            log.debug("[matrix-signals] %s level stats unavailable — signals wait", sym)
            return published
        snap = await _level_view_snap(settings, raw)
        expiry = snap.get("expiration")
        p = la.accuracy_payload(stats, settings, sym)
        state.level_regime[sym] = {"alert": bool(p["regime_alert"]),
                                   "consec_wins": int(p["regime_consec_wins"])}
        published.extend(_trust_and_rate_events(
            sym, expiry, snap, p, int(p["n"]), float(p["accuracy_pct"]),
            p["wins"], p["losses"], bool(p["regime_alert"]), p["regime_consec_wins"],
            min_graded, green, red))
        published.extend(_resolve_level_pick_results(sym, stats, snap))
        published.extend(await _level_daily_recap(sym, settings, snap, expiry, _et_date()))
    except Exception:
        log.exception("[matrix-signals] levels on_evaluation failed (ignored)")
    return published
