"""Simmer — premium-selling readiness engine, HTTP surface (docs/simmer.md).

Every endpoint sits behind the `ensure_tool(user, "simmer")` gate, follows the
repo's no-prefix convention (full path in each decorator) and returns plain
dicts.

Data plane (architecture decision 2026-08-16, superseding the Matrix
client-RLS pattern): **this API is the browser's ONLY surface** — every data
read and write goes through here with the verified JWT, and identity itself
flows through /auth/* (see routes/auth_proxy.py). Watchlist writes are
service-role upserts scoped to the caller's verified user id; the RLS policies
on the Supabase tables remain purely as defense-in-depth that no client uses.
Alerts stay backend-written only (watcher fan-out).

`/simmer/analyze/{symbol}` calls `simmer_watcher.analyze_symbol` — the SAME
function the sweep runs — with persist=False, so the on-demand and scheduled
paths can never diverge and an on-demand call never pollutes calibration.

Settings enforcement is server-side and authoritative: bounded knobs are
clamped through `simmer_config.clamp_setting` (short-delta band hard-clamped
to 0.15–0.40), and `gate_overrides` is validated against the TOGGLEABLE
allow-list — a client asking to disable a locked gate gets a 422, not a
silent ignore.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..auth import get_current_user
from ..entitlements import ensure_tool
from .. import simmer_config
from .. import simmer_news
from .. import simmer_watcher
from .. import supabase_admin
from ..simmer_engine import STRUCTURES
from ..simmer_watcher import state as simmer_state

log = logging.getLogger("edgelane.market.simmer")
router = APIRouter()


async def require_simmer_access(request: Request,
                                user: dict = Depends(get_current_user)) -> dict:
    await ensure_tool(user, "simmer")
    return user


_GATE = [Depends(require_simmer_access)]


def _tradier(request: Request):
    c = getattr(request.app.state, "tradier", None)
    if c is None:
        raise HTTPException(500, "tradier client not initialized")
    return c


def _db(request: Request):
    return getattr(request.app.state, "db", None)


def _dte(exp_iso: str) -> int:
    try:
        return (date.fromisoformat(str(exp_iso)[:10]) - date.today()).days
    except (TypeError, ValueError):
        return 0


# ── Config / status ─────────────────────────────────────────────────────────
@router.get("/simmer/config", dependencies=_GATE)
async def simmer_config_endpoint():
    """Public knob defaults + engine version (nothing here is a secret)."""
    return {
        "engine_version": simmer_config.engine_version(),
        "decision": simmer_config.decision_bands(),
        "weights": simmer_config.weights(),
        "gates": simmer_config.gates(),
        "targets": simmer_config.targets(),
        "management": simmer_config.management(),
        "cadence": simmer_config.cadence(),
        "tickers": simmer_config.tickers(),
        "blocked_tickers": sorted(simmer_config.blocked_tickers()),
        "structures": list(STRUCTURES),
        "locked_gates": list(simmer_config.LOCKED_GATES),
        "toggleable_gates": list(simmer_config.TOGGLEABLE_GATES),
        "user_clamps": {k: list(v) for k, v in simmer_config.user_clamps().items()},
        "alerts": {"hysteresis_margin": simmer_watcher.HYSTERESIS_MARGIN,
                   "ready_dwell_sec": simmer_watcher.READY_DWELL_SEC},
    }


@router.get("/simmer/status", dependencies=_GATE)
async def simmer_status():
    """Watcher-loop health — mirrors the shape of `/status`."""
    s = simmer_state
    return {
        "alive": True,
        "is_running": s.is_running,
        "market_open": s.market_open,
        "market_reason": s.market_reason,
        "last_sweep_at": s.last_sweep_at,
        "sweep_count": s.sweep_count,
        "watched_count": s.watched_count,
        "watchlist_source": s.watchlist_source,
        "keys_tracked": sorted(s.latest_by_key.keys()),
        "regime": s.regime,
        "dispersion": s.dispersion,
        "alerts_fired": s.alerts_fired,
        "alert_states": {k: v.get("state") for k, v in s.alert_states.items()},
        "last_error": s.last_error,
        "last_error_by_symbol": dict(s.last_error_by_symbol),
        "outcomes": {"last_run_at": s.last_outcome_run_at,
                     "recorded": s.outcomes_recorded},
        "iv_history_last_date": s.last_iv_history_date,
        "engine_version": simmer_config.engine_version(),
    }


# ── Watchlist (reads here; writes are client-side under RLS) ────────────────
def _readiness_for(symbol: str, expiration: str | None) -> dict | None:
    """Latest readiness from the in-memory cache. `expiration` None (auto)
    matches any key of that symbol — newest computed_at wins."""
    sym = symbol.upper()
    if expiration:
        return simmer_state.latest_by_key.get(f"{sym}|{expiration}")
    best = None
    for key, env in simmer_state.latest_by_key.items():
        if key.startswith(f"{sym}|"):
            if best is None or str(env.get("computed_at")) > str(best.get("computed_at")):
                best = env
    return best


@router.get("/simmer/watchlist")
async def get_watchlist(request: Request,
                        user: dict = Depends(require_simmer_access)):
    rows = await supabase_admin.select_many(
        "simmer_watchlist",
        "id,symbol,expiration,position,active,overrides,created_at,updated_at",
        {"user_id": f"eq.{user['id']}", "active": "eq.true"},
        order="position.asc,symbol.asc")
    supabase_ok = rows is not None
    out = []
    for r in rows or []:
        sym = str(r.get("symbol") or "").upper()
        exp = str(r.get("expiration"))[:10] if r.get("expiration") else None
        env = _readiness_for(sym, exp)
        if env is None and exp:
            db = _db(request)
            if db is not None:
                env = await asyncio.to_thread(db.latest_simmer_readiness, sym, exp)
        out.append({**r, "readiness": env})
    return {"watchlist": out, "supabase": supabase_ok,
            "write_model": "api",   # ALL data writes via this API; Supabase in the browser is sign-in only
            "last_sweep_at": simmer_state.last_sweep_at}


async def _validate_symbol(request: Request, symbol: str,
                           expiration: str | None = None) -> dict:
    """Shared validation: the symbol must be Simmer-eligible AND carry a
    tradable options chain. Never writes anything."""
    sym = (symbol or "").strip().upper()
    if not sym or not re.fullmatch(r"[A-Z.\-]{1,10}", sym):
        raise HTTPException(422, f"invalid symbol {symbol!r}")
    rule = simmer_config.ticker_rule(sym)
    if not rule.get("eligible", True):
        raise HTTPException(422, f"{sym} is not eligible for Simmer "
                                 f"({rule.get('ineligible_reason', 'not tradable')})")
    tradier = _tradier(request)
    quote = await tradier.stock_quote(sym)
    spot = simmer_watcher._first_price(quote)
    if not spot:
        raise HTTPException(404, f"no quote for {sym}")
    exps = await tradier.option_expirations(sym)
    listed = sorted({str(e)[:10] for e in exps or [] if e})
    if not listed:
        raise HTTPException(422, f"{sym} has no listed options chain")
    if expiration and str(expiration)[:10] not in listed:
        raise HTTPException(422, f"expiration {expiration} is not listed for {sym}")
    return {"symbol": sym, "valid": True, "spot": spot,
            "expirations": listed[:20], "write_model": "client_rls"}


@router.get("/simmer/validate/{symbol}", dependencies=_GATE)
async def validate_symbol(symbol: str, request: Request,
                          expiration: str | None = Query(default=None)):
    return await _validate_symbol(request, symbol, expiration)


class WatchlistAdd(BaseModel):
    position: int | None = Field(default=None, ge=0, le=999)
    symbol: str
    expiration: str | None = None


@router.post("/simmer/watchlist")
async def post_watchlist(body: WatchlistAdd, request: Request,
                         user: dict = Depends(require_simmer_access)):
    """Validate, then WRITE the row on behalf of the verified user
    (service-role upsert on (user_id, symbol)). API-first data plane — the
    browser never writes Supabase tables directly; Supabase in the client is
    for SIGN-IN only (it is the identity provider issuing the JWT)."""
    validated = await _validate_symbol(request, body.symbol, body.expiration)
    validated.pop("write_model", None)   # GET's field; stale "client_rls" here
    uid = str(user["id"])
    if uid in ("admin", "dev-local"):
        raise HTTPException(422, "watchlist rows belong to a signed-in user "
                                 "account; the admin/dev bypass has none")
    row = {"user_id": uid, "symbol": validated["symbol"], "active": True}
    if body.position is not None:
        row["position"] = int(body.position)
    if body.expiration:
        row["expiration"] = str(body.expiration)[:10]
    ok = await supabase_admin.upsert_row(
        "simmer_watchlist", row, on_conflict="user_id,symbol")
    if not ok:
        raise HTTPException(502, "watchlist write failed (Supabase unreachable "
                                 "or unconfigured)")
    return {**validated, "written": True}


@router.delete("/simmer/watchlist/{symbol}")
async def delete_watchlist(symbol: str,
                           user: dict = Depends(require_simmer_access)):
    """Remove a symbol from the caller's watchlist (service-role, scoped to the
    verified user id — a user can only ever delete their own row)."""
    sym = (symbol or "").strip().upper()
    if not re.fullmatch(r"[A-Z.\-]{1,10}", sym):
        raise HTTPException(422, f"invalid symbol {symbol!r}")
    ok = await supabase_admin.delete_rows(
        "simmer_watchlist",
        {"user_id": f"eq.{user['id']}", "symbol": f"eq.{sym}"})
    if not ok:
        raise HTTPException(502, "watchlist delete failed")
    return {"symbol": sym, "deleted": True}


class WatchlistPatch(BaseModel):
    # `model_fields_set` distinguishes an EXPLICIT null (clear the pin → back
    # to engine auto-pick) from the field being absent — plain `is not None`
    # cannot, and "back to auto" would 422.
    expiration: str | None = Field(default=None, description="pin ONE expiry; explicit null clears")
    active: bool | None = None
    position: int | None = Field(default=None, ge=0, le=999)


@router.patch("/simmer/watchlist/{symbol}")
async def patch_watchlist(symbol: str, body: WatchlistPatch, request: Request,
                          user: dict = Depends(require_simmer_access)):
    """Update expiration pin / active / display position on the caller's row."""
    sym = (symbol or "").strip().upper()
    if not re.fullmatch(r"[A-Z.\-]{1,10}", sym):
        raise HTTPException(422, f"invalid symbol {symbol!r}")
    values: dict = {}
    if "expiration" in body.model_fields_set:
        if body.expiration is None:
            values["expiration"] = None        # clear the pin → engine auto-picks
        else:
            await _validate_symbol(request, sym, body.expiration)  # 422 if unlisted
            values["expiration"] = str(body.expiration)[:10]
    if body.active is not None:
        values["active"] = bool(body.active)
    if body.position is not None:
        values["position"] = int(body.position)
    if not values:
        raise HTTPException(422, "nothing to update")
    ok = await supabase_admin.update_rows(
        "simmer_watchlist",
        {"user_id": f"eq.{user['id']}", "symbol": f"eq.{sym}"}, values)
    if not ok:
        raise HTTPException(502, "watchlist update failed")
    return {"symbol": sym, "updated": sorted(values)}


