"""`/levels/*` allow-list proxy to the level engine's READ listener
(docs/level_engine.md §3.3, §17 "Exposure").

Exactly four GETs are forwarded, each to a FIXED upstream template on
LEVEL_ENGINE_READ_URL (never the admin port, never a client-built path):

  GET /levels/decision/{symbol}   symbol  ^[A-Z]{1,6}$
  GET /levels/day/{date}          date    ^[0-9]{4}-[0-9]{2}-[0-9]{2}$
  GET /levels/trace/{symbol}      symbol  ^[A-Z]{1,6}$   (?limit=<int> only)
  GET /levels/stats/{symbol}      symbol  ^[A-Z]{1,6}$   (?limit=<int 1..200> only)

Everything else under /levels/ — any other path, any other method, a bad
symbol / date — is a plain 404. Gate: the same dependency as /snapshot
(`require_teaser_session`) and, like /snapshot's gated fields, only the FULL
identity (entitled user / admin / dev) gets level data; an anonymous teaser
session is 403. Only the upstream JSON body is returned; no client header,
cookie or credential is forwarded. Upstream unreachable / 5xx → 503.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from .. import level_pick
from ..config import get_settings
from ..session_auth import require_teaser_session

router = APIRouter()

_SYMBOL_RE = re.compile(r"[A-Z]{1,6}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_LIMIT_RE = re.compile(r"[0-9]{1,5}")
_STATS_LIMIT_RE = re.compile(r"[0-9]{1,3}")
_STATS_LIMIT_MAX = 200
_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _nf() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


def _require_full(ident: dict) -> None:
    if not ident.get("full"):
        raise HTTPException(403, "Level engine data requires a Matrix login")


async def _forward(path: str, params: dict | None = None) -> JSONResponse:
    try:
        status, body = await level_pick.get_upstream(get_settings(), path, params)
    except level_pick.LevelEngineUnavailable:
        return JSONResponse(status_code=503, content={"error": "level engine unavailable"})
    if status != 200:
        # The read API answers 404 for anything it doesn't serve; mirror it
        # without echoing the upstream body.
        return _nf()
    return JSONResponse(status_code=200, content=body)


@router.get("/levels/decision/{symbol}", include_in_schema=False)
async def levels_decision(symbol: str, ident: dict = Depends(require_teaser_session)):
    _require_full(ident)
    if not _SYMBOL_RE.fullmatch(symbol):
        return _nf()
    return await _forward(f"/levels/decision/{symbol}")


@router.get("/levels/day/{day}", include_in_schema=False)
async def levels_day(day: str, ident: dict = Depends(require_teaser_session)):
    _require_full(ident)
    if not _DATE_RE.fullmatch(day):
        return _nf()
    return await _forward(f"/levels/day/{day}")


@router.get("/levels/trace/{symbol}", include_in_schema=False)
async def levels_trace(symbol: str, limit: str | None = Query(default=None),
                       ident: dict = Depends(require_teaser_session)):
    _require_full(ident)
    if not _SYMBOL_RE.fullmatch(symbol):
        return _nf()
    if limit is not None and not _LIMIT_RE.fullmatch(limit):
        return _nf()
    return await _forward(f"/levels/trace/{symbol}",
                          {"limit": int(limit)} if limit is not None else None)


@router.get("/levels/stats/{symbol}", include_in_schema=False)
async def levels_stats(symbol: str, limit: str | None = Query(default=None),
                       ident: dict = Depends(require_teaser_session)):
    """Graded level legs + win-rate counts (contract C3). `limit` must be an
    int 1..200 (else 404, never forwarded); absent → the read API's default."""
    _require_full(ident)
    if not _SYMBOL_RE.fullmatch(symbol):
        return _nf()
    if limit is not None:
        if not _STATS_LIMIT_RE.fullmatch(limit) or not 1 <= int(limit) <= _STATS_LIMIT_MAX:
            return _nf()
    return await _forward(f"/levels/stats/{symbol}",
                          {"limit": int(limit)} if limit is not None else None)


@router.api_route("/levels", methods=_ALL_METHODS, include_in_schema=False)
@router.api_route("/levels/{rest:path}", methods=_ALL_METHODS, include_in_schema=False)
async def levels_not_found(rest: str = ""):
    """Anything not on the allow-list (other paths, other methods) is 404."""
    return _nf()
