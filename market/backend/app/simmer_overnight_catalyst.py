"""Overnight catalyst analyzer — orchestration (docs/simmer_overnight_llm_spec.md).

Ties the grounded-LLM adapter (`simmer_llm`) + the premium verdict + the
deterministic combiner into one per-ticker analysis row, persisted to the
DEDICATED `simmer_overnight_analysis` table. Phase 0 is **read-only**: it logs
predictions (live + shadow + all combiner modes) and realized outcomes; it NEVER
changes the premium verdict the Overnight tab renders.

Best-effort like the rest of the off-hours path: every failure logs and degrades
(skip a name, leave a field empty) — it must never raise into the watcher loop.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from . import simmer_config, simmer_premium_tracker as pt, supabase_admin
from .config import get_settings
from .simmer_overnight_combiner import combine, reactive_call, reconcile

log = logging.getLogger("edgelane.simmer.overnight.catalyst")
_ET = ZoneInfo("America/New_York")


def _day_label(session: str) -> str:
    try:
        d = datetime.strptime(str(session)[:10], "%Y-%m-%d")
        return f"{d.strftime('%A')} {session[:10]}"
    except Exception:
        return str(session)[:10]


def _premium_trend_line(premium: dict) -> Optional[str]:
    """One-line premium-aware context for the grounded read (config-gated). Tells
    the LLM what the option tape is doing WITHOUT handing it any math."""
    pt, ut = premium.get("premium_trend"), premium.get("underlying_trend")
    if not pt and not ut:
        return None
    return (f"Option tape so far: premium is {pt or 'n/a'}, the underlying is "
            f"{ut or 'n/a'} (the premium-only engine currently says "
            f"{premium.get('call') or 'wait'}).")


def _make_llm(provider: str, model: str, s, ovn: dict):
    from .simmer_llm import make_llm
    return make_llm(provider, model=model or None, thinking=ovn.get("thinking", "medium"),
                    context_size=ovn.get("context_size", "medium"),
                    gemini_key=s.gemini_api_key, openai_key=s.openai_api_key)


async def _run_provider(llm, ctx) -> dict:
    """grounded_read → structured_read for one provider → a flat verdict dict (or
    {} on any failure). Carries the note, tokens and latency for calibration."""
    if llm is None:
        return {}
    note = await llm.grounded_read(ctx)
    if note is None:
        return {}
    verdict = await llm.structured_read(note.text, ctx)
    if verdict is None:
        return {"note": note.text, "queries": note.queries, "_incomplete": True}
    out = verdict.model_dump()
    out.update({"provider": llm.provider, "model": llm.model, "note": note.text,
                "queries": note.queries,
                "latency_ms": (note.latency_ms or 0) + (getattr(verdict, "latency_ms", 0) or 0),
                "tokens": _sum_tokens(note.tokens, getattr(verdict, "tokens", {}) or {})})
    return out


def _sum_tokens(a: dict, b: dict) -> dict:
    return {k: int((a or {}).get(k, 0)) + int((b or {}).get(k, 0)) for k in ("in", "out", "think")}


def _combiner_block(premium: dict, llm: dict, mode: str) -> dict:
    """Compute the two candidate modes (cheap) so the forward test can score both,
    and mark the one `decision_mode` selects as `final`. Modes are deliberately
    just two: `llm_only` (trust the catalyst read) and `combiner_blended` (premium
    gate, but a strong catalyst may promote a premium `wait`)."""
    cat = {"call": llm.get("call"), "sustain": llm.get("sustain"),
           "confidence": llm.get("confidence")} if llm.get("call") else None
    options = {
        "llm_only": {"call": llm.get("call"), "confidence": llm.get("confidence"), "source": "llm_only"},
        "combiner_blended": combine(premium, cat, promote=True),
    }
    final = options.get(mode) or options["llm_only"]
    return {"mode": mode, "final": final, **options}


_RECALL_SIDES = ("sell_bear_call", "sell_bull_put")


def _recall_target(analysis: dict, reconciled: dict) -> Optional[str]:
    """Phase B trigger (pure). Return the call to RE-VALIDATE with a fresh LLM read,
    or None. A recall is worth a paid call only when the intraday tape has MATERIALLY
    reversed against the 09:25 catalyst read — i.e. premium drove the reconciled call
    to the OPPOSITE committed side (`premium_override`) of what the LLM originally said.
    De-duped: never recall twice for the same reversed call (an `llm_recall.for_call`
    marker), so an override that persists doesn't re-spend."""
    r_call, r_src = reconciled.get("call"), reconciled.get("source")
    if r_src != "premium_override" or r_call not in _RECALL_SIDES:
        return None
    orig = (analysis.get("llm") or {}).get("call")
    if r_call == orig:                      # the tape agrees with the 09:25 read — nothing to re-check
        return None
    if (analysis.get("llm_recall") or {}).get("for_call") == r_call:
        return None                          # already re-validated this reversal
    return r_call


