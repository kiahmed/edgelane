"""Tiny client for the level engine's READ listener (docs/level_engine.md §3.1,
§3.3, §3.4).

The level engine is a separate container; its read API serves exactly
`GET /levels/decision/{symbol}`, `/levels/day/{date}`, `/levels/trace/{symbol}`,
`/levels/stats/{symbol}` on the compose network (LEVEL_ENGINE_READ_URL).
Consumers here:

  * routes/levels_proxy.py — the allow-list proxy (`get_upstream`, uncached).
  * level_view.py (→ /snapshot, the poller's matrix_signals hand-off, …) and
    routes/orders.py — the levels view of Matrix and the order dialog's
    level-pick lookup (`fetch_decision`, with a ~5 s in-process cache so
    /snapshot polling never hammers the engine).

Every call is bounded (~1.5 s) and never raises out of `fetch_decision`: any
failure is `None`; in levels mode the caller then shows NO pick (never the
legacy one — §3.4). No client headers, cookies or credentials are ever
forwarded — the upstream gets a bare GET.
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger("edgelane.market.level_pick")

TIMEOUT_SEC = 1.5
CACHE_TTL_SEC = 5.0


class LevelEngineUnavailable(Exception):
    """The read listener could not be reached or answered with a 5xx / non-JSON."""


async def _http_get(url: str, params: dict | None, timeout: float) -> tuple[int, object]:
    """One bare GET. Module-level so tests can replace it (and assert the URL)."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        resp = await client.get(url, params=params or None,
                                headers={"Accept": "application/json"})
    try:
        body = resp.json()
    except ValueError:
        body = None
    return resp.status_code, body


def _base(settings) -> str:
    return (getattr(settings, "level_engine_read_url", "") or "").rstrip("/")


async def get_upstream(settings, path: str, params: dict | None = None) -> tuple[int, object]:
    """GET `<LEVEL_ENGINE_READ_URL><path>`. `path` must come from a fixed
    template built by the caller (never client input). Returns (status, json)
    for 2xx / 4xx; raises LevelEngineUnavailable on transport errors, 5xx, a
    missing base URL or a non-JSON body."""
    base = _base(settings)
    if not base:
        raise LevelEngineUnavailable("LEVEL_ENGINE_READ_URL is blank")
    try:
        status, body = await _http_get(base + path, params, TIMEOUT_SEC)
    except Exception as e:  # httpx errors, timeouts, DNS — all "unavailable"
        raise LevelEngineUnavailable(f"{type(e).__name__}: {e}") from None
    if status >= 500 or (status < 400 and body is None):
        raise LevelEngineUnavailable(f"upstream HTTP {status}")
    return status, body


# symbol -> (monotonic ts, decision | None)
_cache: dict[str, tuple[float, dict | None]] = {}


def clear_cache() -> None:
    _cache.clear()


async def _fetch_uncached(settings, sym: str) -> dict | None:
    try:
        status, body = await get_upstream(settings, f"/levels/decision/{sym}")
    except LevelEngineUnavailable as e:
        log.debug("level engine decision unavailable for %s: %s", sym, e)
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    dec = body.get("decision")
    if dec is None:
        # Reachable, but no tick yet (or no brain): a real "nothing to show".
        dec = {"call": None, "pick": None, "status": "no_decision"}
    elif not isinstance(dec, dict):
        return None
    out = dict(dec)
    out["engine_status"] = body.get("status") if isinstance(body.get("status"), dict) else None
    return out


async def fetch_decision(settings, symbol: str, *, max_age: float = CACHE_TTL_SEC) -> dict | None:
    """Latest level-engine decision for `symbol` (the inner `decision` dict plus
    `engine_status`), or None when the engine is unreachable / answers badly.
    Never raises. Results — None included — are cached for `max_age` seconds;
    `max_age=0` forces a fresh read (the order path)."""
    try:
        sym = (symbol or "").upper()
        now = time.monotonic()
        hit = _cache.get(sym)
        if max_age > 0 and hit is not None and now - hit[0] < max_age:
            return hit[1]
        dec = await _fetch_uncached(settings, sym)
        _cache[sym] = (time.monotonic(), dec)
        return dec
    except Exception:
        log.exception("fetch_decision failed for %s", symbol)
        return None


def _grid_candidates(decision: dict):
    """(strategy key, candidate) for every FULL candidate in the level grid
    (decision["strategies"][*]["best"] and ["all_widths"], contract C1)."""
    grid = decision.get("strategies")
    if not isinstance(grid, dict):
        return
    for key, bucket in grid.items():
        if not isinstance(bucket, dict):
            continue
        best = bucket.get("best")
        if isinstance(best, dict):
            yield key, best
        for c in bucket.get("all_widths") or []:
            if isinstance(c, dict):
                yield key, c


def find_level_pick(decision: dict | None, pick_id: str | None) -> dict | None:
    """The candidate with `pick_id`, searched in `decision.pick`, then the level
    grid (`decision.strategies[*].best` / `all_widths` — full candidates with
    legs), then `decision.pick.candidates` (compact, no legs — the caller decides
    whether that is usable). None if absent."""
    if not decision or not pick_id:
        return None
    pick = decision.get("pick")
    if isinstance(pick, dict) and pick.get("pick_id") == pick_id:
        return pick
    for _key, c in _grid_candidates(decision):
        if c.get("pick_id") == pick_id:
            return c
    if not isinstance(pick, dict):
        return None
    for c in pick.get("candidates") or []:
        if isinstance(c, dict) and c.get("pick_id") == pick_id:
            return c
    return None


def level_strategy_names(decision: dict | None, pick_id: str | None) -> set[str]:
    """Every strategy name `pick_id` is shown under: the candidate's own
    `strategy` plus the grid key(s) it sits in. The order route accepts any of
    these as the request's `strategy` (the grid card sends its key)."""
    names: set[str] = set()
    if not decision or not pick_id:
        return names
    hit = find_level_pick(decision, pick_id)
    if hit is not None and hit.get("strategy"):
        names.add(hit["strategy"])
    for key, c in _grid_candidates(decision):
        if c.get("pick_id") == pick_id:
            names.add(key)
            if c.get("strategy"):
                names.add(c["strategy"])
    return names


def compact_decision(decision: dict | None) -> dict:
    """The small `level_decision` block the Matrix tile reads."""
    if decision is None:
        return {"status": "unavailable"}
    pick = decision.get("pick") if isinstance(decision.get("pick"), dict) else None
    es = decision.get("engine_status") or {}
    return {
        "call": decision.get("call"),
        "confidence": decision.get("confidence"),
        "reason": decision.get("reason"),
        "target_level": decision.get("target_level"),
        "invalidation_level": decision.get("invalidation_level"),
        "status": decision.get("status"),
        "ts": decision.get("ts"),
        "feed": es.get("feed") if isinstance(es, dict) else None,
        "pick_id": pick.get("pick_id") if pick else None,
    }
