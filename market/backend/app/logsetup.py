"""Backend-wide log verbosity (LOG_LEVEL in edgelane_market.config).

  quiet (default) — only transactions (orders placed / cancelled / modified,
                    news signals and their outcome) plus warnings and errors.
                    No per-request access lines, no per-call httpx lines, no
                    per-cycle poller chatter.
  info            — everything at INFO (the old behavior).
  debug           — everything at DEBUG.

Transactions go through the dedicated `edgelane.txn` logger (TXN below),
which stays at INFO in every mode — that's what keeps them visible in quiet.
Read once at startup: change it in the config and restart.
"""
from __future__ import annotations

import logging

TXN = logging.getLogger("edgelane.txn")

LEVELS = {"quiet": logging.WARNING, "info": logging.INFO, "debug": logging.DEBUG}
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def normalize(level: str | None) -> str:
    lvl = (level or "quiet").strip().lower()
    return lvl if lvl in LEVELS else "quiet"


def configure_logging(level: str | None) -> str:
    """Apply `level` to the root logger and the uvicorn loggers; returns the
    normalized level actually applied (unknown values fall back to quiet)."""
    lvl = normalize(level)
    root_level = LEVELS[lvl]
    logging.basicConfig(level=root_level, format=_FORMAT)
    logging.getLogger().setLevel(root_level)
    # uvicorn configures its own loggers (non-propagating) before importing
    # the app — set them explicitly or its per-request access log survives.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(root_level)
    TXN.setLevel(logging.INFO if lvl != "debug" else logging.DEBUG)
    return lvl


def txn(event: str, **fields) -> None:
    """One transaction line: `event k=v k=v …` (None fields omitted)."""
    parts = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    TXN.info("%s %s", event, parts) if parts else TXN.info("%s", event)