def _recall_ctx(analysis: dict, premium: dict, target: str, session: str):
    """A recall-flavoured grounded-read context: tell the LLM the tape has REVERSED
    against its 09:25 read and ask it to re-examine (debunk / overreaction / real
    reversal). No math handed over — just the premium-engine's own words."""
    from .simmer_llm import CatalystCtx
    orig = (analysis.get("llm") or {}).get("call") or "wait"
    gap = float(premium.get("gap") or analysis.get("gap") or 0.0)
    pt, ut = premium.get("premium_trend"), premium.get("underlying_trend")
    retr = premium.get("gap_retraced")
    line = (f"INTRADAY REVERSAL since the open: the 09:25 read was '{orig}' after a "
            f"{'down' if gap < 0 else 'up'} gap of {abs(gap) * 100:.1f}%. The premium-only "
            f"engine now reads '{premium.get('call') or 'wait'}' (premiums {pt or 'n/a'}, "
            f"underlying {ut or 'n/a'}" + (f", {retr:.0%} of the gap retraced" if retr is not None else "")
            + f"). Re-examine: is the original move being DEBUNKED / was it an overreaction, "
            f"or is this a genuine reversal? Give the side that makes sense NOW (the tape "
            f"currently points to '{target}').")
    return CatalystCtx(symbol=analysis["symbol"], gap=gap,
                       dir=str(analysis.get("dir") or ("down" if gap < 0 else "up")),
                       crowd=analysis.get("crowd"), day_label=_day_label(session),
                       premium_trend_line=line)


# Cost guard: a recall whose grounded read keeps FAILING persists nothing (no call →
# dedup never sets), so without this it would re-call every loop tick while the reversal
# holds. Count attempts per (session, symbol, target) in memory and stop after a few —
# mirrors the catalyst retry cap. Resets on restart (rare inside a session).
_MAX_RECALL_ATTEMPTS = 3
_recall_attempts: dict[tuple[str, str, str], int] = {}


