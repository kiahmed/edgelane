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
from .simmer_overnight_combiner import combine

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
    return {"enabled": True, "names": len(rows), "analyzed": done, "skipped": skipped, "gave_up": gave_up}


async def refresh_combiner(session: str, *, lock: bool = False) -> int:
    """Re-combine each analyzed name against the LIVE premium verdict — WITHOUT any
    new LLM call (the catalyst read is frozen at 09:25, one call/day; only the
    premium engine's verdict moves).

    FAIRNESS: the SCORED decision must be made before the outcome window, and at a
    comparable time to `llm_only` (which is fixed at 09:25). So the combiner updates
    live only until a cutoff, then FREEZES: the watcher passes `lock=True` once past
    ~10:00 ET, which stamps `decided_at` on the combiner block; a row already
    carrying `decided_at` is skipped (never re-scored from late-session premiums).
    Pure math; read-only w.r.t. simmer_overnight. Gated by `enabled`."""
    ovn = simmer_config.overnight_llm()
    if not ovn.get("enabled"):
        return 0
    analyses = await supabase_admin.list_simmer_overnight_analysis(session) or []
    if not analyses:
        return 0
    ov_rows = await supabase_admin.list_simmer_overnight(session) or []
    prem_by = {r["symbol"]: (r.get("verdict") or {}) for r in ov_rows}
    mode = ovn.get("decision_mode", "llm_only")
    updated = 0
    for a in analyses:
        if (a.get("combiner") or {}).get("decided_at"):   # decision already frozen
            continue
        llm = a.get("llm") or {}
        if not llm.get("call"):                # nothing to combine until the LLM read lands
            continue
        v = prem_by.get(a["symbol"]) or {}
        premium = {"call": v.get("call"), "premium_trend": v.get("premium_trend"),
                   "underlying_trend": v.get("underlying_trend"), "confidence": v.get("confidence")}
        comb = _combiner_block(premium, llm, mode)
        if lock:                               # past the cutoff → this is the scored decision
            comb["decided_at"] = datetime.now(_ET).isoformat()
        await supabase_admin.upsert_simmer_overnight_analysis({
            "symbol": a["symbol"], "session_date": str(session)[:10],
            "premium": premium, "combiner": comb})
        updated += 1
    return updated


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
            patch["close"] = {
                "spot": spot_f,
                "intraday_pct": round((spot_f - pm) / pm * 100, 2) if (spot_f and pm) else None,
                "llm_only_call": llm_only_call,
                "llm_only_right": _direction_right(llm_only_call, gap, pm, spot_f),
                "combiner_blended_call": blended_call,
                "combiner_blended_right": _direction_right(blended_call, gap, pm, spot_f),
            }
            patch["outcome"] = _outcome(gap, pm, spot_f)
            patch["direction_right"] = _direction_right(final_call, gap, pm, spot_f)
        await supabase_admin.upsert_simmer_overnight_analysis(patch)
        touched += 1
    log.info("[catalyst] reading %s captured for %d names", label, touched)
    return touched