# ── Analyze / expirations ───────────────────────────────────────────────────
@router.get("/simmer/analyze/{symbol}")
async def analyze(symbol: str, request: Request,
                  expiration: str | None = Query(default=None),
                  user: dict = Depends(require_simmer_access)):
    """On-demand cook — the SAME function the watcher's sweep calls
    (`simmer_watcher.analyze_symbol`), with persist=False so an on-demand call
    never adds engine-verdict rows to the calibration tables."""
    # Use the SWEEP's provider when the loop has published one — analyze must
    # run on the same data source as the sweep (tradier fallback pre-loop).
    source = simmer_state.provider or _tradier(request)
    db = _db(request)
    try:
        env = await simmer_watcher.analyze_symbol(
            source, db, symbol, expiration,
            regime=simmer_state.regime,
            # Same cross-sectional peers the sweep uses, so on-demand /analyze
            # and the scheduled sweep agree (the cold-start VRP bootstrap).
            peer_vrp=[v for v in simmer_state.peer_iv_rv.values() if v is not None],
            persist=False)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(404, str(e))
    return env


@router.get("/simmer/expirations/{symbol}", dependencies=_GATE)
async def expirations(symbol: str, request: Request):
    tradier = _tradier(request)
    exps = await tradier.option_expirations(symbol.upper())
    listed = sorted({str(e)[:10] for e in exps or [] if e})
    if not listed:
        raise HTTPException(404, f"no expirations for {symbol.upper()}")
    g = simmer_config.gates()
    lo, hi = float(g["dte_min"]), float(g["dte_max"])
    in_window = [e for e in listed if lo <= _dte(e) <= hi]
    recommended = simmer_watcher.choose_expiration(listed, None, g)
    return {
        "symbol": symbol.upper(),
        "dte_window": [lo, hi],
        "expirations": [
            {"date": e, "dte": _dte(e), "in_window": e in in_window,
             "recommended": e == recommended}
            for e in listed if _dte(e) >= 0
        ],
    }


