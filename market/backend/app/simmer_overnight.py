"""Overnight-movers orchestration (docs/simmer.md → "Overnight movers").

Ties the pieces together:
  * `simmer_relativeqs.fetch_movers` — the pre-open board,
  * `simmer_premium_tracker` — the pure strike/verdict math,
  * `public.simmer_overnight` — the shared, user-agnostic analysis state,
  * `public.simmer_watchlist` — the per-user import (badged RQ).

Two entry points, both best-effort (never raise, never block the sweep):
  * `run_import(provider)` — fired ONCE at 09:25 ET: fetch movers, keep the ≥2%
    Simmer-eligible ones, snapshot each name's FROZEN baseline + a one-line
    thesis, and upsert them into every opted-in user's watchlist (override, not
    skip, on a duplicate).
  * `refresh_analysis(provider)` — fired each sweep after the open: re-read the
    same strikes live and recompute the one verdict per name.

The baseline (last-night close, captured once at 9:25) is NEVER recaptured once
set — table 1 is immutable; only `latest`/`verdict` move.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from . import simmer_config, simmer_premium_tracker as pt, simmer_relativeqs, supabase_admin

log = logging.getLogger("edgelane.simmer.overnight")

_ET = ZoneInfo("America/New_York")
MIN_GAP = 0.02                       # ignore anything that moved < 2% overnight


def session_date() -> str:
    """Today's ET trading date (the key that scopes a day's overnight analysis)."""
    return datetime.now(_ET).date().isoformat()


def eligible_movers(movers: list[dict], min_gap: float = MIN_GAP) -> list[dict]:
    """Keep whatever RelativeQs surfaces that cleared the 2% floor and is
    Simmer-tradeable — import the moving names, NOT a pre-configured subset.

    We trust RelativeQs as the Nasdaq-100 universe source (it's our sibling app).
    The injection/flood defense is at ingestion (`normalize_movers`: strict
    ^[A-Z]{1,6}$ symbol format, finite gap, 100-row cap) — NOT a membership
    allowlist, which would defeat the feature's purpose. A name Simmer has no
    per-ticker row for just uses the base rule (works for any liquid name).

    Drops here: sub-2% moves, blocked index products (SPX/NDX/RUT/DJX/VIX —
    Simmer can't trade them), and names a per-ticker rule marks ineligible."""
    out = []
    blocked = simmer_config.blocked_tickers()
    for m in movers or []:
        sym = str(m.get("symbol") or "").upper()
        if not sym or abs(float(m.get("gap") or 0.0)) < min_gap:
            continue
        if sym in blocked or not simmer_config.ticker_rule(sym).get("eligible", True):
            continue
        out.append(m)
    return out


def _templated_thesis(symbol: str, gap: float, direction: str, crowd: str | None) -> str:
    pct = abs(gap) * 100.0
    peer = ("sector-wide" if crowd in ("sector_wide", "with_market")
            else "an isolated" if crowd in ("lone", "against_market") else "a")
    return f"${symbol} gapped {direction} {pct:.1f}% overnight in {peer} move."


async def research_thesis(symbol: str, gap: float, direction: str, crowd: str | None = None,
                          summarize: Optional[Callable[..., Any]] = None,
                          headlines: Optional[list[str]] = None) -> str:
    """A ONE-LINE 'why it moved'. Seam: when a `summarize` callable (Gemini with
    Google-Search grounding, prose-only) is injected, use it; else fold the top
    overnight headline in; else a factual templated line. Best-effort → the
    templated line on any failure. Never feeds scoring (Simmer's prose-only rule)."""
    base = _templated_thesis(symbol, gap, direction, crowd)
    try:
        if summarize is not None:
            out = summarize(symbol=symbol, gap=gap, direction=direction, headlines=headlines)
            out = await out if hasattr(out, "__await__") else out
            if out:
                return str(out).strip().splitlines()[0][:240]
        if headlines:
            return f"{base} Top headline: {str(headlines[0])[:160]}"
    except Exception:                       # noqa: BLE001 — best-effort narration
        log.warning("[overnight] thesis research failed for %s", symbol)
    return base


async def capture_baseline(provider, symbol: str, expiration: str, direction: str) -> dict:
    """Snapshot the strikes toward the move + their last-night-close premiums and
    the underlying level. Called ONCE at 9:25 (market closed → quotes are the
    prior close). Returns {} on any data failure."""
    try:
        chain = await provider.chain(symbol, expiration)
    except Exception as e:                   # noqa: BLE001
        log.warning("[overnight] baseline chain failed for %s: %s", symbol, e)
        return {}
    spot = None
    try:
        spot = (await provider.quote(symbol)).get("last")
    except Exception:                        # noqa: BLE001
        pass
    if not spot:
        return {}
    side = pt.toward_move_side(direction)
    strikes = pt.pick_strikes(float(spot), chain, direction)
    if not strikes:
        return {}
    return {
        "side": side,
        "strikes": strikes,
        "baseline": pt.premium_map(chain, strikes, side),
        "spot_baseline": float(spot),
    }


async def refresh_live(provider, symbol: str, expiration: str,
                       strikes: list[float], side: str) -> dict:
    """Re-read the SAME strikes live (premium + volume) + the live underlying."""
    try:
        chain = await provider.chain(symbol, expiration)
    except Exception as e:                   # noqa: BLE001
        log.warning("[overnight] live chain failed for %s: %s", symbol, e)
        return {}
    spot_now = None
    try:
        spot_now = (await provider.quote(symbol)).get("last")
    except Exception:                        # noqa: BLE001
        pass
    return {
        "latest": pt.premium_map(chain, strikes, side),
        "volume": pt.volume_map(chain, strikes, side),
        "spot_now": float(spot_now) if spot_now else None,
    }


async def _next_expiry(provider, symbol: str) -> Optional[str]:
    try:
        exps = await provider.expirations(symbol)
    except Exception:                        # noqa: BLE001
        return None
    if not exps:
        return None
    from .simmer_watcher import choose_expiration    # lazy — avoid import cycle
    return choose_expiration(sorted({str(e)[:10] for e in exps if e}), None, simmer_config.gates())


async def _ensure_overnight_row(provider, mover: dict, expiration: str, session: str,
                                summarize=None) -> bool:
    """Create the user-agnostic analysis row once: frozen baseline + thesis.
    Never recaptures a baseline that already exists. Returns True when a usable
    overnight row exists (baseline captured now or already present), False when we
    couldn't get a baseline — so the caller can skip importing an unanalyzable
    name into watchlists."""
    sym = mover["symbol"]
    existing = await supabase_admin.get_simmer_overnight(sym, expiration, session)
    if existing and existing.get("baseline"):
        return True                           # baseline is immutable, already have it
    snap = await capture_baseline(provider, sym, expiration, mover["dir"])
    if not snap:
        return False
    thesis = await research_thesis(sym, mover["gap"], mover["dir"], mover.get("crowd"),
                                   summarize=summarize)
    verdict = pt.analyze(direction=mover["dir"], crowd=mover.get("crowd"),
                         gap=float(mover["gap"]), baseline=snap["baseline"], latest=None,
                         spot_baseline=snap["spot_baseline"])
    await supabase_admin.upsert_simmer_overnight({
        "symbol": sym, "expiration": expiration, "session_date": session,
        "dir": mover["dir"], "gap": float(mover["gap"]), "crowd": mover.get("crowd"),
        "thesis": thesis, "strikes": snap["strikes"], "side": snap["side"],
        "baseline": snap["baseline"], "spot_baseline": snap["spot_baseline"],
        "latest": {}, "verdict": verdict,
    })
    return True


async def _upsert_watchlist_rq(user_id: str, mover: dict, expiration: str) -> None:
    """Attach the overnight overlay to the user's watchlist for this name.

    NON-DESTRUCTIVE on a row the user already owns: if the name is already on
    their list, ONLY merge `overrides.overnight` — never touch their chosen
    expiration, their active flag, or flip source 'user'→'rq' (that would lose
    their pinned expiry, re-activate a name they switched off, and make a later
    'remove rq rows' cleanup delete their own row). A brand-new name is inserted
    as source='rq', pinned to the next expiry."""
    sym = mover["symbol"]
    existing = await supabase_admin.get_watchlist_row(user_id, sym)
    overnight = {
        "gap": float(mover["gap"]), "dir": mover["dir"], "crowd": mover.get("crowd"),
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    if existing:
        overrides = dict(existing.get("overrides") or {})
        overrides["overnight"] = overnight
        # Only `overrides` in the payload → on-conflict updates ONLY that column;
        # active / source are left exactly as the user had them. EXCEPTION: if the
        # row has NO expiration pinned (it would sit "pending" forever with no
        # chain to evaluate), auto-assign the overnight's next-available expiry so
        # it gets a readiness card — without clobbering a user's deliberate pin.
        payload = {"user_id": user_id, "symbol": sym, "overrides": overrides}
        if not existing.get("expiration"):
            payload["expiration"] = expiration
        await supabase_admin.upsert_row("simmer_watchlist", payload,
                                        on_conflict="user_id,symbol")
    else:
        await supabase_admin.upsert_row("simmer_watchlist", {
            "user_id": user_id, "symbol": sym, "expiration": expiration,
            "active": True, "source": "rq", "overrides": {"overnight": overnight},
        }, on_conflict="user_id,symbol")


async def run_import(provider, *, summarize=None) -> dict:
    """The 09:25 ET pre-open job. Best-effort; returns a small summary dict."""
    from .config import get_settings
    base = await supabase_admin.get_app_config("relativeqs_api_base") \
        or getattr(get_settings(), "relativeqs_api_base", None)
    token = await supabase_admin.get_app_config("relativeqs_api_token")   # optional, future
    movers = eligible_movers(await simmer_relativeqs.fetch_movers(base, token))
    if not movers:
        return {"movers": 0, "imported": 0, "analyzed": 0}
    # Toggle 1: import EVERY mover into these users' watchlists (adds new names).
    import_users = await supabase_admin.users_with_overnight_import() or []
    # Toggle 2 (default ON): for these users, analyze ONLY movers they ALREADY
    # watch — the intersection of their watchlist and today's RQ board. Never
    # imports a new name; only attaches the overnight overlay to an existing row.
    analyze_users = [u for u in (await supabase_admin.users_with_overnight_analyze_all() or [])
                     if u not in set(import_users)]
    session = session_date()
    imported = 0
    analyzed = 0
    rows = 0
    for m in movers:
        exp = await _next_expiry(provider, m["symbol"])
        if not exp:
            continue
        # The analysis row (frozen baseline + thesis) is user-agnostic and must
        # exist before any user can see the Overnight tab. Only create it for a
        # name we can actually analyze (listed options + a usable baseline).
        if not await _ensure_overnight_row(provider, m, exp, session, summarize=summarize):
            continue
        rows += 1
        for uid in import_users:                       # toggle 1 — import the name
            await _upsert_watchlist_rq(uid, m, exp)
            imported += 1
        for uid in analyze_users:                      # toggle 2 — intersection only
            if await supabase_admin.get_watchlist_row(uid, m["symbol"]):
                # Non-destructive: _upsert_watchlist_rq only merges overrides.overnight
                # on an existing row — never adds a name, never touches gate scoring.
                await _upsert_watchlist_rq(uid, m, exp)
                analyzed += 1
    log.info("[overnight] import: %d movers, %d rows, %d imported, %d analyzed (intersection)",
             len(movers), rows, imported, analyzed)
    return {"movers": len(movers), "rows": rows, "imported": imported, "analyzed": analyzed,
            "import_users": len(import_users), "analyze_users": len(analyze_users)}


async def reset_rq_end_of_day() -> dict:
    """End-of-day RQ reset (product decision): DELETE purely-RQ watchlist rows
    (source='rq') and STRIP the overnight overlay from user-owned rows, so RQ
    status clears and tomorrow re-imports whatever RelativeQs surfaces.

    The ANALYSIS is explicitly NOT touched: simmer_overnight (frozen baseline +
    premium verdict) and simmer_overnight_analysis (llm/combiner/outcome) are
    separate history tables that persist for later reference/plots. Best-effort;
    returns a small summary. Idempotent: a second run finds no rq rows/overlays."""
    rows = await supabase_admin.select_many(
        "simmer_watchlist", "id,user_id,symbol,source,overrides", {}) or []
    deleted = stripped = 0
    if any(r.get("source") == "rq" for r in rows):
        if await supabase_admin.delete_rows("simmer_watchlist", {"source": "eq.rq"}):
            deleted = sum(1 for r in rows if r.get("source") == "rq")
    for r in rows:
        if r.get("source") == "rq":
            continue                           # already deleted above
        ov = r.get("overrides") or {}
        if isinstance(ov, dict) and ov.get("overnight"):
            cleaned = {k: v for k, v in ov.items() if k != "overnight"}
            await supabase_admin.upsert_row("simmer_watchlist", {
                "user_id": r["user_id"], "symbol": r["symbol"], "overrides": cleaned,
            }, on_conflict="user_id,symbol")
            stripped += 1
    log.info("[overnight] RQ EOD reset: %d rq rows deleted, %d overlays stripped "
             "(analysis preserved)", deleted, stripped)
    return {"deleted": deleted, "stripped": stripped}


def _news_bad(mover_dir: str, sentiment: Optional[dict]) -> Optional[bool]:
    """Fold the already-graded news sentiment into a decisive-bad hint, if present."""
    if not sentiment:
        return None
    score = sentiment.get("score")
    if score is None:
        return None
    try:
        return float(score) < -0.1
    except (TypeError, ValueError):
        return None


async def refresh_analysis(provider, *, sentiment_by_symbol: Optional[dict] = None) -> int:
    """Each post-open sweep: re-read the same strikes for today's overnight rows
    and recompute the one verdict. Returns the number of rows updated."""
    session = session_date()
    rows = await supabase_admin.list_simmer_overnight(session)
    if not rows:
        return 0
    updated = 0
    for row in rows:
        if not row.get("baseline") or not row.get("strikes"):
            continue
        live = await refresh_live(provider, row["symbol"], row["expiration"],
                                  row["strikes"], row.get("side") or pt.toward_move_side(row["dir"]))
        if not live:
            continue
        news_bad = _news_bad(row.get("dir"), (sentiment_by_symbol or {}).get(row["symbol"]))
        verdict = pt.analyze(
            direction=row["dir"], crowd=row.get("crowd"), gap=float(row.get("gap") or 0.0),
            baseline=row["baseline"], latest=live["latest"],
            spot_baseline=float(row.get("spot_baseline") or 0.0),
            spot_now=live.get("spot_now"), news_bad=news_bad)
        # NOTE: this is an INSERT-on-conflict (PostgREST upsert), so the payload
        # must satisfy the table's NOT NULL columns (dir, gap) even though the row
        # already exists — carry them from the row being refreshed. On conflict,
        # only the provided columns update; baseline/strikes/thesis are preserved.
        await supabase_admin.upsert_simmer_overnight({
            "symbol": row["symbol"], "expiration": row["expiration"], "session_date": session,
            "dir": row["dir"], "gap": float(row.get("gap") or 0.0),
            "latest": live["latest"], "volume": live["volume"], "verdict": verdict,
        })
        updated += 1
    return updated