async def run_recall(session: str) -> dict:
    """Phase B: when the intraday tape has REVERSED against the 09:25 read, re-call the
    LLM ONCE (per distinct reversal, capped) to re-read the 'why' against the new tape.
    Premium stays master — this only annotates the catalyst reasoning; it never changes
    the reconciled call. Gated by overnight_llm.recall_enabled (default OFF). Best-effort."""
    try:
        await simmer_config.refresh_setup()
    except Exception:                          # noqa: BLE001
        pass
    ovn = simmer_config.overnight_llm()
    if not ovn.get("recall_enabled"):
        return {"enabled": False}
    analyses = await supabase_admin.list_simmer_overnight_analysis(session) or []
    if not analyses:
        return {"enabled": True, "recalled": 0}
    ov_rows = await supabase_admin.list_simmer_overnight(session) or []
    prem_by = {}
    for r in ov_rows:
        v = dict(r.get("verdict") or {})
        v.update(dir=r.get("dir"), gap=float(r.get("gap") or 0.0), crowd=r.get("crowd"))
        prem_by[str(r.get("symbol") or "").upper()] = v
    drift_cfg = simmer_config.overnight_drift()
    s = get_settings()
    cap = int(ovn.get("recall_cap", 2))
    recalled = 0
    for a in analyses:
        sym = str(a.get("symbol") or "").upper()
        reconciled = reconcile(prem_by.get(sym) or {}, a.get("llm") or {},
                               a.get("combiner") or {}, drift_cfg=drift_cfg)
        target = _recall_target(a, reconciled)
        if not target:
            continue
        prior = a.get("llm_recall") or {}
        if int(prior.get("count", 0)) >= cap:
            continue
        akey = (str(session)[:10], sym, target)
        if _recall_attempts.get(akey, 0) >= _MAX_RECALL_ATTEMPTS:
            continue                           # cost guard: stop retrying a broken recall
        ctx = _recall_ctx({**a, "symbol": sym}, prem_by.get(sym) or {}, target, session)
        try:
            live = await _run_provider(
                _make_llm(ovn.get("provider", "openai"), ovn.get("model", ""), s, ovn), ctx)
        except Exception:                      # noqa: BLE001 — best-effort per name
            log.exception("[recall] failed for %s", sym)
            live = {}
        if not live.get("call"):
            _recall_attempts[akey] = _recall_attempts.get(akey, 0) + 1   # count the failed try
            continue
        _recall_attempts.pop(akey, None)       # success → clear the failure count
        await supabase_admin.upsert_simmer_overnight_analysis({
            "symbol": sym, "session_date": str(session)[:10],
            "llm_recall": {
                "for_call": target, "call": live.get("call"), "sustain": live.get("sustain"),
                "deserved": live.get("deserved"), "sentiment": live.get("web_social_sentiment"),
                "why_moved": live.get("why_moved"), "one_liner": live.get("one_liner"),
                "confidence": live.get("confidence"), "provider": live.get("provider"),
                "at": datetime.now(_ET).isoformat(), "count": int(prior.get("count", 0)) + 1,
            }})
        recalled += 1
    if recalled:
        log.info("[recall] re-validated %d reversal(s)", recalled)
    return {"enabled": True, "recalled": recalled}


async def analyze_one(mv: dict, premium: dict, session: str, s, ovn: dict) -> Optional[dict]:
    """Run live + shadow providers + combiner for one mover. Returns the row to
    persist (llm / llm_shadow / premium / combiner), or None if the live read
    produced nothing. Does not write — the caller persists."""
    from .simmer_llm import CatalystCtx
    premium_line = _premium_trend_line(premium) if ovn.get("premium_context_to_llm", True) else None
    ctx = CatalystCtx(symbol=mv["symbol"], gap=float(mv.get("gap") or 0.0),
                      dir=str(mv.get("dir") or ("up" if float(mv.get("gap") or 0) >= 0 else "down")),
                      crowd=mv.get("crowd"), day_label=_day_label(session),
                      premium_trend_line=premium_line)

    live = await _run_provider(_make_llm(ovn.get("provider", "openai"), ovn.get("model", ""), s, ovn), ctx)
    shadow = {}
    if ovn.get("shadow_provider"):
        shadow = await _run_provider(
            _make_llm(ovn["shadow_provider"], ovn.get("shadow_model", ""), s, ovn), ctx)

    if not live.get("call") and not shadow.get("call"):
        log.info("[catalyst] no usable LLM read for %s", mv["symbol"])
        return None
    # Stamp WHEN the read was made. On a normal day this is ~09:25; on a LATE START
    # (backend up mid-session) it's late, and the close grader uses it to mark the
    # llm_only/combiner calls non-predictive (no pre-open lookahead in the bake-off).
    if live.get("call"):
        live["at"] = datetime.now(_ET).isoformat()
    combiner = _combiner_block(premium, live, ovn.get("decision_mode", "llm_only"))
    return {"symbol": mv["symbol"], "session_date": str(session)[:10],
            "dir": ctx.dir, "gap": ctx.gap, "crowd": mv.get("crowd"),
            "premium": premium, "llm": live, "llm_shadow": shadow, "combiner": combiner}


# Per-name LLM retry cap (cost guard). On a name whose grounded read keeps failing,
# the loop retries every ~60s through the 10:30 window — ~65 passes, each up to 4
# paid calls (live + shadow, 2 steps). Cap attempts per (session, name) so a broken
# name is tried a few times, then left. Kept in memory (resets on a restart, which is
# rare inside the 65-min window) rather than a new schema column.
_MAX_CATALYST_ATTEMPTS = 3
_catalyst_attempts: dict[tuple[str, str], int] = {}