# ── Alerts ──────────────────────────────────────────────────────────────────
@router.get("/simmer/alerts")
async def get_alerts(user: dict = Depends(require_simmer_access),
                     limit: int = Query(default=50, ge=1, le=200),
                     offset: int = Query(default=0, ge=0),
                     unacknowledged_only: bool = Query(default=False)):
    filters = {"user_id": f"eq.{user['id']}"}
    if unacknowledged_only:
        filters["acknowledged"] = "eq.false"
    rows = await supabase_admin.select_many(
        "simmer_alerts",
        "id,symbol,expiration,score,state,structure,payload,acknowledged,created_at",
        filters, order="created_at.desc", limit=limit, offset=offset)
    return {"alerts": rows or [], "limit": limit, "offset": offset,
            "supabase": rows is not None}


_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


class AlertAck(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)


@router.post("/simmer/alerts/ack")
async def ack_alerts(body: AlertAck, user: dict = Depends(require_simmer_access)):
    ids = [i.strip() for i in body.ids if i and _UUID_RE.fullmatch(i.strip())]
    if not ids:
        raise HTTPException(422, "no valid alert ids")
    ok = await supabase_admin.update_rows(
        "simmer_alerts",
        {"user_id": f"eq.{user['id']}", "id": f"in.({','.join(ids)})"},
        {"acknowledged": True})
    return {"acknowledged": ids if ok else [], "ok": ok}


