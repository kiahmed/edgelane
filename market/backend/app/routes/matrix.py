"""Matrix read-only integration API + snapshot render surface.

A SEPARATE surface from the browser API: bearer-token auth via `MATRIX_API_TOKEN`
(Secret Manager `matrix-api-token`), no user JWT. Read-only — it projects what the
poller and the grader already decided and never writes, never recomputes.

Mirrors `routes/simmer.py`'s own integration surface one-for-one
(docs/matrix_events_update.md §4/§5), including the reason the snap endpoint
exists at all: matrix.facades.trade is behind a user login, so a headless browser
screenshotting the SPA captures the sign-in dialog and nothing else. The poster
hits these instead.

    GET /matrix/state/{SYM}?block=pick|grid|bias|win_eval|walls
    GET /matrix/snap/{SYM}?view=engine_pick|strategy_grid|bias_chip|walls_chip|win_eval_grid

Blank token ⇒ 401 (dark until provisioned), matching Simmer.
"""
from __future__ import annotations

import hmac
import re

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

from ..config import get_settings
from ..poller import state as poller_state

router = APIRouter()

_SYM_RE = re.compile(r"^[A-Z.\-]{1,10}$")
_STATE_BLOCKS = ("pick", "grid", "bias", "win_eval", "walls")


def require_matrix_api_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer gate for the integration API. 401 when the server token is unset
    (feature closed) or the presented token is missing / mismatched.
    Constant-time compare so a mismatch can't be timed."""
    token = (get_settings().matrix_api_token or "").strip()
    if not token:
        raise HTTPException(401, "matrix integration API not configured")
    presented = ""
    if authorization and authorization[:7].lower() == "bearer ":
        presented = authorization[7:].strip()
    if not presented or not hmac.compare_digest(presented, token):
        raise HTTPException(401, "invalid or missing bearer token")


_API_TOKEN_GATE = [Depends(require_matrix_api_token)]


def _snapshot_or_404(symbol: str) -> tuple[str, dict]:
    sym = (symbol or "").upper()
    if not _SYM_RE.match(sym):
        raise HTTPException(422, f"invalid symbol {symbol!r}")
    snap = (poller_state.latest_by_symbol or {}).get(sym)
    if not snap:
        raise HTTPException(404, f"no snapshot for {sym} (poller hasn't run yet)")
    return sym, snap


def _accuracy_view(sym: str) -> tuple[dict, dict]:
    """(trust, stats) for the self-eval blocks, reusing the accuracy route's own
    logic rather than restating the tiering rules here."""
    from .. import main as _main
    from .accuracy import _tier, _trust_state

    settings = get_settings()
    db = getattr(_main, "_db", None)
    if db is None:
        return {}, {}
    try:
        stats = db.fetch_accuracy(sym, int(settings.eval_rolling_window))
    except Exception:
        return {}, {}
    n = int(stats.get("n") or 0)
    pct = float(stats.get("accuracy_pct") or 0.0)
    trust = _trust_state(sym, n, pct, settings)
    trust["tier"] = _tier(pct, n, float(settings.pill_green_pct), float(settings.pill_red_pct))
    return trust, stats


def _state_block(snap: dict, block: str, sym: str) -> dict:
    """Slice one block out of the cached snapshot. Read-only projections."""
    bias = snap.get("bias") or {}
    if block == "pick":
        pick = snap.get("engine_pick") or {}
        return {
            "expiration": snap.get("expiration"),
            "spot": snap.get("spot"),
            "pick": pick or None,
            "polled_at": snap.get("polled_at"),
        }
    if block == "grid":
        grid = {}
        for key, bucket in (snap.get("strategies") or {}).items():
            best = (bucket or {}).get("best") or {}
            grid[key] = {
                "label": best.get("label"),
                "structure_text": best.get("structure_text"),
                "composite_score": best.get("composite_score"),
                "composite_verdict": best.get("composite_verdict"),
                "health": best.get("health"),
                "liquidity": best.get("liquidity"),
                "pop_pct": best.get("pop_pct"),
            } if best else None
        return {"expiration": snap.get("expiration"), "grid": grid}
    if block == "bias":
        trust, _ = _accuracy_view(sym)
        return {
            "bias_label": bias.get("bias_label"),
            "directional_score": bias.get("directional_score"),
            "confidence": bias.get("confidence"),
            "net_gex": bias.get("net_gex"),
            "recommended_strategies": bias.get("recommended_strategies"),
            "trust": trust or None,
        }
    if block == "win_eval":
        trust, stats = _accuracy_view(sym)
        return {"trust": trust or None, "stats": stats or None}
    if block == "walls":
        return {
            "spot": snap.get("spot"),
            "expected_move": snap.get("expected_move"),
            "key_levels": {
                "call_wall": bias.get("call_wall_strike"),
                "call_wall_strength": bias.get("call_wall_strength"),
                "put_wall": bias.get("put_wall_strike"),
                "put_wall_strength": bias.get("put_wall_strength"),
                "vex_wall": bias.get("vex_wall_strike"),
                "tex_wall": bias.get("tex_wall_strike"),
                "gex_wall": bias.get("gex_wall_strike"),
            },
        }
    return {}


@router.get("/matrix/state/{symbol}", dependencies=_API_TOKEN_GATE)
async def matrix_state_block(symbol: str, block: str = Query(default="pick")):
    """One block of a symbol's latest snapshot (pick|grid|bias|win_eval|walls) —
    the data each post moment's copy template needs."""
    if block not in _STATE_BLOCKS:
        raise HTTPException(422, f"invalid block {block!r}; allowed: "
                            f"{', '.join(_STATE_BLOCKS)}")
    sym, snap = _snapshot_or_404(symbol)
    return {"symbol": sym, "block": block, "data": _state_block(snap, block, sym)}


@router.get("/matrix/snap/{symbol}", dependencies=_API_TOKEN_GATE,
            response_class=HTMLResponse)
async def matrix_snap(symbol: str, view: str = Query(default="engine_pick")):
    """Server-rendered card as standalone HTML for the snapshot service to
    screenshot. Never touches the SPA or a user session — see module docstring."""
    from .. import matrix_snap as snap_mod

    if view not in snap_mod.VIEWS:
        raise HTTPException(422, f"invalid view {view!r}; allowed: "
                            f"{', '.join(snap_mod.VIEWS)}")
    sym, snap = _snapshot_or_404(symbol)
    trust = stats = None
    if view in ("bias_chip", "win_eval_grid"):
        trust, stats = _accuracy_view(sym)
    return HTMLResponse(snap_mod.render_snap_card(snap, view, trust=trust, stats=stats))