async def run_catalyst(session: str) -> dict:
    """Phase-0 batch: for every overnight row today, run the catalyst analyzer and
    persist to simmer_overnight_analysis. Read-only w.r.t. simmer_overnight. Gated
    by the `overnight_llm.enabled` setting. Returns a small summary."""
    # Read the LATEST settings from the table first, so a change made this morning
    # (provider / model / mode / enabled / shadow) takes effect same-day WITHOUT a
    # restart — best-effort: on a read failure we fall back to the in-memory snapshot.
    try:
        await simmer_config.refresh_setup()
    except Exception:                          # noqa: BLE001
        log.warning("[catalyst] settings refresh failed — using last in-memory snapshot")
    ovn = simmer_config.overnight_llm()
    if not ovn.get("enabled"):
        return {"enabled": False}
    rows = await supabase_admin.list_simmer_overnight(session)
    if not rows:
        return {"enabled": True, "names": 0}
    s = get_settings()
    day = str(session)[:10]
    done = skipped = gave_up = 0
    for row in rows:
        # Restart-safe idempotency: if this name already has a PERSISTED llm read
        # for today, skip it. catalyst_date is in-memory only, so a mid-session
        # restart would otherwise re-run the pass — re-paying for the LLM calls AND
        # overwriting the 09:25-era prediction with one made after the market moved
        # (lookahead bias in the very forward test this exists for).
        prior = await supabase_admin.get_simmer_overnight_analysis(row["symbol"], session)
        if prior and (prior.get("llm") or {}).get("call"):
            skipped += 1
            continue
        # Cost guard: a name whose read keeps failing is retried each tick; stop after
        # a few tries so one broken name can't rack up paid calls all morning.
        akey = (day, row["symbol"])
        if _catalyst_attempts.get(akey, 0) >= _MAX_CATALYST_ATTEMPTS:
            gave_up += 1
            continue
        verdict = row.get("verdict") or {}
        premium = {"call": verdict.get("call"), "premium_trend": verdict.get("premium_trend"),
                   "underlying_trend": verdict.get("underlying_trend"),
                   "confidence": verdict.get("confidence")}
        mv = {"symbol": row["symbol"], "dir": row.get("dir"), "gap": row.get("gap"),
              "crowd": row.get("crowd")}
        try:
            built = await analyze_one(mv, premium, session, s, ovn)
        except Exception:                      # noqa: BLE001 — best-effort per name
            log.exception("[catalyst] analyze failed for %s", row.get("symbol"))
            built = None
        if built:
            await supabase_admin.upsert_simmer_overnight_analysis(built)
            _catalyst_attempts.pop(akey, None)   # success → clear the failure count
            done += 1
        else:
            _catalyst_attempts[akey] = _catalyst_attempts.get(akey, 0) + 1
    log.info("[catalyst] analyzed %d, skipped %d (already done), gave up %d (retry cap) of %d names "
             "(provider=%s mode=%s shadow=%s)",
             done, skipped, gave_up, len(rows), ovn.get("provider"), ovn.get("decision_mode"),
             ovn.get("shadow_provider"))
    # `complete` → nothing left to try: every name is either analyzed (skipped) or has
    # hit the attempt cap. The loop uses this to stop calling run_catalyst for the day
    # (no fixed 10:30 window), so a late start still finishes and a normal day winds down.
    complete = bool(rows) and (skipped + gave_up) >= len(rows)
    return {"enabled": True, "names": len(rows), "analyzed": done, "skipped": skipped,
            "gave_up": gave_up, "complete": complete}


_REACTIVE_HISTORY_CAP = 12       # append-on-change only → a handful of entries/day


