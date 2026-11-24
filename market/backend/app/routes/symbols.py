"""Configured symbol list (populates the UI dropdown).

MATRIX_DECISION_SOURCE=levels advertises only SPX (the only symbol polled —
docs/level_engine.md §3.4); `engine` returns SYMBOLS unchanged. Same shape."""
from __future__ import annotations
from fastapi import APIRouter, Request

from .. import level_view

router = APIRouter()


@router.get("/symbols")
async def list_symbols(request: Request):
    s = request.app.state.settings
    return {"symbols": level_view.level_symbols(s, s.symbols)}
