"""EdgeLane MARKET backend — FastAPI entry point.

Wires together config, database, Tradier client, poller, and HTTP routes.
Designed to run as a single Uvicorn process.

Run locally:
    cd market/backend
    python -m uvicorn app.main:app --host 127.0.0.1 --port 8788 --reload

When TRADIER_TOKEN (and sandbox token) are empty in the config, the service
boots with a MockTradierClient that serves a deterministic synthetic chain,
so the full UI works end-to-end without credentials. In mock mode the poller
also polls when the market is closed (so devs see output any time).
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import get_settings
from .db import Database
from .poller import poll_loop, state as poller_state
from .evaluator import evaluator_loop, rehydrate_regime as evaluator_rehydrate_regime
from .tradier_client import TradierClient
from .mock_tradier import MockTradierClient
from .external_gex import state as _external_gex_state
from . import torque_config as tcfg
from . import supabase_admin


# Module-level handles exposed for routes (avoid import cycles).
_db: Database | None = None
_tradier_account_id: str = ""
_tradier_client = None

log = logging.getLogger("edgelane.market")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _settings_view(settings, should_poll_when_closed: bool, tradier_mode: str):
    """Duck-typed view of `settings` for the poller (Pydantic Settings is
    frozen, so we copy fields into a SimpleNamespace)."""
    return SimpleNamespace(
        symbols=list(settings.symbols),
        poll_interval_sec=int(settings.poll_interval_sec),
        market_hours_tz=settings.market_hours_tz,
        market_open=settings.market_open,
        market_close=settings.market_close,
        width_preferences=list(settings.width_preferences),
        force_poll_when_closed=bool(should_poll_when_closed),
        should_poll_when_closed=bool(should_poll_when_closed),
        tradier_env=settings.tradier_env,
        tradier_mode=tradier_mode,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    log.info("EdgeLane MARKET starting up")
    log.info("  symbols=%s  poll=%ss  env=%s",
             settings.symbols, settings.poll_interval_sec, settings.tradier_env)
    log.info("  db=%s", settings.db_path_expanded)

    db = Database(settings.db_path_expanded)
    db.connect()
    app.state.db = db
    app.state.settings = settings
    global _db
    _db = db

    mode = settings.tradier_mode
    should_poll_closed = settings.should_poll_when_market_closed
    if settings.active_tradier_token:
        log.info("  tradier=%s  base=%s", mode.upper(), settings.tradier_base_url)
        client = TradierClient(settings.tradier_base_url, settings.active_tradier_token)
        app.state.using_mock = False
    else:
        log.warning("  tradier=%s  (no TRADIER_TOKEN — serving synthetic chain)", mode.upper())
        client = MockTradierClient()
        app.state.using_mock = True
    runtime_settings = _settings_view(settings, should_poll_closed, mode)
    app.state.tradier = client
    app.state.runtime_settings = runtime_settings
    global _tradier_client
    _tradier_client = client

    # Resolve Tradier account_id. Priority:
    #   1. config-provided value for the current env
    #   2. else, in live mode, fetch from /v1/user/profile and cache
    #   3. mock mode → leave blank
    global _tradier_account_id
    _tradier_account_id = settings.active_account_id
    if not _tradier_account_id and not app.state.using_mock:
        try:
            resolved = await client.resolve_account_id()
            if resolved:
                _tradier_account_id = resolved
                log.info("  tradier_account_id=%s  (auto-resolved from /v1/user/profile)", resolved)
            else:
                log.warning("  tradier_account_id=<empty>  (auto-resolve found no accounts — orders will require explicit account_id)")
        except Exception as e:
            log.warning("  tradier_account_id=<unresolved>  (auto-resolve failed: %s)", e)
    elif _tradier_account_id:
        log.info("  tradier_account_id=%s  (from config)", _tradier_account_id)
    app.state.tradier_account_id = _tradier_account_id

    # Seed operator entitlements from config (NOT hardcoded in the SQL migration):
    # grant the configured operator uid(s) the full toolset so they can use the
    # market + Torque tools from a normal signed-in session. Idempotent union;
    # no-op when unconfigured or when Supabase isn't wired.
    for _uid in tcfg.operator_uids():
        try:
            if await supabase_admin.grant_user_tools(_uid, ["market", "torque"]):
                log.info("  operator entitlements ensured for uid=%s", _uid)
        except Exception as e:
            log.warning("  operator seed failed for uid=%s: %s", _uid, e)

    poll_task = asyncio.create_task(
        poll_loop(client, db, runtime_settings),
        name="edgelane.poll_loop",
    )
    app.state.poll_task = poll_task

    # Restore per-symbol regime counters (consec loss/win + pause flag) from the
    # outcomes table BEFORE the evaluator loop starts — otherwise a restart wipes
    # an active pause / loss streak and the win-rate gets republished mid-meltdown.
    try:
        await asyncio.to_thread(evaluator_rehydrate_regime, db, settings)
    except Exception as e:
        log.warning("  regime rehydrate skipped: %s", e)

    eval_task = asyncio.create_task(
        evaluator_loop(db, poller_state, settings),
        name="edgelane.evaluator_loop",
    )
    app.state.eval_task = eval_task

    try:
        yield
    finally:
        log.info("EdgeLane MARKET shutting down")
        for t in (poll_task, eval_task):
            t.cancel()
        for t in (poll_task, eval_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await client.close()
        except Exception:
            pass
        db.close()
        globals()['_db'] = None
        globals()['_tradier_client'] = None
        globals()['_tradier_account_id'] = ""


app = FastAPI(
    title="EdgeLane MARKET",
    version="0.1.0",
    description="Continuous 0DTE bias + strategy service for index tickers",
    lifespan=lifespan,
)

_settings = get_settings()

# Per-session rate limiting. Registered BEFORE CORS so CORS stays the outermost
# middleware — that way even a 429 response carries CORS headers and the browser
# can read it. No-op when AUTH_ENABLED=false (dev). Real DDoS shield is the
# Cloudflare edge in front of the tunnel; this is app-level fairness.
from . import ratelimit  # noqa: E402
app.middleware("http")(ratelimit.rate_limit_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_allow_origins,
    allow_origin_regex=_settings.cors_allow_origin_regex,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


from .routes import (  # noqa: E402
    status, symbols, snapshot, accuracy, diag, orders, webhook, torque, session, broker, contact,
    strike_profiles,
)

app.include_router(status.router)
app.include_router(symbols.router)
app.include_router(snapshot.router)
app.include_router(accuracy.router)
app.include_router(diag.router)
app.include_router(orders.router)
app.include_router(webhook.router)
app.include_router(torque.router)
app.include_router(session.router)
app.include_router(broker.router)
app.include_router(contact.router)
# page route first so /strike-profiles/admin matches the HTML page (no JSON gate)
# before the API router's /strike-profiles/{symbol} parameterized route.
app.include_router(strike_profiles.page_router)
app.include_router(strike_profiles.router)


@app.get("/")
async def root():
    return {
        "service": "edgelane-market",
        "version": "0.1.0",
        "docs": "/docs",
        "status": "/status",
        "snapshot": "/snapshot",
        "accuracy": "/accuracy/{symbol}",
        "diag": "/diag/tradier",
        "orders": {
            "preview": "POST /orders/preview",
            "submit":  "POST /orders/submit  (body must include confirm=true)",
        },
    }