def _apply_reactive(existing: dict, scored_final: dict, premium: dict,
                    drift_cfg: dict, *, reactive_lock: bool) -> tuple[dict, list, bool]:
    """Fold the Phase A reactive overlay into a combiner block. Returns
    (premium_reactive, premium_reactive_history, changed).

    The SCORED reactive call (`premium_reactive`) freezes at a checkpoint
    (`reactive_lock`, ~noon ET) and is NEVER re-scored after — so the close grade is
    made before the outcome window, exactly like llm_only (09:25) and
    combiner_blended (10:00). Without this freeze the last-tick call would be graded
    against the 16:00 close and a late flip toward the realized move would be
    credited as 'right' — lookahead that would flatter the reactive candidate in the
    very forward test used to decide whether to ship it.

    `premium_reactive_history` is an append-ON-CHANGE log (not per tick) of the live
    reactive call for observability — bounded, non-predictive; the UI reads its last
    entry for the always-live call. The scored freeze above is what gets graded."""
    live = reactive_call(scored_final, premium, drift_cfg=drift_cfg)
    history = list(existing.get("premium_reactive_history") or [])
    last = history[-1] if history else {}
    changed = (last.get("call"), last.get("source")) != (live["call"], live["source"])
    if changed:
        history.append({"t": datetime.now(_ET).isoformat(), "call": live["call"],
                        "source": live["source"], "gap_retraced": premium.get("gap_retraced"),
                        "skew_shift": premium.get("skew_shift")})
        history = history[-_REACTIVE_HISTORY_CAP:]
    prev_scored = existing.get("premium_reactive") or {}
    if prev_scored.get("decided_at"):
        scored = prev_scored                 # already frozen at the checkpoint — never re-score
    else:
        scored = dict(live)
        if reactive_lock:
            scored["decided_at"] = datetime.now(_ET).isoformat()
            # Spot AT the freeze — the reactive call is graded from here to the close,
            # not from 09:25, so it isn't credited for the pre-decision morning move.
            scored["spot_at"] = premium.get("spot_now")
    scored_changed = (prev_scored.get("call"), prev_scored.get("source"),
                      prev_scored.get("decided_at")) != (scored.get("call"), scored.get("source"),
                                                         scored.get("decided_at"))
    return scored, history, (changed or scored_changed)


async def refresh_combiner(session: str, *, lock: bool = False, reactive_lock: bool = False) -> int:
    """Re-combine each analyzed name against the LIVE premium verdict — WITHOUT any
    new LLM call (the catalyst read is frozen at 09:25, one call/day; only the
    premium engine's verdict moves).

    FAIRNESS: the SCORED decision must be made before the outcome window, and at a
    comparable time to `llm_only` (which is fixed at 09:25). So the combiner updates
    live only until a cutoff, then FREEZES: the watcher passes `lock=True` once past
    ~10:00 ET, which stamps `decided_at` on the combiner block; a frozen combiner is
    never re-scored. The Phase A reactive overlay gets a LATER checkpoint
    (`reactive_lock`, ~noon ET) so it can react through the morning, then it too
    freezes for a lookahead-free grade. Pure math; read-only w.r.t.
    simmer_overnight. Gated by `enabled`."""
    ovn = simmer_config.overnight_llm()
    if not ovn.get("enabled"):
        return 0
    analyses = await supabase_admin.list_simmer_overnight_analysis(session) or []
    if not analyses:
        return 0
    ov_rows = await supabase_admin.list_simmer_overnight(session) or []
    prem_by = {r["symbol"]: (r.get("verdict") or {}) for r in ov_rows}
    mode = ovn.get("decision_mode", "llm_only")
    drift_cfg = simmer_config.overnight_drift()
    updated = 0
    for a in analyses:
        llm = a.get("llm") or {}
        if not llm.get("call"):                # nothing to combine until the LLM read lands
            continue
        v = prem_by.get(a["symbol"]) or {}
        # Carry the LEADING signals (skew_shift, gap_retraced) through to the reactive
        # override, not just the toward-side magnitude.
        premium = {"call": v.get("call"), "premium_trend": v.get("premium_trend"),
                   "underlying_trend": v.get("underlying_trend"),
                   "confidence": v.get("confidence"), "mean_ratio": v.get("mean_ratio"),
                   "skew_shift": v.get("skew_shift"), "gap_retraced": v.get("gap_retraced"),
                   "spot_now": v.get("spot_now")}
        existing = a.get("combiner") or {}
        frozen = bool(existing.get("decided_at"))
        if frozen:
            # The combiner scored call is locked (bake-off fairness), `final` untouched.
            # Only the reactive overlay keeps moving (until ITS own noon freeze).
            scored, history, changed = _apply_reactive(
                existing, existing.get("final") or {}, premium, drift_cfg, reactive_lock=reactive_lock)
            if not changed:
                continue                        # no change → skip the write (no churn)
            comb = {**existing, "premium_reactive": scored, "premium_reactive_history": history}
            await supabase_admin.upsert_simmer_overnight_analysis({
                "symbol": a["symbol"], "session_date": str(session)[:10], "combiner": comb})
            updated += 1
            continue
        comb = _combiner_block(premium, llm, mode)
        if lock:                               # past the cutoff → this is the scored decision
            comb["decided_at"] = datetime.now(_ET).isoformat()
        scored, history, _ = _apply_reactive(
            existing, comb["final"], premium, drift_cfg, reactive_lock=reactive_lock)
        comb["premium_reactive"] = scored
        comb["premium_reactive_history"] = history
        await supabase_admin.upsert_simmer_overnight_analysis({
            "symbol": a["symbol"], "session_date": str(session)[:10],
            "premium": premium, "combiner": comb})
        updated += 1
    return updated


