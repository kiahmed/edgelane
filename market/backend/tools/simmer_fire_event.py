#!/usr/bin/env python3
"""Admin end-to-end "fire a test alert" for Simmer.

Makes the engine fire a GENUINE alert — the real topic publish AND the real
readiness email — without waiting for live market conditions, so the whole
downstream posting pipeline (soljet-postiz) can be exercised on demand.

    python tools/simmer_fire_event.py --state ready
    python tools/simmer_fire_event.py --state watch --no-email     # topic only

Ticker: the first active ticker from the DB/config ticker list, else NVDA.
Expiry: the nearest listed expiry via the data provider, else a synthetic
        ~14-DTE Friday.
Fires:  (1) publishes via simmer_events.publish_transition(force=True) with a
            Pub/Sub attribute test="true" so the poster can tell it's a test;
        (2) emails the readiness card to the admin (SIMMER_FIRE_ADMIN_EMAIL,
            default kahmed@solutionjet.net) via the real renderer + emailer.
Records the exactly-once row (fired_at + takeaways) and prints a summary.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app import emailer
from app import simmer_config
from app import simmer_email
from app import simmer_events
from app.config import get_settings

DEFAULT_ADMIN_EMAIL = "kahmed@solutionjet.net"
# CLI state → the topic event state.
_EVENT_STATE = {"watch": "watch_entered", "ready": "ready"}


def resolve_tickers(db: Any = None) -> list[str]:
    """Candidate tickers, DB/config first. DuckDB holds no ticker table (the
    watchlist lives in Supabase), so the offline, deterministic source is the
    Simmer config ticker list; a caller may inject `db` for a richer source."""
    try:
        return [str(t).upper() for t in (simmer_config.tickers() or [])]
    except Exception:
        return []


def pick_ticker(candidates: list[str] | None) -> str:
    """First non-empty candidate, else NVDA."""
    for t in candidates or []:
        if t and str(t).strip():
            return str(t).strip().upper()
    return "NVDA"


def synthetic_expiry(days: int = 14, today: date | None = None) -> str:
    """A plausible ~`days`-out Friday (weekly options expire Friday)."""
    d = (today or date.today()) + timedelta(days=days)
    d += timedelta(days=(4 - d.weekday()) % 7)      # advance to the next Friday
    return d.isoformat()


def nearest_expiry(exps: list[str] | None, today: date | None = None) -> str:
    """Nearest listed expiry on/after today, else a synthetic ~14-DTE Friday."""
    t = today or date.today()
    listed = sorted(e for e in (exps or []) if e)
    for e in listed:
        try:
            if date.fromisoformat(str(e)[:10]) >= t:
                return str(e)[:10]
        except (TypeError, ValueError):
            continue
    return synthetic_expiry(today=t)


def build_envelope(symbol: str, expiry: str, state: str,
                   today: date | None = None) -> dict:
    """A synthetic readiness envelope with plausible KEY-TAKEAWAY fields, enough
    for the email renderer and the topic takeaways. Marked `_test_fire` so nothing
    downstream mistakes it for a live verdict."""
    sym = symbol.upper()
    ready = state == "ready"
    t = today or date.today()
    try:
        dte = max(0, (date.fromisoformat(str(expiry)[:10]) - t).days)
    except (TypeError, ValueError):
        dte = 14
    spot = 100.0
    short, long = 95.0, 90.0        # bull put spread, short 95 / long 90
    return {
        "symbol": sym,
        "expiration": expiry,
        "dte": float(dte),
        "spot": spot,
        "decision": "ready" if ready else "watch",
        "score": 78.0 if ready else 58.0,
        "structure": "bull_put",
        "strikes": {"short": short, "long": long, "width": short - long},
        "credit_mid": 0.95,
        "credit_fill": 0.90,
        "max_loss": (short - long) - 0.90,
        "pop_breakeven": 0.78,
        "expected_value": 0.06,
        "alpha": 0.015,
        "components": {
            "structural_safety": {"score": 0.82, "weight": 0.30},
            "volatility_richness": {"score": 0.71, "weight": 0.20,
                                    "vrp": 1.35, "iv_percentile": 62.0},
            "sentiment_lean": {"score": 1.0, "weight": 0.05, "sentiment_score": 0.1},
        },
        "veto_reasons": [],
        "avoid_if": [],
        "metrics": {
            "vrp": 1.35,
            "iv_percentile_effective": 62.0,
            "em_1sd": 5.5,             # expected-move band, in points
            "atm_iv": 0.33,
            "dte_tier": simmer_engine_tier(dte),
        },
        "regime": {"state": "contango"},
        "earnings": None,
        "management": _management(),
        "computed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "_test_fire": True,
    }


def simmer_engine_tier(dte: float) -> str:
    try:
        from app import simmer_engine
        return simmer_engine.classify_dte_tier(dte)
    except Exception:
        return "6-21"


def _management() -> dict:
    try:
        return dict(simmer_config.management())
    except Exception:
        return {"profit_target_pct": 50.0, "manage_dte": 21, "stop_credit_multiple": 2.0}


def _takeaways(env: dict, event_state: str) -> dict:
    return {
        "symbol": env.get("symbol"),
        "expiry": env.get("expiration"),
        "state": event_state,
        "score": env.get("score"),
        "structure": env.get("structure"),
        "decision": env.get("decision"),
    }


async def fire(state: str = "ready", *, symbol: str | None = None,
               expiry: str | None = None, publish: bool = True,
               email: bool = True, db: Any = None, provider: Any = None,
               admin_email: str | None = None,
               today: date | None = None) -> dict:
    """Run the real fire paths. Returns a summary dict. Never raises for a
    publish/email outage — those degrade to published=False / email_sent=False."""
    state = state if state in _EVENT_STATE else "ready"
    event_state = _EVENT_STATE[state]
    sym = pick_ticker([symbol] if symbol else resolve_tickers(db))

    if not expiry:
        exps = None
        if provider is not None and hasattr(provider, "expirations"):
            try:
                exps = await provider.expirations(sym)
            except Exception:
                exps = None
        expiry = nearest_expiry(exps, today=today)

    env = build_envelope(sym, expiry, state, today=today)
    eid = simmer_events.event_id(sym, event_state, expiry)
    admin = admin_email or os.environ.get("SIMMER_FIRE_ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL)

    # Report new-vs-deduped BEFORE the forced publish rewrites the ledger row.
    deduped = False
    if db is not None:
        try:
            deduped = db.simmer_published_event_exists(eid)
        except Exception:
            deduped = False

    published = False
    if publish:
        # force=True so the test always reaches the topic; test="true" lets the
        # poster distinguish a manual fire from a live transition.
        published = await simmer_events.publish_transition(
            sym, event_state, expiry, db=db, force=True,
            extra_attributes={"test": "true"}, takeaways=_takeaways(env, event_state))

    email_sent = False
    if email:
        try:
            settings = get_settings()
            subject, html = simmer_email.render_readiness_email(
                env, app_url=getattr(settings, "simmer_app_url", ""))
            email_sent = await emailer.send_email(
                admin, subject, html,
                from_email=getattr(settings, "simmer_alert_from_email", None))
        except Exception:
            email_sent = False

    return {
        "fired_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "event_id": eid,
        "symbol": sym,
        "expiry": expiry,
        "state": event_state,
        "published": bool(published),
        "topic": ("new" if (published and not deduped) else
                  "republished" if published else "skipped"),
        "was_already_published": bool(deduped),
        "email_sent": bool(email_sent),
        "admin_email": admin if email else None,
    }


def _build_runtime() -> tuple[Any, Any]:
    """(db, provider) from live config — used by the CLI, not the tests."""
    from app.db import Database
    from app.tradier_client import TradierClient
    from app.mock_tradier import MockTradierClient
    from app.simmer_data_provider import get_simmer_provider

    settings = get_settings()
    db = Database(settings.db_path_expanded)
    db.connect()
    if settings.active_tradier_token:
        client = TradierClient(settings.tradier_base_url, settings.active_tradier_token)
    else:
        client = MockTradierClient()
    return db, get_simmer_provider(settings, client)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fire a Simmer test alert end-to-end.")
    ap.add_argument("--state", choices=("watch", "ready"), default="ready")
    ap.add_argument("--symbol", default=None, help="override ticker (default: DB/config first, else NVDA)")
    ap.add_argument("--no-publish", action="store_true", help="skip the topic publish")
    ap.add_argument("--no-email", action="store_true", help="skip the admin email")
    args = ap.parse_args(argv)

    db = provider = None
    try:
        db, provider = _build_runtime()
    except Exception as e:            # keep firing even if one runtime piece is unavailable
        print(f"! runtime init partial ({type(e).__name__}: {e}) — continuing")

    summary = asyncio.run(fire(
        args.state, symbol=args.symbol, publish=not args.no_publish,
        email=not args.no_email, db=db, provider=provider))

    print("── Simmer test fire ─────────────────────────────")
    for k in ("fired_at", "event_id", "symbol", "expiry", "state",
              "topic", "was_already_published", "published",
              "email_sent", "admin_email"):
        print(f"  {k:22} {summary.get(k)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
