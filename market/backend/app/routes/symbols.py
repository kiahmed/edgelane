"""Configured symbol list (populates the UI dropdown)."""
from __future__ import annotations
from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/symbols")
async def list_symbols(request: Request):
    s = request.app.state.settings
    return {"symbols": s.symbols}