# An LLM read after this ET wall-clock time is a LATE-START read (backend came up
# mid-session), not a genuine pre/at-open prediction — graded but flagged
# non-predictive so it doesn't flatter the forward-test bake-off with lookahead.
_PREDICTIVE_CUTOFF = (9, 45)


def _read_predictive(at_iso: Optional[str]) -> bool:
    """True if the catalyst read was made early enough (≤ ~09:45 ET) to count as a
    pre/at-open prediction. No timestamp (older rows) → assume predictive."""
    if not at_iso:
        return True
    try:
        t = datetime.fromisoformat(str(at_iso))
    except (TypeError, ValueError):
        return True
    if t.tzinfo is not None:
        t = t.astimezone(_ET)
    return (t.hour, t.minute) <= _PREDICTIVE_CUTOFF


# ── Readings + realized outcome (the forward test's scoring half) ────────────
def _outcome(gap: float, spot_premarket: float | None, spot_close: float | None) -> Optional[str]:
    """Map the realized intraday path to extended | held | faded, reusing the
    premium tracker's underlying classifier (retracing → faded)."""
    if not spot_premarket or not spot_close:
        return None
    u = pt.classify_underlying(gap, spot_premarket, spot_close)
    return {"extending": "extended", "holding": "held", "retracing": "faded"}.get(u)


def _direction_right(call: Optional[str], gap: float, spot_premarket: float | None,
                     spot_close: float | None) -> Optional[bool]:
    """Did the logged side match the realized move? bear_call wants the name NOT to
    rally; bull_put wants it NOT to break down (vs the 09:25 level)."""
    if not call or call in ("wait", "skip") or not spot_premarket or not spot_close:
        return None
    move = (spot_close - spot_premarket) / spot_premarket
    if call == "sell_bear_call":
        return move <= 0.005
    if call == "sell_bull_put":
        return move >= -0.005
    return None


