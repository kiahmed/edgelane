"""Read listener (docs/level_engine.md §3.1, §3.3) — container 0.0.0.0:8791,
compose network only (never published); the backend's `/levels/*` allow-list
proxy forwards exactly these four GETs:

  GET /levels/decision/{symbol}   latest decision + runner status (in memory)
  GET /levels/day/{date}          levels, clusters, level states, beliefs,
                                  picks, legs, day summary for that ET date
  GET /levels/trace/{symbol}      recent belief traces (?limit=, default all kept)
  GET /levels/stats/{symbol}      graded legs across days, newest closed first,
                                  + the open leg + binary counts (§12, contract
                                  C3; ?limit= int 1..200, default 50, else 404)

plus `GET /healthz` for the compose healthcheck (not under /levels/, so the
proxy never forwards it). Every other path AND every non-GET/HEAD method is a
plain 404 (no 405 that would reveal a route exists). No docs / OpenAPI. No writes.
A symbol other than cfg.symbol is 404. A malformed date is 404.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .brain.day_summary import binary_result
from .levels import build_clusters, convert_levels
from .store import Store

_NOT_FOUND = {"detail": "Not Found"}


def _nf() -> JSONResponse:
    return JSONResponse(status_code=404, content=_NOT_FOUND)


def _j(obj) -> JSONResponse:
    return JSONResponse(jsonable_encoder(obj))


STATS_LIMIT_DEFAULT, STATS_LIMIT_MAX = 50, 200
CALL_KEYS = ("up", "down", "range")


def _obj(v) -> dict | None:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return None
    return v if isinstance(v, dict) else None


def _pn(v) -> dict | None:
    v = _obj(v)
    return None if v is None else {"price": v.get("price"), "name": v.get("name")}


def leg_view(row: dict) -> dict:
    """One lvl_legs row in the C3 stats shape. Level names come from the leg's
    own target_level / invalidation_level (else the pick's); pick fields from
    the pick jsonb (None for a shadow leg). Pre-binary results map to loss."""
    pick = _obj(row.get("pick")) or {}
    result, why = binary_result(row.get("result"), row.get("close_reason"))
    return {
        "id": row.get("id"), "trade_date": row.get("trade_date"),
        "opened_at": row.get("opened_at"), "closed_at": row.get("closed_at"),
        "call": row.get("call"), "result": result, "close_reason": why,
        "target_level": _pn(row.get("target_level")) or _pn(pick.get("target_level")),
        "invalidation_level": (_pn(row.get("invalidation_level"))
                               or _pn(pick.get("invalidation_level"))),
        "entry_spot": row.get("entry_spot"), "exit_spot": row.get("exit_spot"),
        "progress_pct": row.get("progress_pct"), "mae_pts": row.get("mae_pts"),
        "time_to_target_min": row.get("time_to_target_min"),
        "strategy_pnl": row.get("strategy_pnl"),
        "pick_id": pick.get("pick_id"), "strategy": pick.get("strategy"),
        "label": pick.get("label"), "structure_text": pick.get("structure_text"),
        "spread_type": pick.get("spread_type") or pick.get("type"),
    }


def _bucket() -> dict:
    return {"n": 0, "wins": 0, "losses": 0, "win_rate": None}


def _rate(b: dict) -> dict:
    d = b["wins"] + b["losses"]
    b["win_rate"] = round(100.0 * b["wins"] / d, 1) if d else None
    return b


def stats_counts(legs: list[dict]) -> dict:
    """Binary counts over closed legs (C3): win_rate = wins / (wins + losses)
    in percent (None with no closed legs); by_call always has up/down/range;
    by_level is keyed by the TARGET level name ("?" when unknown)."""
    total = _bucket()
    by_call = {k: _bucket() for k in CALL_KEYS}
    by_level: dict[str, dict] = {}
    for g in legs:
        if g.get("result") not in ("win", "loss"):
            continue
        name = (g.get("target_level") or {}).get("name") or "?"
        for b in (total, by_call.setdefault(g.get("call") or "?", _bucket()),
                  by_level.setdefault(name, _bucket())):
            b["n"] += 1
            b["wins" if g["result"] == "win" else "losses"] += 1
    return {**_rate(total), "by_call": {k: _rate(v) for k, v in by_call.items()},
            "by_level": {k: _rate(v) for k, v in sorted(by_level.items())}}


def create_read_app(runner, store: Store | None = None) -> FastAPI:
    """`runner` = a TickRunner (latest_decision, trace_tail, status, clusters,
    trade_date, cfg). `store` defaults to runner.store (main passes its own
    connection so reads never share the tick loop's)."""
    st: Store = store if store is not None else runner.store
    cfg = runner.cfg
    app = FastAPI(title="Level Engine read API", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.middleware("http")
    async def _only_get(request: Request, call_next):
        if request.method not in ("GET", "HEAD"):
            return _nf()
        return await call_next(request)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        return _nf()

    @app.exception_handler(RequestValidationError)
    async def _bad_params(request: Request, exc: RequestValidationError):
        return _nf()

    def _sym_ok(symbol: str) -> bool:
        return (symbol or "").upper() == cfg.symbol.upper()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/levels/decision/{symbol}")
    def decision(symbol: str):
        if not _sym_ok(symbol):
            return _nf()
        return _j({"symbol": cfg.symbol, "decision": runner.latest_decision,
                   "status": runner.status})

    @app.get("/levels/trace/{symbol}")
    def trace(symbol: str, limit: int | None = None):
        if not _sym_ok(symbol):
            return _nf()
        tail = list(runner.trace_tail)
        if limit is not None and limit >= 0:
            tail = tail[-limit:] if limit else []
        return _j({"symbol": cfg.symbol, "trade_date": runner.trade_date, "traces": tail})

    @app.get("/levels/stats/{symbol}")
    def stats(symbol: str, limit: int = STATS_LIMIT_DEFAULT):
        if not _sym_ok(symbol) or not 1 <= limit <= STATS_LIMIT_MAX:
            return _nf()
        legs = [leg_view(r) for r in st.list_recent_legs(limit)]
        td = runner.trade_date
        open_row = st.get_open_leg(td) if td is not None else None
        return _j({"symbol": cfg.symbol, "as_of": datetime.now(timezone.utc).isoformat(),
                   "legs": legs, "open_leg": leg_view(open_row) if open_row else None,
                   "counts": stats_counts(legs)})

    @app.get("/levels/day/{day}")
    def day(day: str):
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return _nf()
        levels = st.list_levels(d)
        if d == runner.trade_date:
            clusters = [c.to_dict() for c in runner.clusters]
            ratio = runner.ratio.ratio
        else:
            ratio = st.latest_locked_ratio(d)
            if ratio is None:
                ratio = next((float(r["ratio"]) for r in levels
                              if r.get("source") == "converted" and r.get("ratio")), None)
            resolved, _ = convert_levels(levels, ratio, cfg.buffer_pts)
            clusters = [c.to_dict() for c in build_clusters(resolved, cfg.cluster_pts,
                                                            cfg.strike_step)]
        return _j({
            "trade_date": d, "ratio": ratio, "levels": levels, "clusters": clusters,
            "level_states": st.list_rows("level_state", d),
            "beliefs": st.list_rows("beliefs", d),
            "picks": st.list_rows("picks", d),
            "legs": st.list_rows("legs", d),
            "day_summary": st.get_day_summary(d),
        })

    return app
