"""3-minute outcome evaluator for EdgeLane MARKET.

Started by main.py lifespan as a background asyncio task. Every 30s it sweeps
`bias_decisions` looking for rows that:
  - Are older than `eval_window_min` (default 3 minutes)
  - Carry a saved engine pick (pick_legs IS NOT NULL)
  - Have no matching `outcomes` row yet

Grading is spread-outcome based (see docs/spread_outcome_eval.md), NOT spot
direction. For each pending decision:
  1. Re-price the SAME saved pick legs against the current cached snapshot
     (poller_state.latest_by_symbol[symbol]["_quote_by_symbol"]).
  2. favorable delta = premium change in the spread's profit direction
        (debit wins as it expands; credit wins as it decays toward 0).
  3. friction band = ½ × the spread's current bid/ask package width (noise floor).
  4. result: fav > +friction → win; fav < −friction → loss; else neutral.
     (Spot move is still computed as context metadata only.)
  5. Insert outcomes row (with the premium-delta columns).
  6. Update per-symbol regime counters:
        - On 'loss': consec_losses++, consec_wins=0
        - On 'win':  consec_wins++,   consec_losses=0
        - On 'neutral': leave both counters alone (regime can't trigger on noise)
        - When consec_losses >= regime_alert_consec_losses → alert ON
        - When alert is ON and consec_wins >= regime_clear_consec_wins → alert OFF

State is exposed via module-level `state` (an EvaluatorState) for the
/accuracy route + UI banner.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Trading-session timezone; the regime streak resets at the ET day boundary
# (matches the market-hours clock the poller uses).
_SESSION_TZ = ZoneInfo("America/New_York")

# Data-quality gate for the end-of-day rollup. A day is `complete` only if it
# covers most of the RTH session with no big polling gaps — partial days (backend
# left off, frontend never launched) are still archived but flagged incomplete so
# modeling can require full sessions.
_SESSION_MINUTES = 390.0          # 09:30–16:00 ET
_MAX_GAP_MIN = 15.0               # a gap this big = backend was down mid-session
_MIN_COVERAGE = 0.75             # first→last decision must span ≥75% of the session

log = logging.getLogger("edgelane.market.evaluator")


class EvaluatorState:
    """Per-process singleton, mirrored back through /accuracy/{symbol}."""

    def __init__(self) -> None:
        self.last_run_at: str | None = None
        self.evaluated_count: int = 0
        # Per-symbol regime tracking
        self.consec_losses_by_symbol: dict[str, int] = {}
        self.consec_wins_by_symbol: dict[str, int] = {}
        self.regime_alert_active_by_symbol: dict[str, bool] = {}
        self.regime_alert_triggered_at: dict[str, str] = {}
        self.last_error: str | None = None
        # ET date (isoformat) of the last end-of-day archival check.
        self.last_archive_date: str | None = None


state = EvaluatorState()


# ── label → direction mapping ──────────────────────────────────────────────
_LABEL_TO_DIRECTION = {
    "bullish":      "up",
    "mild_bullish": "up",
    "bearish":      "down",
    "mild_bearish": "down",
    "neutral":      "neutral",
}


def _predict_direction(label: str | None) -> str:
    if not label:
        return "neutral"
    return _LABEL_TO_DIRECTION.get(label.lower(), "neutral")


# Tiny non-zero friction floor used when the live bid/ask width can't be
# recovered, so a clear mid move still scores instead of silently neutralizing.
_FRICTION_EPSILON = 1e-6


def _reprice_spread(legs: list[dict], quote_map: dict) -> tuple[float, float] | None:
    """Re-mark the saved pick legs against a current quote map.

    Returns `(signed_open, friction_band)` or None if any leg lacks a usable
    mid (caller skips → retried next sweep).

    `signed_open` = Σ long_short_i · mid_i — the signed cost to OPEN the spread:
    positive = net debit paid, negative = net credit collected. This matches the
    builders' net-premium math (debit verticals/flies: +long −short = debit;
    credit verticals/condors: stored net_premium = −signed_open).

    `friction_band` = ½ · Σ |long_short_i| · (ask_i − bid_i) — half the package's
    summed per-leg bid/ask width (the 2× butterfly body counts twice via
    |long_short|). Falls back to a tiny epsilon when no width is recoverable.
    """
    if not legs:
        return None
    signed = 0.0
    width = 0.0
    have_width = False
    for leg in legs:
        sym = leg.get("symbol")
        q = quote_map.get(sym) if sym else None
        if not q:
            return None
        mid = q.get("mid")
        if mid is None:
            return None
        ls = float(leg.get("long_short") or 0)
        signed += ls * float(mid)
        try:
            spread_w = float(q.get("ask") or 0.0) - float(q.get("bid") or 0.0)
        except (TypeError, ValueError):
            spread_w = 0.0
        if spread_w > 0:
            width += abs(ls) * spread_w
            have_width = True
    friction = 0.5 * width if have_width else _FRICTION_EPSILON
    return signed, friction


def _grade_spread(spread_type: str, entry_mid: float, legs: list[dict],
                  quote_map: dict) -> tuple[float, float, float, str] | None:
    """Grade the saved pick by its own net premium moving in its favor.

    Returns `(net_now, favorable_delta, friction_band, result)` or None when the
    legs can't be re-priced from this snapshot.

    favorable delta = premium change in the spread's PROFIT direction:
      - debit  (paid to open): wins as the spread expands → fav = net_now − entry
      - credit (collected):    wins as it decays toward 0 → fav = entry − net_now
    result: fav > +friction → win; fav < −friction → loss; else neutral.
    """
    rp = _reprice_spread(legs, quote_map)
    if rp is None:
        return None
    signed, friction = rp
    if (spread_type or "").lower() == "credit":
        net_now = -signed
        fav = entry_mid - net_now
    else:  # debit (default)
        net_now = signed
        fav = net_now - entry_mid
    if fav > friction:
        result = "win"
    elif fav < -friction:
        result = "loss"
    else:
        result = "neutral"
    return net_now, fav, friction, result


def _update_regime(symbol: str, result: str, settings) -> None:
    """Adjust per-symbol consec counters + flip the alert flag."""
    if result == "loss":
        state.consec_losses_by_symbol[symbol] = state.consec_losses_by_symbol.get(symbol, 0) + 1
        state.consec_wins_by_symbol[symbol] = 0
        threshold = int(getattr(settings, "regime_alert_consec_losses", 3))
        if state.consec_losses_by_symbol[symbol] >= threshold and \
           not state.regime_alert_active_by_symbol.get(symbol, False):
            state.regime_alert_active_by_symbol[symbol] = True
            state.regime_alert_triggered_at[symbol] = datetime.now(timezone.utc).isoformat()
            log.warning(
                "regime alert ACTIVE for %s — %d consecutive losses",
                symbol, state.consec_losses_by_symbol[symbol],
            )
    elif result == "win":
        state.consec_wins_by_symbol[symbol] = state.consec_wins_by_symbol.get(symbol, 0) + 1
        state.consec_losses_by_symbol[symbol] = 0
        clear_threshold = int(getattr(settings, "regime_clear_consec_wins", 2))
        if state.regime_alert_active_by_symbol.get(symbol, False) and \
           state.consec_wins_by_symbol[symbol] >= clear_threshold:
            state.regime_alert_active_by_symbol[symbol] = False
            state.regime_alert_triggered_at.pop(symbol, None)
            log.info(
                "regime alert CLEARED for %s — %d consecutive wins",
                symbol, state.consec_wins_by_symbol[symbol],
            )
    # 'neutral' result: counters untouched (noise shouldn't trigger/clear regime)


def rehydrate_regime(db, settings) -> None:
    """Rebuild the in-memory regime counters from the outcomes table on startup.

    The consec-loss/win counters + alert flag live only on the `state` singleton
    (in memory), and are advanced solely when a NEW decision is graded. A process
    restart therefore wipes an active pause / loss streak, and it is never rebuilt
    from history — so after a redeploy the paused state silently resets to zero and
    the win-rate gets published again mid-meltdown. This replays each ticker's
    recent graded outcomes (oldest→newest) through the same state machine as
    `_update_regime`, per symbol, to restore the pre-restart position. Runtime
    maintenance (increment on grade, reset on win, clear after N wins) is unchanged
    — this only closes the restart gap.

    Scoped to the CURRENT trading session (outcomes since ET midnight today):
    yesterday's streak is a different market regime and must not carry over. So a
    restart mid-session restores the live streak, but the first boot of a new day
    starts clean. Strategy-agnostic by design — the streak tracks the engine's top
    pick regardless of which structure it wore (a bias-trust signal, not a
    per-strategy edge). Best-effort: never blocks startup.
    """
    alert_thresh = int(getattr(settings, "regime_alert_consec_losses", 3))
    clear_thresh = int(getattr(settings, "regime_clear_consec_wins", 2))
    session_start = datetime.now(_SESSION_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    try:
        rows = db.fetch_regime_replay(since=session_start)
    except Exception:
        log.exception("regime rehydrate: fetch failed; starting with empty counters")
        return

    # Group results per symbol (already ordered symbol ASC, oldest→newest).
    by_symbol: dict[str, list[str]] = {}
    for symbol, result in rows:
        by_symbol.setdefault(symbol, []).append(result)

    restored = 0
    for symbol, results in by_symbol.items():
        cl = cw = 0
        alert = False
        for result in results:
            if result == "loss":
                cl += 1
                cw = 0
                if cl >= alert_thresh:
                    alert = True
            elif result == "win":
                cw += 1
                cl = 0
                if alert and cw >= clear_thresh:
                    alert = False
            # 'neutral': counters untouched (mirrors _update_regime)
        state.consec_losses_by_symbol[symbol] = cl
        state.consec_wins_by_symbol[symbol] = cw
        state.regime_alert_active_by_symbol[symbol] = alert
        if alert:
            restored += 1
            log.warning(
                "regime rehydrate: %s restored PAUSED (%d consec losses, %d/%d "
                "confirming wins to resume)", symbol, cl, cw, clear_thresh,
            )
        else:
            log.info("regime rehydrate: %s consec_losses=%d consec_wins=%d", symbol, cl, cw)
    log.info("regime rehydrate complete: %d symbol(s), %d paused", len(by_symbol), restored)


def archive_completed_days(db) -> int:
    """Roll up each COMPLETED trading day (ET) into outcome_daily_summary once.

    Runs at startup and whenever the ET day rolls over (see evaluator_loop). Only
    days strictly before today are archived (today is still live); days already
    summarized are skipped (idempotent). Raw rows are never deleted — this is a
    deduped, modeling-friendly daily record with a data-quality flag.

    Each (ET date, symbol) gets win/loss/neutral counts, accuracy, session span,
    the largest polling gap, and `complete` = covered ≥75% of the RTH session with
    no gap > 15 min. Partial days (backend was off / frontend never launched) are
    kept but flagged incomplete. Returns the number of (day,symbol) rows written.
    """
    today_start = datetime.now(_SESSION_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    try:
        rows = db.fetch_graded_for_archive(today_start)
        already = db.summarized_pairs()
    except Exception:
        log.exception("daily archive: fetch failed; skipping")
        return 0

    # Bucket (symbol, ts, result) into ET calendar days.
    buckets: dict[tuple, list] = {}
    for symbol, ts, result in rows:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        et_date = ts.astimezone(_SESSION_TZ).date().isoformat()
        buckets.setdefault((et_date, symbol), []).append((ts, result))

    written = 0
    for (et_date, symbol), items in buckets.items():
        if (et_date, symbol) in already:
            continue
        items.sort(key=lambda x: x[0])
        results = [r for _, r in items]
        wins = results.count("win")
        losses = results.count("loss")
        neutrals = results.count("neutral")
        n = len(results)
        first_ts = items[0][0]
        last_ts = items[-1][0]
        span_min = (last_ts - first_ts).total_seconds() / 60.0
        max_gap_min = 0.0
        for (a_ts, _), (b_ts, _) in zip(items, items[1:]):
            gap = (b_ts - a_ts).total_seconds() / 60.0
            if gap > max_gap_min:
                max_gap_min = gap
        coverage_pct = min(1.0, span_min / _SESSION_MINUTES)
        complete = bool(coverage_pct >= _MIN_COVERAGE and max_gap_min <= _MAX_GAP_MIN)
        try:
            db.upsert_daily_summary({
                "session_date": et_date,
                "symbol": symbol,
                "n": n, "wins": wins, "losses": losses, "neutrals": neutrals,
                "accuracy_pct": round(wins / n * 100.0, 1) if n else 0.0,
                "first_ts": first_ts.astimezone(timezone.utc).replace(tzinfo=None),
                "last_ts": last_ts.astimezone(timezone.utc).replace(tzinfo=None),
                "span_min": round(span_min, 1),
                "max_gap_min": round(max_gap_min, 1),
                "coverage_pct": round(coverage_pct, 3),
                "complete": complete,
                "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
            })
            written += 1
            log.info(
                "daily archive %s %s: n=%d w=%d l=%d nu=%d cov=%.0f%% maxgap=%.1fm complete=%s",
                et_date, symbol, n, wins, losses, neutrals,
                coverage_pct * 100, max_gap_min, complete,
            )
        except Exception:
            log.exception("daily archive: upsert failed for %s %s", et_date, symbol)
    return written


async def evaluate_pending(db, poller_state, settings) -> int:
    """Single sweep over pending decisions. Returns count evaluated.

    Grades each ripe decision that carries a saved engine pick by re-pricing the
    SAME legs against the current cached snapshot (spread-outcome eval — see
    docs/spread_outcome_eval.md). Decisions with no pick never reach here
    (filtered out by fetch_pending_evaluations). Spot move is kept only as
    context metadata; it no longer decides win/loss.
    """
    pending = await asyncio.to_thread(
        db.fetch_pending_evaluations, settings.eval_window_min
    )
    if not pending:
        return 0

    n = 0
    for row in pending:
        # tuple shape: (id, ts, symbol, spot_at_decision, label, score,
        #               pick_legs, pick_entry_mid, pick_spread_type, pick_strategy)
        (decision_id, ts_decision, symbol, spot_at_decision, label, _score,
         pick_legs_json, pick_entry_mid, pick_spread_type, _pick_strategy) = row

        snap = poller_state.latest_by_symbol.get(symbol)
        if not snap:
            # Poller hasn't produced a snapshot for this symbol yet — retry next sweep.
            continue
        if pick_legs_json is None or pick_entry_mid is None:
            # Should not happen (SQL filters NULL picks) — skip defensively.
            continue

        try:
            legs = json.loads(pick_legs_json)
        except (TypeError, ValueError):
            log.warning("bad pick_legs JSON for decision_id=%s", decision_id)
            continue

        quote_map = snap.get("_quote_by_symbol") or {}
        graded = _grade_spread(pick_spread_type, float(pick_entry_mid), legs, quote_map)
        if graded is None:
            # Couldn't re-price the exact legs from this snapshot (rotated
            # expiration / missing quotes) — leave un-evaluated, retry next sweep.
            continue
        net_now, fav, friction, result = graded

        # Spot move retained as context only (not the scoring basis).
        try:
            spot_at_eval = float(snap.get("spot") or 0) or float(spot_at_decision or 0)
        except (TypeError, ValueError):
            spot_at_eval = float(spot_at_decision or 0)
        direction = _predict_direction(label)
        move_pct = None
        if spot_at_decision and spot_at_eval:
            move_pct = round((spot_at_eval - float(spot_at_decision)) / float(spot_at_decision) * 100.0, 4)

        evaluated_at = datetime.now(timezone.utc)
        # ts_decision may be a datetime (DuckDB) — guard either way.
        try:
            elapsed = (evaluated_at - ts_decision).total_seconds() / 60.0
        except Exception:
            elapsed = float(settings.eval_window_min)

        try:
            await asyncio.to_thread(db.insert_outcome, {
                "decision_id": int(decision_id),
                "evaluated_at": evaluated_at,
                "spot_at_eval": spot_at_eval,
                "elapsed_minutes": round(elapsed, 3),
                "predicted_direction": direction,
                "actual_move_pct": move_pct,
                "result": result,
                "entry_net_premium": round(float(pick_entry_mid), 4),
                "eval_net_premium": round(net_now, 4),
                "favorable_delta": round(fav, 4),
                "friction_band": round(friction, 4),
                "spread_type": pick_spread_type,
            })
        except Exception:
            log.exception("insert_outcome failed for decision_id=%s", decision_id)
            continue

        _update_regime(symbol, result, settings)
        n += 1
        log.info(
            "outcome decision_id=%s sym=%s strat=%s type=%s entry=%.3f now=%.3f "
            "fav=%.3f friction=%.3f result=%s",
            decision_id, symbol, _pick_strategy, pick_spread_type,
            float(pick_entry_mid), net_now, fav, friction, result,
        )
    return n


async def evaluator_loop(db, poller_state, settings) -> None:
    """Run forever. Sleep 30s between sweeps regardless of work."""
    log.info("evaluator loop started; window=%dm band=%.4f%% alert_after=%dL clear_after=%dW",
             int(settings.eval_window_min),
             float(getattr(settings, "neutral_band_pct", 0.05)),
             int(getattr(settings, "regime_alert_consec_losses", 3)),
             int(getattr(settings, "regime_clear_consec_wins", 2)))
    try:
        while True:
            try:
                # End-of-day rollup: runs on the FIRST sweep (startup catch-up)
                # and whenever the ET day rolls over — BEFORE the market-open
                # guard below, because archival happens once the session is over
                # (market closed). Archives only completed (past) days; idempotent.
                today_et = datetime.now(_SESSION_TZ).date().isoformat()
                if state.last_archive_date != today_et:
                    written = await asyncio.to_thread(archive_completed_days, db)
                    if written:
                        log.info("daily archive: wrote %d day/symbol summaries", written)
                    state.last_archive_date = today_et

                # Self-eval is paused while the market is closed: prices are
                # frozen, so any "outcome" would be manufactured noise. The
                # poller leaves evaluation_active False during display-only
                # off-hours polling.
                if not getattr(poller_state, "evaluation_active", poller_state.market_open):
                    await asyncio.sleep(30)
                    continue
                n = await evaluate_pending(db, poller_state, settings)
                if n:
                    log.info("evaluated %d outcomes this sweep", n)
                state.last_run_at = datetime.now(timezone.utc).isoformat()
                state.evaluated_count += n
                state.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("evaluator sweep failed")
                state.last_error = f"{type(e).__name__}: {e}"
            await asyncio.sleep(30)
    finally:
        log.info("evaluator loop stopped")