async def capture_reading(provider, label: str, session: str) -> int:
    """Snapshot spot + the tracked-strike premiums for each of today's overnight
    names and append `{t:label, spot, premiums}` to its analysis row. At the 16:00
    reading, also fill `close` + the realized `outcome`/`direction_right`. Enabled
    only when the analyzer is on. Best-effort; returns rows touched."""
    if not simmer_config.overnight_llm().get("enabled"):
        return 0
    rows = await supabase_admin.list_simmer_overnight(session) or []
    touched = 0
    for row in rows:
        sym, exp = row["symbol"], row["expiration"]
        existing = await supabase_admin.get_simmer_overnight_analysis(sym, session) or {}
        # Idempotent per label: if this reading already landed today, skip it (and
        # its quote reads) — so a restart never duplicates 09:25/10:00 or re-stamps.
        if any(r.get("t") == label for r in (existing.get("readings") or [])):
            continue
        strikes = row.get("strikes") or []
        side = row.get("side") or pt.toward_move_side(row.get("dir") or "down")
        spot = premiums = None
        try:
            chain = await provider.chain(sym, exp)
            premiums = pt.premium_map(chain, strikes, side) if strikes else {}
            q = await provider.quote(sym)
            spot = q.get("last") if q else None
        except Exception:                      # noqa: BLE001 — best-effort per name
            log.warning("[catalyst] reading %s failed for %s", label, sym)
        readings = list(existing.get("readings") or [])
        readings.append({"t": label, "spot": float(spot) if spot else None, "premiums": premiums or {}})
        patch: dict[str, Any] = {"symbol": sym, "session_date": str(session)[:10], "readings": readings}
        if label == "09:25":
            # self-contained premarket anchor on the analysis row (mirrors
            # simmer_overnight's baseline) so `close` can measure intraday later.
            patch["premarket"] = {"spot": float(spot) if spot else row.get("spot_baseline"),
                                  "gap": float(row.get("gap") or 0.0), "premiums": premiums or {}}
        if label == "16:00":
            pm = (existing.get("premarket") or {}).get("spot")
            gap = float(existing.get("gap") or row.get("gap") or 0.0)
            spot_f = float(spot) if spot else None
            comb = existing.get("combiner") or {}
            # Score EVERY candidate independently, so the forward-test bake-off can
            # compare them no matter which `decision_mode` is set: the frozen 09:25
            # llm_only call, the frozen-at-10:00 combiner_blended call, and the
            # decision_mode `final`. All are calls made before the close.
            llm_only_call = (comb.get("llm_only") or {}).get("call")
            blended_call = (comb.get("combiner_blended") or {}).get("call")
            final_call = (comb.get("final") or {}).get("call")
            # llm_only / combiner_blended claim to be PRE-OPEN (09:25 / 10:00) calls. If
            # the LLM actually read late (a late start), grading them from the premarket
            # spot is lookahead → mark them non-predictive (grade = None) so the bake-off
            # ignores them. The reactive candidate is already graded from its own spot.
            predictive = _read_predictive((existing.get("llm") or {}).get("at"))
            # Phase A reactive candidate — the call FROZEN at the noon checkpoint,
            # graded from the spot AT THAT FREEZE (not 09:25), so it's credited only
            # for the move AFTER it was made, lookahead-free.
            reactive_pr = comb.get("premium_reactive") or {}
            reactive_c = reactive_pr.get("call")
            reactive_from = reactive_pr.get("spot_at") or pm
            # The last live reactive call (post-noon). Graded too but LABELLED
            # non-predictive — a late flip toward the realized move is not a real
            # pre-outcome call, so this must never be read as the reactive score.
            hist = comb.get("premium_reactive_history") or []
            reactive_live_c = hist[-1].get("call") if hist else reactive_c
            patch["close"] = {
                "spot": spot_f,
                "intraday_pct": round((spot_f - pm) / pm * 100, 2) if (spot_f and pm) else None,
                "llm_only_call": llm_only_call,
                "llm_only_right": _direction_right(llm_only_call, gap, pm, spot_f) if predictive else None,
                "combiner_blended_call": blended_call,
                "combiner_blended_right": (_direction_right(blended_call, gap, pm, spot_f)
                                           if predictive else None),
                "late_llm": not predictive,    # read made mid-session → the two above are non-predictive
                # scored (noon-frozen) reactive call — the one the bake-off compares,
                # graded from its freeze spot (spot_at) to the close.
                "premium_reactive_call": reactive_c,
                "premium_reactive_from": reactive_from,
                "premium_reactive_right": _direction_right(reactive_c, gap, reactive_from, spot_f),
                # non-predictive: last live reactive call, for observing the path only.
                "premium_reactive_live_call": reactive_live_c,
                "premium_reactive_live_right_nonpredictive": _direction_right(reactive_live_c, gap, pm, spot_f),
            }
            patch["outcome"] = _outcome(gap, pm, spot_f)
            patch["direction_right"] = _direction_right(final_call, gap, pm, spot_f)
        await supabase_admin.upsert_simmer_overnight_analysis(patch)
        touched += 1
    log.info("[catalyst] reading %s captured for %d names", label, touched)
    return touched