# ── News (Phase 3a — read-only view over simmer_news + research cache) ──────
@router.get("/simmer/news/{symbol}", dependencies=_GATE)
async def simmer_news_endpoint(symbol: str, request: Request):
    """Last-24h scored news CLUSTERS for one symbol (deduped — one entry per
    wire story, `breadth` = syndication count), plus the trailing sentiment
    aggregate and velocity state from the tier-1 research cache. Read-only:
    ingestion/scoring happens only in the watcher's news refresh."""
    sym = (symbol or "").strip().upper()
    if not sym or not re.fullmatch(r"[A-Z.\-]{1,10}", sym):
        raise HTTPException(422, f"invalid symbol {symbol!r}")
    db = _db(request)
    if db is None:
        raise HTTPException(500, "database not initialized")
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    rows = await asyncio.to_thread(simmer_news.fetch_news_since, db, sym, since)

    clusters: dict[str, dict] = {}
    for r in rows:                                # rows arrive oldest-first
        ck = r.get("cluster_key") or r.get("article_id")
        c = clusters.get(ck)
        if c is None:
            c = clusters[ck] = {
                "cluster_key": ck,
                "headline": r.get("headline"),     # representative = earliest
                "url": r.get("url"),
                "source": r.get("source"),
                "published_at": r.get("published_at"),
                "breadth": 0,
                "_scores": [],
            }
        c["breadth"] += 1
        if r.get("sentiment") is not None:
            c["_scores"].append(float(r["sentiment"]))
    out_clusters = []
    for c in sorted(clusters.values(),
                    key=lambda c: str(c.get("published_at")), reverse=True):
        scores = c.pop("_scores")
        c["sentiment"] = round(sum(scores) / len(scores), 4) if scores else None
        pub = c.get("published_at")
        if isinstance(pub, datetime):
            c["published_at"] = pub.replace(tzinfo=timezone.utc).isoformat() \
                .replace("+00:00", "Z")
        out_clusters.append(c)

    research = await asyncio.to_thread(db.get_simmer_research, sym) or {}
    news_at = research.get("news_at")
    if isinstance(news_at, datetime):
        news_at = news_at.replace(tzinfo=timezone.utc).isoformat() \
            .replace("+00:00", "Z")
    return {
        "symbol": sym,
        "window_hours": 24,
        "clusters": out_clusters,
        "aggregate": {"sentiment_score": research.get("sentiment_score"),
                      "sentiment_n": research.get("sentiment_n"),
                      "news_at": news_at},
        "velocity": {"p": research.get("velocity_p"),
                     "tier": research.get("velocity_tier")},
    }


