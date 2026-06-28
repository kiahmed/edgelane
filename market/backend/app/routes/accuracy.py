"""Rolling accuracy + bias-trust surface for a symbol.

Reads from:
  - Database (rolling-N spread-outcome stats + recent outcomes)
  - EvaluatorState (regime alert flag + consec counters)
  - PollerState (latest snapshot → current bias confidence)
  - Settings (pill thresholds + eval_min_graded)

Returns a single JSON blob the UI polls every 10s. The bias-trust fields
(`state`, `win_rate`, `graded`, `wins_to_resume`, `display_text`,
`bias_confidence`, `show_hint`, `hint_text`) are a pinned contract the frontend
renders verbatim — see docs/spread_outcome_eval.md. Legacy fields are kept for
back-compat.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import get_settings
from ..evaluator import state as evaluator_state
from ..poller import state as poller_state

router = APIRouter()

# Confidence framing shown under the pick whenever the bias isn't in sync —
# never "don't trade" (the tool exists to find the winning play).
_HINT_TEXT = "Bias re-syncing — wait for a confirming win before sizing up."


def _tier(pct: float, n: int, green: float, red: float) -> str:
    if n == 0:
        return "unknown"
    if pct >= green:
        return "green"
    if pct <= red:
        return "red"
    return "yellow"


def _latest_bias_confidence(sym: str) -> str | None:
    snap = poller_state.latest_by_symbol.get(sym)
    if not snap:
        return None
    conf = (snap.get("bias") or {}).get("confidence")
    return str(conf).lower() if conf else None


def _trust_state(sym: str, graded: int, pct: float, settings) -> dict:
    """Compute the single bias-trust state + its display strings.

    Precedence: paused (regime alert) → low_conf (bias confidence low) →
    calibrating (too few graded) → in_sync.
    """
    regime_active = bool(evaluator_state.regime_alert_active_by_symbol.get(sym, False))
    consec_wins = int(evaluator_state.consec_wins_by_symbol.get(sym, 0))
    clear_wins = int(getattr(settings, "regime_clear_consec_wins", 2))
    min_graded = int(getattr(settings, "eval_min_graded", 10))
    bias_conf = _latest_bias_confidence(sym)

    wins_to_resume = 0
    if regime_active:
        state = "paused"
        wins_to_resume = max(0, clear_wins - consec_wins)
        plural = "" if wins_to_resume == 1 else "s"
        display_text = (
            f"⏸ recalibrating — bias re-syncing "
            f"({wins_to_resume} confirming win{plural} to resume)"
        )
        win_rate = None
    elif bias_conf == "low":
        state = "low_conf"
        win_rate = pct
        display_text = f"{pct:.0f}% win rate ({graded} graded) — lower-conviction read"
    elif graded < min_graded:
        state = "calibrating"
        win_rate = None
        display_text = f"Calibrating — {graded}/{min_graded} graded"
    else:
        state = "in_sync"
        win_rate = pct
        display_text = f"{pct:.0f}% win rate ({graded} graded)"

    return {
        "state": state,
        "win_rate": win_rate,
        "graded": graded,
        "wins_to_resume": wins_to_resume,
        "display_text": display_text,
        "bias_confidence": bias_conf,
        "show_hint": state != "in_sync",
        "hint_text": _HINT_TEXT,
    }


@router.get("/accuracy/{symbol}")
def get_accuracy(symbol: str):
    sym = symbol.upper()
    # Runtime import — avoids the import cycle main.py ↔ routes at module load.
    from .. import main as _main
    db = getattr(_main, "_db", None)
    if db is None:
        raise HTTPException(503, "db not ready")
    settings = get_settings()
    stats = db.fetch_accuracy(sym, settings.eval_rolling_window)
    recent = db.fetch_recent_outcomes(sym, 10)
    pct = float(stats.get("accuracy_pct") or 0.0)
    n = int(stats.get("n") or 0)
    tier = _tier(pct, n, float(settings.pill_green_pct), float(settings.pill_red_pct))
    trust = _trust_state(sym, n, pct, settings)
    return {
        "symbol": sym,
        "window": int(settings.eval_rolling_window),
        "tier": tier,
        "accuracy_pct": pct,
        "n": n,
        "wins": int(stats.get("wins") or 0),
        "losses": int(stats.get("losses") or 0),
        "neutrals": int(stats.get("neutrals") or 0),
        # ── Pinned bias-trust contract (frontend renders these verbatim) ──
        "state": trust["state"],
        "win_rate": trust["win_rate"],
        "graded": trust["graded"],
        "wins_to_resume": trust["wins_to_resume"],
        "display_text": trust["display_text"],
        "bias_confidence": trust["bias_confidence"],
        "show_hint": trust["show_hint"],
        "hint_text": trust["hint_text"],
        # ── Legacy regime fields (kept for back-compat) ──
        "regime_alert": bool(evaluator_state.regime_alert_active_by_symbol.get(sym, False)),
        "regime_consec_losses": int(evaluator_state.consec_losses_by_symbol.get(sym, 0)),
        "regime_consec_wins": int(evaluator_state.consec_wins_by_symbol.get(sym, 0)),
        "regime_triggered_at": evaluator_state.regime_alert_triggered_at.get(sym),
        "recent_outcomes": recent,
        "last_evaluated_at": evaluator_state.last_run_at,
        "evaluator_error": evaluator_state.last_error,
    }
