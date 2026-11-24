"""Matrix self-eval from LEVEL grading (docs/level_engine.md §3.4, §12; contract C3).

With `MATRIX_DECISION_SOURCE=levels` the legacy evaluator does not grade. Every
surface that shows or posts a track record — `/accuracy/SPX`, the
`/matrix/state` win_eval / bias blocks, the snap cards, the ledger rows,
matrix_signals' trust / win-rate / pick_result / daily_recap and the daily
email — reads the level engine's binary leg grades instead, through the read
API:

    GET /levels/stats/SPX?limit=N   newest-closed-first legs + counts (C3)
    GET /levels/day/{date}          that ET day's raw rows (daily email / recap)

Both go through `level_pick.get_upstream` (bare GET, ~1.5 s bound). Nothing here
raises: an unreachable engine is `None`, and the payload builders turn that
into an explicit "level engine unavailable" — never legacy numbers.

`accuracy_payload` reproduces the `/accuracy/{symbol}` contract key-for-key so
the UI cannot tell which engine graded: wins / losses from the leg sequence
(binary — `neutrals` is always 0), the SAME `_trust_state` thresholds, and the
regime alert replayed from today's legs with the legacy regime thresholds.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import level_pick, level_view

log = logging.getLogger("edgelane.market.level_accuracy")

_SESSION_TZ = ZoneInfo("America/New_York")
LEVELS_SYMBOL = level_view.LEVELS_SYMBOL
CACHE_TTL_SEC = 10.0
DEFAULT_LIMIT = 50
RECENT_N = 10
UNAVAILABLE = "level engine unavailable"

# limit -> (monotonic ts, stats | None)
_cache: dict[int, tuple[float, dict | None]] = {}
_last_good: dict | None = None


def clear_cache() -> None:
    global _last_good
    _cache.clear()
    _last_good = None


def levels_mode(settings: Any) -> bool:
    try:
        return level_view.levels_mode(settings)
    except Exception:
        return False


def applies(settings: Any, symbol: str | None) -> bool:
    """Level grading replaces the legacy grader for this symbol."""
    return (symbol or "").upper() == LEVELS_SYMBOL and levels_mode(settings)


def _limit(settings: Any, limit: int | None) -> int:
    try:
        want = int(limit if limit is not None else DEFAULT_LIMIT)
    except (TypeError, ValueError):
        want = DEFAULT_LIMIT
    try:
        want = max(want, int(getattr(settings, "eval_rolling_window", 0) or 0))
    except (TypeError, ValueError):
        pass
    return max(1, min(200, want))


async def level_stats(settings: Any, limit: int | None = DEFAULT_LIMIT, *,
                      max_age: float = CACHE_TTL_SEC) -> dict | None:
    """The C3 stats body, or None when the engine is unreachable / answers
    badly. Cached (None included) for `max_age` seconds. Never raises."""
    global _last_good
    try:
        n = _limit(settings, limit)
        now = time.monotonic()
        hit = _cache.get(n)
        if max_age > 0 and hit is not None and now - hit[0] < max_age:
            return hit[1]
        body: dict | None = None
        try:
            status, raw = await level_pick.get_upstream(
                settings, f"/levels/stats/{LEVELS_SYMBOL}", {"limit": n})
            if status == 200 and isinstance(raw, dict) and isinstance(raw.get("legs"), list):
                body = raw
        except level_pick.LevelEngineUnavailable as e:
            log.debug("level stats unavailable: %s", e)
        _cache[n] = (time.monotonic(), body)
        if body is not None:
            _last_good = body
        return body
    except Exception:
        log.exception("level_stats failed")
        return None


def cached_stats() -> dict | None:
    """The last good stats body this process fetched (sync contexts — the
    ledger row builder runs in a thread). None before the first good fetch."""
    return _last_good


async def level_day(settings: Any, day: date | str) -> dict | None:
    """`/levels/day/{date}` body, or None. Uncached (once a day). Never raises."""
    try:
        d = day.isoformat() if isinstance(day, date) else str(day)
        date.fromisoformat(d)                      # fixed template: a real date only
        status, raw = await level_pick.get_upstream(settings, f"/levels/day/{d}")
        if status == 200 and isinstance(raw, dict):
            return raw
    except level_pick.LevelEngineUnavailable as e:
        log.debug("level day unavailable: %s", e)
    except Exception:
        log.exception("level_day failed")
    return None


# ── legs ────────────────────────────────────────────────────────────────────

def normalize_result(result: Any) -> str | None:
    """Binary (C2). Pre-binary rows ('flipped' / 'unresolved') are losses — a
    flip or a session close without a hit. Open legs → None."""
    r = str(result or "").strip().lower()
    if r == "win":
        return "win"
    if r in ("loss", "flipped", "unresolved"):
        return "loss"
    return None


def normalize_leg(row: dict) -> dict:
    """A leg in the C3 stats shape, from either a stats leg or a raw
    `/levels/day` lvl_legs row (whose strategy / label / pick_id live in the
    `pick` jsonb). Never raises on odd input."""
    row = dict(row or {})
    pick = row.get("pick") if isinstance(row.get("pick"), dict) else {}
    for k in ("pick_id", "strategy", "label", "structure_text", "spread_type"):
        if row.get(k) is None and pick.get(k) is not None:
            row[k] = pick.get(k)
    for k in ("target_level", "invalidation_level"):
        if row.get(k) is None and isinstance(pick.get(k), dict):
            row[k] = pick.get(k)
    row["result"] = normalize_result(row.get("result"))
    return row


def closed_legs(stats: dict | None) -> list[dict]:
    """Graded legs, newest-closed first (the order C3 serves them)."""
    out = []
    for leg in (stats or {}).get("legs") or []:
        if not isinstance(leg, dict):
            continue
        n = normalize_leg(leg)
        if n.get("result") in ("win", "loss"):
            out.append(n)
    return out


def _parse_ts(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _leg_day(leg: dict) -> str | None:
    td = leg.get("trade_date")
    if td:
        return str(td)[:10]
    ts = _parse_ts(leg.get("opened_at"))
    return ts.astimezone(_SESSION_TZ).date().isoformat() if ts else None


def today_et() -> str:
    return datetime.now(_SESSION_TZ).date().isoformat()


def replay_regime(legs_newest_first: list[dict], settings: Any,
                  session_date: str | None = None) -> dict:
    """The legacy regime state machine (evaluator._update_regime /
    rehydrate_regime) replayed over the session's level legs, oldest→newest:
    N losses in a row → alert; while alerted, M wins in a row → clear. Scoped
    to the current ET session like the legacy rehydrate (yesterday's streak is
    a different regime)."""
    alert_thresh = int(getattr(settings, "regime_alert_consec_losses", 3))
    clear_thresh = int(getattr(settings, "regime_clear_consec_wins", 2))
    day = session_date or today_et()
    seq = [l for l in legs_newest_first if _leg_day(l) == day]
    seq.sort(key=lambda l: (_parse_ts(l.get("closed_at")) or datetime.min.replace(tzinfo=timezone.utc)))
    cl = cw = 0
    alert = False
    triggered_at = None
    for leg in seq:
        r = leg.get("result")
        if r == "loss":
            cl += 1
            cw = 0
            if cl >= alert_thresh and not alert:
                alert = True
                triggered_at = leg.get("closed_at")
        elif r == "win":
            cw += 1
            cl = 0
            if alert and cw >= clear_thresh:
                alert = False
                triggered_at = None
    return {"alert": alert, "consec_losses": cl, "consec_wins": cw,
            "triggered_at": triggered_at}


def _window(settings: Any) -> int:
    try:
        return max(1, int(getattr(settings, "eval_rolling_window", 20) or 20))
    except (TypeError, ValueError):
        return 20


def window_counts(stats: dict | None, settings: Any) -> dict:
    """wins / losses / pct over the newest `eval_rolling_window` graded legs —
    the same rolling window the legacy /accuracy uses. Binary: neutrals = 0."""
    legs = closed_legs(stats)[:_window(settings)]
    wins = sum(1 for l in legs if l["result"] == "win")
    losses = len(legs) - wins
    n = len(legs)
    return {"n": n, "wins": wins, "losses": losses, "neutrals": 0,
            "accuracy_pct": round(wins / n * 100.0, 1) if n else 0.0}


def _move_pct(entry: Any, exit_: Any) -> float | None:
    try:
        e, x = float(entry), float(exit_)
    except (TypeError, ValueError):
        return None
    if not e:
        return None
    return round((x - e) / e * 100.0, 4)


def recent_outcome_row(leg: dict) -> dict:
    """One `recent_outcomes` row (legacy keys first, level extras after)."""
    label = leg.get("label") or leg.get("structure_text")
    return {
        "id": leg.get("id"),
        "ts": leg.get("opened_at"),
        "label": label,
        "spot_at_decision": leg.get("entry_spot"),
        "evaluated_at": leg.get("closed_at"),
        "spot_at_eval": leg.get("exit_spot"),
        "actual_move_pct": _move_pct(leg.get("entry_spot"), leg.get("exit_spot")),
        "predicted_direction": leg.get("call"),
        "result": leg.get("result"),
        "pick_strategy": leg.get("strategy"),
        "spread_type": leg.get("spread_type"),
        "entry_net_premium": None,
        "eval_net_premium": None,
        "favorable_delta": leg.get("strategy_pnl"),
        "friction_band": None,
        # level extras
        "close_reason": leg.get("close_reason"),
        "target_level": leg.get("target_level"),
        "progress_pct": leg.get("progress_pct"),
        "pick_id": leg.get("pick_id"),
    }


def _settings(settings: Any) -> Any:
    if settings is not None:
        return settings
    from .config import get_settings
    return get_settings()


def accuracy_payload(stats: dict | None, settings: Any = None,
                     symbol: str = LEVELS_SYMBOL) -> dict:
    """The exact `/accuracy/{symbol}` contract, built from level legs. `stats`
    None (engine unreachable) → n=0 and evaluator_error="level engine
    unavailable" — never legacy numbers."""
    from .routes.accuracy import _tier, _trust_state

    settings = _settings(settings)
    sym = (symbol or LEVELS_SYMBOL).upper()
    counts = window_counts(stats, settings)
    if stats is None:
        regime = {"alert": False, "consec_losses": 0, "consec_wins": 0, "triggered_at": None}
    else:
        regime = replay_regime(closed_legs(stats), settings)
    n, pct = counts["n"], counts["accuracy_pct"]
    tier = _tier(pct, n, float(settings.pill_green_pct), float(settings.pill_red_pct))
    trust = _trust_state(sym, n, pct, settings, regime_active=regime["alert"],
                         consec_wins=regime["consec_wins"])
    if stats is None:
        trust["display_text"] = "Level engine unavailable"
        trust["show_hint"] = True
    recent = [recent_outcome_row(l) for l in closed_legs(stats)[:RECENT_N]]
    return {
        "symbol": sym,
        "window": _window(settings),
        "tier": tier,
        "accuracy_pct": pct,
        "n": n,
        "wins": counts["wins"],
        "losses": counts["losses"],
        "neutrals": 0,
        "state": trust["state"],
        "win_rate": trust["win_rate"],
        "graded": trust["graded"],
        "wins_to_resume": trust["wins_to_resume"],
        "display_text": trust["display_text"],
        "bias_confidence": trust["bias_confidence"],
        "show_hint": trust["show_hint"],
        "hint_text": trust["hint_text"],
        "regime_alert": bool(regime["alert"]),
        "regime_consec_losses": int(regime["consec_losses"]),
        "regime_consec_wins": int(regime["consec_wins"]),
        "regime_triggered_at": regime["triggered_at"],
        "recent_outcomes": recent,
        "last_evaluated_at": (stats or {}).get("as_of"),
        "evaluator_error": None if stats is not None else UNAVAILABLE,
    }


def accuracy_view(stats: dict | None, settings: Any = None,
                  symbol: str = LEVELS_SYMBOL) -> tuple[dict, dict]:
    """(trust, stats) in the shape routes/matrix._accuracy_view returns — the
    self-eval blocks / cards / ledger rows read this."""
    p = accuracy_payload(stats, settings, symbol)
    trust = {k: p[k] for k in ("state", "win_rate", "graded", "wins_to_resume",
                               "display_text", "bias_confidence", "show_hint", "hint_text")}
    trust["tier"] = p["tier"]
    st = {k: p[k] for k in ("n", "wins", "losses", "neutrals", "accuracy_pct")}
    if stats is None:
        st["error"] = UNAVAILABLE
    return trust, st
