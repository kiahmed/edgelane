"""Level Engine container entrypoint — `python -m level_engine.main` (docs §3.1).

One asyncio loop runs three things:
  * the READ listener  (create_read_app)  on 0.0.0.0:READ_PORT  (compose network only)
  * the ADMIN listener (create_admin_app) on 0.0.0.0:ADMIN_PORT (host loopback only)
  * the tick loop: fetch the backend feed → is_new → TickRunner.process; when
    nothing new arrived, TickRunner.maybe_close (session-close fallback).

The feed is fetched every TICK_SEC/4 (min 1 s), not every TICK_SEC: polling at
the poll's own cadence drifts against it and would skip polls; a faster fetch
sees every new ts within a few seconds and `is_new` drops the repeats.

Store calls are synchronous (psycopg), so each loop step runs in
asyncio.to_thread and the listeners stay responsive. Each consumer (tick loop,
read API, admin) gets its own PgStore connection behind a lock that
serialises calls and reconnects after a connection error. An exception in a
step is logged and the loop carries on. SIGTERM / SIGINT → graceful shutdown.

Startup refuses to run with an empty LVL_DB_URL unless
LEVEL_ENGINE_MEMORY_STORE=1 (local dev: an in-memory store, nothing durable).
On start the runner warm-starts from today's recorded ticks (restart safety).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
from typing import Any, Callable

import uvicorn

from .admin import create_admin_app
from .clock import now_utc, today_et
from .config import load_config
from .feed import FeedClient
from .read_api import create_read_app
from .replay import load_brain
from .store import MemoryStore
from .tick import TickRunner

log = logging.getLogger("level_engine.main")

MEMORY_ENV = "LEVEL_ENGINE_MEMORY_STORE"
FETCH_DIVISOR = 4


class StartupError(RuntimeError):
    pass


def _is_conn_error(e: Exception) -> bool:
    return type(e).__name__ in ("OperationalError", "InterfaceError")


class SyncedStore:
    """Serialises calls to one store (one DB connection) across threads and
    rebuilds the connection on the next call after a connection error."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._lock = threading.RLock()
        self._s = factory()

    def _get(self):
        if self._s is None:
            self._s = self._factory()
        return self._s

    def close(self) -> None:
        with self._lock:
            if self._s is not None and hasattr(self._s, "close"):
                with contextlib.suppress(Exception):
                    self._s.close()
            self._s = None

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        if not callable(getattr(self._get(), name)):
            return getattr(self._get(), name)

        def call(*a, **kw):
            with self._lock:
                try:
                    return getattr(self._get(), name)(*a, **kw)
                except Exception as e:
                    if _is_conn_error(e):
                        log.warning("store connection error (%s) — reconnecting next call",
                                    type(e).__name__)
                        self.close()
                    raise
        return call


class _Server(uvicorn.Server):
    """uvicorn server that leaves signal handling to us (two servers in one loop)."""

    @contextlib.contextmanager
    def capture_signals(self):     # uvicorn >= 0.29
        yield

    def install_signal_handlers(self) -> None:   # older uvicorn
        return None


def _make_stores(cfg) -> tuple[Any, Any, Any]:
    if os.environ.get(MEMORY_ENV) == "1":
        log.warning("%s=1 — in-memory store: NOTHING is persisted (local dev only)", MEMORY_ENV)
        shared = SyncedStore(MemoryStore)
        return shared, shared, shared
    if not cfg.lvl_db_url:
        raise StartupError(
            "LVL_DB_URL is empty in facades_levels_engine.config — the level engine needs the "
            "facades_levels_engine_rw Postgres DSN (docs/level_engine.md §5.1). For local dev "
            f"without a DB set {MEMORY_ENV}=1.")
    from .store import PgStore

    def factory():
        return PgStore(cfg.lvl_db_url)
    try:
        return SyncedStore(factory), SyncedStore(factory), SyncedStore(factory)
    except Exception as e:
        raise StartupError(f"cannot connect with LVL_DB_URL ({type(e).__name__})") from e


async def amain() -> int:
    cfg = load_config()
    tick_store, read_store, admin_store = _make_stores(cfg)
    if not cfg.internal_feed_token:
        log.warning("INTERNAL_FEED_TOKEN is empty — the backend refuses the feed (404); "
                    "no ticks will be recorded")
    feed = FeedClient(cfg.backend_feed_url, cfg.internal_feed_token, cfg.symbol,
                      tick_sec=cfg.tick_sec)
    brain = load_brain(cfg)
    log.info("brain: %s", "loaded" if brain is not None else "none (phase 1: ticks only)")
    runner = TickRunner(cfg, tick_store, brain=brain)
    try:
        n = await asyncio.to_thread(runner.warm_start, today_et())
        if n:
            log.info("warm start: replayed %d recorded ticks for %s", n, runner.trade_date)
    except Exception:
        log.exception("warm start failed — starting the day cold")

    read_app = create_read_app(runner, store=read_store)
    admin_app = create_admin_app(admin_store, cfg, prior_close=feed.prior_close)
    servers = [
        _Server(uvicorn.Config(read_app, host="0.0.0.0", port=cfg.read_port, lifespan="off",
                               log_level="info", access_log=False)),
        _Server(uvicorn.Config(admin_app, host="0.0.0.0", port=cfg.admin_port, lifespan="off",
                               log_level="info", access_log=False)),
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal(sig):
        log.info("received %s — shutting down", signal.Signals(sig).name)
        stop.set()
        for s in servers:
            s.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _on_signal, sig)

    fetch_sec = max(1.0, cfg.tick_sec / FETCH_DIVISOR)

    def _step() -> dict | None:
        doc = feed.fetch()
        runner.set_feed_status(feed.status)
        if doc is not None and feed.is_new(doc):
            out = runner.process(doc)
            runner.set_feed_status(feed.status)
            return out
        return runner.maybe_close(doc, now_utc())

    async def tick_loop():
        last_state = None
        while not stop.is_set():
            try:
                out = await asyncio.to_thread(_step)
                if out and out.get("session_close"):
                    log.info("session closed for %s", out.get("trade_date"))
                elif out and out.get("transitions"):
                    log.info("tick %s spot=%s: %d transition(s)", out["ts"], out["spot"],
                             len(out["transitions"]))
            except Exception:
                log.exception("tick step failed — continuing")
            st = runner.feed_status.get("state")
            if st != last_state:
                (log.info if st == "ok" else log.warning)(
                    "feed %s (%s)%s", st, feed.url,
                    f" error={runner.feed_status.get('error')}" if runner.feed_status.get("error")
                    else "")
                last_state = st
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=fetch_sec)

    log.info("level engine up: read :%d, admin :%d, feed %s every %.1fs",
             cfg.read_port, cfg.admin_port, feed.url, fetch_sec)
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    tasks.append(asyncio.create_task(tick_loop()))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():       # a listener died (e.g. port in use) → stop everything
            for t in done:
                if t.exception():
                    log.error("component failed: %r", t.exception())
            _on_signal(signal.SIGTERM)
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        feed.close()
        for s in {id(x): x for x in (tick_store, read_store, admin_store)}.values():
            s.close()
    log.info("level engine stopped")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(amain())
    except StartupError as e:
        print(f"level_engine: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
