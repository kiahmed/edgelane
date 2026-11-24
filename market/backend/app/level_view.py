"""The "levels view" of Matrix (docs/level_engine.md §3.4, revised 2026-11-23).

With `MATRIX_DECISION_SOURCE=levels` EVERY decision Matrix shows or emits comes
from the level engine; the legacy engine is a data service only (spot / bias /
walls / flow — which the level engine itself consumes through the feed). This
module is the one place that builds that view, so every consumer (the /snapshot
routes, the poller's matrix_signals hand-off, the Matrix ledger / snap / email
in W2b) agrees on it:

  * `levels_mode(settings)`      — the switch.
  * `level_symbols(settings, s)` — what the poller polls / the UI advertises.
  * `level_snapshot(settings, snap)` — a COPY of a legacy snapshot with the
    decision fields replaced by the level engine's.

The poller's cached snapshot (`poller.state.latest_by_symbol`) is NEVER mutated:
the internal feed (routes/internal_feed.py) keeps sending the legacy output to
the level engine, and each consumer gets its own level-view copy.
"""
from __future__ import annotations

import copy

from . import level_pick

LEVELS_SYMBOL = "SPX"


def levels_mode(settings) -> bool:
    """True when MATRIX_DECISION_SOURCE=levels.

    `settings` may be the Pydantic Settings or the poller's duck-typed
    SimpleNamespace view (main._settings_view), which doesn't carry this field —
    a missing attribute falls back to the process settings (get_settings())."""
    src = getattr(settings, "matrix_decision_source", None)
    if src is None:
        try:
            from .config import get_settings
            src = getattr(get_settings(), "matrix_decision_source", "engine")
        except Exception:
            src = "engine"
    return (src or "engine") == "levels"


def level_symbols(settings, symbols: list[str]) -> list[str]:
    """The symbols to poll / advertise. `engine` mode: `symbols`, unchanged (the
    same object). `levels` mode: always `["SPX"]` — the level engine is SPX-only
    and needs the SPX feed, so SPX is polled even if SYMBOLS omits it; NDX and
    every other SYMBOLS entry are dropped while `levels` is on."""
    if not levels_mode(settings):
        return symbols
    return [LEVELS_SYMBOL]


async def level_snapshot(settings, snap: dict, *, max_age: float | None = None) -> dict:
    """A copy of the legacy snapshot `snap` with Matrix's decision fields taken
    from the level engine (contract C1). `snap` is never mutated.

      engine_pick         the level pick (None on wait / done / no pick)
      engine_pick_legacy  the legacy pick   (kept for audit / debugging only)
      strategies          decision["strategies"] — the level grid ({} on wait /
                          done / no pick)
      strategies_legacy   the legacy grid   (audit only)
      level_decision      level_pick.compact_decision(...)

    Level engine unreachable → engine_pick None, strategies {},
    level_decision {"status": "unavailable"}: nothing legacy is ever shown in
    levels mode (no graceful degrade to the legacy pick).

    Level-derived parts are deep-copied (fetch_decision caches its dict for a few
    seconds and consumers must not be able to poison it); the legacy parts that
    are re-exposed under *_legacy are deep-copied too. Everything else (spot,
    bias, walls, …) is shared read-only with `snap`, exactly as /snapshot does
    today."""
    kw = {} if max_age is None else {"max_age": max_age}
    decision = await level_pick.fetch_decision(settings, LEVELS_SYMBOL, **kw)
    out = dict(snap)
    out["engine_pick_legacy"] = copy.deepcopy(snap.get("engine_pick"))
    out["strategies_legacy"] = copy.deepcopy(snap.get("strategies"))
    out["level_decision"] = level_pick.compact_decision(decision)
    pick = decision.get("pick") if isinstance(decision, dict) else None
    if isinstance(pick, dict) and pick.get("pick_id"):
        out["engine_pick"] = copy.deepcopy(pick)
        grid = decision.get("strategies")
        out["strategies"] = copy.deepcopy(grid) if isinstance(grid, dict) else {}
    else:
        out["engine_pick"] = None
        out["strategies"] = {}
    return out
