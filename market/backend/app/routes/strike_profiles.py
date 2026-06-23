"""CRUD for per-ticker debit-spread strike profiles (the smart picker config).

The market UI reads these to show/edit how debit verticals are built per symbol.
Saved rows take effect on the next poll cycle (the poller calls
strike_profiles.resolve_profile each pass). See strike_profiles.py for the
field semantics and the picker that consumes them.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from ..strike_profiles import StrikeProfile, DEFAULT_PROFILE
from ..config import get_settings
from .. import auth
from ..auth import require_admin

# Server-owner config (it controls how the engine builds every user's strategies),
# so the whole router is admin-only: the admin token / ?token= grants access,
# regular signed-in users get 403. No-op when AUTH_ENABLED=false (dev).
router = APIRouter(dependencies=[Depends(require_admin)])

# The HTML page is served by a SEPARATE router with NO router-level dependency,
# so an unauthorized top-level GET returns a friendly HTML 401 (with "append
# ?token=") instead of the JSON 401 the API router would emit. Mirrors Torque.
page_router = APIRouter()
_HTML_PATH = Path(__file__).resolve().parents[2] / "ui" / "strike_profiles.html"


def _page_authorized(request: Request) -> bool:
    """Authorize the initial page navigation. Browsers can't attach a custom
    header on a top-level GET, so the shell accepts ?token=<ADMIN_API_TOKEN> in
    the URL (the page then stores it and sends it as X-Admin-Token on fetches).
    A header/Bearer is also honored if present. No-op when AUTH_ENABLED=false."""
    settings = get_settings()
    if not settings.auth_enabled:
        return True
    if auth._admin_token_ok(request):
        return True
    tok = (request.query_params.get("token") or "").strip()
    if settings.admin_api_token and tok == settings.admin_api_token:
        return True
    return False


@page_router.get("/strike-profiles/admin")
async def strike_profiles_page(request: Request):
    if not _page_authorized(request):
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><title>Strike Profiles — locked</title>"
            "<body style='background:#0b0f14;color:#aab7c5;font:15px system-ui;"
            "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'><h2 style='color:#36d399'>Strike Profiles is locked</h2>"
            "<p>Append <code>?token=YOUR_ADMIN_TOKEN</code> to the URL to access.</p></div>",
            status_code=401, headers={"Cache-Control": "no-store, max-age=0"})
    if not _HTML_PATH.is_file():
        raise HTTPException(404, f"strike_profiles.html not found at {_HTML_PATH}")
    # never cache the page — UI fixes land on a normal reload, not a hard refresh.
    return FileResponse(str(_HTML_PATH), media_type="text/html",
                        headers={"Cache-Control": "no-store, max-age=0"})


class StrikeProfileIn(BaseModel):
    """Editable fields. All optional on PUT — omitted fields inherit the
    built-in default for that knob."""
    enabled: bool = True
    long_delta_lo: float = Field(0.38, ge=0.0, le=1.0)
    long_delta_hi: float = Field(0.46, ge=0.0, le=1.0)
    long_offset_pts: float | None = Field(None, ge=0.0)
    short_min_delta: float = Field(0.12, ge=0.0, le=1.0)
    min_width_pts: float | None = Field(None, ge=0.0)
    max_width_pts: float | None = Field(None, ge=0.0)
    target_source: str = Field("wall", pattern="^(wall|oi|move)$")
    round_snap: float = Field(0.0, ge=0.0)
    min_oi: int = Field(0, ge=0)
    min_vol: int = Field(0, ge=0)


def _db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(500, "db not initialized on app.state")
    return db


@router.get("/strike-profiles")
async def list_profiles(request: Request):
    """All saved profiles (DEFAULT + any per-ticker overrides)."""
    return {"profiles": _db(request).list_strike_profiles()}


@router.get("/strike-profiles/{symbol}")
async def get_profile(symbol: str, request: Request):
    """The saved profile for a symbol if present, else the effective fallback
    (the DEFAULT row, or the built-in default) flagged as not symbol-specific."""
    db = _db(request)
    row = db.get_strike_profile(symbol)
    if row:
        return {"profile": row, "is_default": False}
    fallback = db.get_strike_profile("DEFAULT") or DEFAULT_PROFILE.to_row()
    return {"profile": fallback, "is_default": True}


@router.put("/strike-profiles/{symbol}")
async def upsert_profile(symbol: str, body: StrikeProfileIn, request: Request):
    """Create or replace a symbol's profile. Takes effect next poll cycle."""
    if body.long_delta_hi < body.long_delta_lo:
        raise HTTPException(422, "long_delta_hi must be >= long_delta_lo")
    if (body.min_width_pts is not None and body.max_width_pts is not None
            and body.max_width_pts < body.min_width_pts):
        raise HTTPException(422, "max_width_pts must be >= min_width_pts")
    prof = StrikeProfile(symbol=symbol.upper(), **body.model_dump())
    _db(request).upsert_strike_profile(prof.to_row())
    return {"ok": True, "profile": prof.to_row()}