# ── Outcomes ────────────────────────────────────────────────────────────────
@router.get("/simmer/outcomes/summary", dependencies=_GATE)
async def outcomes_summary(request: Request):
    """Aggregate paper-outcome stats. ENGINE verdicts only — outcome rows are
    keyed to readiness rows the watcher wrote without any user's settings, so
    these numbers are comparable across every user configuration."""
    db = _db(request)
    if db is None:
        raise HTTPException(500, "database not initialized")
    summary = await asyncio.to_thread(db.fetch_simmer_outcome_summary)
    summary["basis"] = "engine_verdicts_only"
    summary["last_run_at"] = simmer_state.last_outcome_run_at
    return summary


# ── Settings (validated + clamped server-side, written service-role) ────────
_ALLOWED_STRICTNESS = ("relaxed", "balanced", "strict")


def sanitize_settings(payload: dict) -> dict:
    """Clamp bounded knobs, reject locked-gate overrides with 422 (never a
    silent ignore — see docs/simmer.md, 'User settings and overrides')."""
    out: dict = {}
    # Keys are the simmer_settings COLUMN names (migration 0010) — not the
    # engine-internal gates() keys (dte_min/dte_max). A wrong key here doesn't
    # error locally: PostgREST 400s the whole upsert and settings silently stop
    # persisting. Pinned by tests/simmer/test_settings_schema.py.
    for k in ("min_score", "min_iv_percentile", "short_delta_min",
              "short_delta_max", "min_dte", "max_dte"):
        if payload.get(k) is not None:
            try:
                out[k] = simmer_config.clamp_setting(k, float(payload[k]))
            except (TypeError, ValueError):
                raise HTTPException(422, f"{k} must be a number")
    if "short_delta_min" in out and "short_delta_max" in out \
            and out["short_delta_min"] > out["short_delta_max"]:
        raise HTTPException(422, "short_delta_min must be <= short_delta_max")
    for k in ("min_dte", "max_dte", "max_concurrent"):
        if k in out:
            out[k] = int(out[k])
    # Mirror the migration's CHECK (max_dte > min_dte) with a readable 422
    # instead of letting PostgREST bubble an opaque constraint violation.
    if "min_dte" in out and "max_dte" in out and out["max_dte"] <= out["min_dte"]:
        raise HTTPException(422, "max_dte must be greater than min_dte")
    if payload.get("max_concurrent") is not None:
        try:
            out["max_concurrent"] = max(1, min(50, int(payload["max_concurrent"])))
        except (TypeError, ValueError):
            raise HTTPException(422, "max_concurrent must be an integer")
    # Watcher sweep cadence, in MINUTES (migration 0011). Range mirrors the DB
    # CHECK (1–60) so the upsert never bounces on a constraint. The watcher reads
    # the min across users as the global open-market cadence.
    if payload.get("sweep_interval") is not None:
        try:
            out["sweep_interval"] = max(1, min(60, int(payload["sweep_interval"])))
        except (TypeError, ValueError):
            raise HTTPException(422, "sweep_interval must be an integer (minutes)")
    if payload.get("structures_enabled") is not None:
        structs = payload["structures_enabled"]
        if not isinstance(structs, (list, tuple)) or \
                not set(structs) <= set(STRUCTURES) or not structs:
            raise HTTPException(
                422, f"structures_enabled must be a non-empty subset of "
                     f"{list(STRUCTURES)}")
        out["structures_enabled"] = list(structs)
    if payload.get("regime_strictness") is not None:
        rs = str(payload["regime_strictness"])
        if rs not in _ALLOWED_STRICTNESS:
            raise HTTPException(422, f"regime_strictness must be one of {_ALLOWED_STRICTNESS}")
        out["regime_strictness"] = rs
    if payload.get("risk_profile") is not None:
        rp = str(payload["risk_profile"])
        if rp not in ("conservative", "balanced", "aggressive"):
            raise HTTPException(422, "risk_profile must be conservative|balanced|aggressive")
        out["risk_profile"] = rp
    if payload.get("notify_email") is not None:
        out["notify_email"] = bool(payload["notify_email"])
    if payload.get("gate_overrides") is not None:
        overrides = payload["gate_overrides"]
        if not isinstance(overrides, dict):
            raise HTTPException(422, "gate_overrides must be an object")
        locked = set(simmer_config.LOCKED_GATES)
        toggleable = set(simmer_config.TOGGLEABLE_GATES)
        bad_locked = sorted(set(overrides) & locked)
        if bad_locked:
            raise HTTPException(
                422, f"gate(s) {bad_locked} are locked and cannot be overridden "
                     "(safety, not preference)")
        unknown = sorted(set(overrides) - toggleable)
        if unknown:
            raise HTTPException(
                422, f"unknown gate override(s) {unknown}; toggleable gates are "
                     f"{sorted(toggleable)}")
        out["gate_overrides"] = {k: bool(v) for k, v in overrides.items()}
    return out


@router.post("/simmer/settings")
async def post_settings(payload: dict, user: dict = Depends(require_simmer_access)):
    sanitized = sanitize_settings(payload or {})
    if not sanitized:
        raise HTTPException(422, "no recognised settings in payload")
    persisted = await supabase_admin.upsert_row(
        "simmer_settings", {"user_id": user["id"], **sanitized},
        on_conflict="user_id")
    return {"settings": sanitized, "persisted": persisted}


@router.get("/simmer/settings")
async def get_settings_endpoint(user: dict = Depends(require_simmer_access)):
    row = await supabase_admin._select_one(
        "simmer_settings", "user_id", str(user["id"]),
        "min_score,min_iv_percentile,min_dte,max_dte,short_delta_min,"
        "short_delta_max,notify_email,risk_profile,structures_enabled,"
        "gate_overrides,regime_strictness,max_concurrent,sweep_interval,prefs")
    return {"settings": row or {}, "defaults": {
        "min_score": simmer_config.decision_bands()["ready"],
        "structures_enabled": list(STRUCTURES),
        "regime_strictness": "balanced",
    }}
