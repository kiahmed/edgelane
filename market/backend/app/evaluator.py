"""3-minute outcome evaluator for EdgeLane MARKET.

Started by main.py lifespan as a background asyncio task. Every 30s it sweeps
`bias_decisions` looking for rows that:
  - Are older than `eval_window_min` (default 3 minutes)
  - Have no matching `outcomes` row yet

For each pending decision:
  1. Read current spot from PollerState (in-memory cache, refreshed every
     POLL_INTERVAL_SEC by the poll loop).
  2. Compute actual_move_pct = (spot_at_eval - spot_at_decision) / spot_at_decision * 100.
  3. Derive predicted_direction from bias_decision.label:
        bullish, mild_bullish   → 'up'
        bearish, mild_bearish   → 'down'
        neutral                 → 'neutral'
  4. Resolve win/loss using `neutral_band_pct` as the dead-zone:
        up:      win if move >  +band, loss if move <  -band, else neutral
        down:    win if move <  -band, loss if move >  +band, else neutral
        neutral: win if |move| <= band, else loss
  5. Insert outcomes row.
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
import logging
from datetime import datetime, timezone

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


def _resolve_result(direction: str, move_pct: float, band: float) -> str:
    """Tiered win/loss/neutral classifier.

    `band` is the symmetric dead-zone in percent (e.g. 0.05 = ±5bps).
    """
    if direction == "up":
        if move_pct > band:
            return "win"
        if move_pct < -band:
            return "loss"
        return "neutral"
    if direction == "down":
        if move_pct < -band:
            return "win"
        if move_pct > band:
            return "loss"
        return "neutral"
    # neutral prediction: win if price barely moved
    return "win" if abs(move_pct) <= band else "loss"


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


async def evaluate_pending(db, poller_state, settings) -> int:
    """Single sweep over pending decisions. Returns count evaluated."""
    pending = await asyncio.to_thread(
        db.fetch_pending_evaluations, settings.eval_window_min
    )
    if not pending:
        return 0

    band = float(getattr(settings, "neutral_band_pct", 0.05))
    n = 0
    for row in pending:
        # tuple shape: (id, ts, symbol, spot_at_decision, label, score)
        decision_id, ts_decision, symbol, spot_at_decision, label, _score = row
        snap = poller_state.latest_by_symbol.get(symbol)
        if not snap:
            # Poller hasn't produced a snapshot for this symbol yet — skip,
            # we'll retry on the next sweep.
            continue
        try:
            spot_at_eval = float(snap.get("spot") or 0)
        except (TypeError, ValueError):
            spot_at_eval = 0.0
        if not spot_at_eval or not spot_at_decision:
            continue

        spot_at_decision = float(spot_at_decision)
        move_pct = (spot_at_eval - spot_at_decision) / spot_at_decision * 100.0
        direction = _predict_direction(label)
        result = _resolve_result(direction, move_pct, band)

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
                "actual_move_pct": round(move_pct, 4),
                "result": result,
            })
        except Exception:
            log.exception("insert_outcome failed for decision_id=%s", decision_id)
            continue

        _update_regime(symbol, result, settings)
        n += 1
        log.info(
            "outcome decision_id=%s sym=%s label=%s dir=%s move=%.4f%% result=%s",
            decision_id, symbol, label, direction, move_pct, result,
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
