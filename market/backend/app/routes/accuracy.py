"""Rolling accuracy + regime-shift surface for a symbol.

Reads from:
  - Database (rolling-N accuracy stats + recent outcomes)
  - EvaluatorState (regime alert flag + counters)
  - Settings (pill thresholds)

Returns a single JSON blob the UI polls every 10s.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import get_settings
from ..evaluator import state as evaluator_state

router = APIRouter()


def _tier(pct: float, n: int, green: float, red: float) -> str:
    if n == 0:
        return "unknown"
    if pct >= green:
        return "green"
    if pct <= red:
        return "red"
    return "yellow"


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
    return {
        "symbol": sym,
        "window": int(settings.eval_rolling_window),
        "tier": tier,
        "accuracy_pct": pct,
        "n": n,
        "wins": int(stats.get("wins") or 0),
        "losses": int(stats.get("losses") or 0),
        "neutrals": int(stats.get("neutrals") or 0),
        "regime_alert": bool(evaluator_state.regime_alert_active_by_symbol.get(sym, False)),
        "regime_consec_losses": int(evaluator_state.consec_losses_by_symbol.get(sym, 0)),
        "regime_consec_wins": int(evaluator_state.consec_wins_by_symbol.get(sym, 0)),
        "regime_triggered_at": evaluator_state.regime_alert_triggered_at.get(sym),
        "recent_outcomes": recent,
        "last_evaluated_at": evaluator_state.last_run_at,
        "evaluator_error": evaluator_state.last_error,
    }
