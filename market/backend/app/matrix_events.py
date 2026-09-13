"""Best-effort Pub/Sub publisher for Matrix engine state transitions.

Mirrors `app/simmer_events.py` but on Matrix's OWN topic `facades.matrix-events`
(env `MATRIX_EVENTS_TOPIC`) — an explicit design decision on the Postiz side, not
Simmer's `facades.ticker-events`. Consumed by the soljet-postiz pipeline
(docs/matrix_events_update.md). Needs `roles/pubsub.publisher` on that topic.

Posture matches `emailer.py` / `simmer_events.py`: sending is best-effort and
NEVER raises. Disabled (`matrix_events_enabled=False`, the default), topic/project
unset, or the `google-cloud-pubsub` client / credentials missing ⇒ log + return
False. An events outage can never fail or slow the evaluator sweep.

The six states fire on TRANSITIONS from the evaluator sweep, so each is published
once; the deterministic `event_id` makes any re-publish a downstream no-op — no
local dedupe table needed.

Message shape (attributes only; body empty):
    product   "matrix"
    symbol    e.g. "NVDA"
    state     one of the 6 in docs/matrix_events_update.md §2
    expiry    option expiration (YYYY-MM-DD), or "" when unknown
    event_id  MTX-<SYM>-<YYMMDD>-<state>   (deterministic per symbol+UTC day+state)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

from .config import get_settings

log = logging.getLogger("edgelane.matrix.events")

# Lazy singleton PublisherClient — reused gRPC channel; tests stub `_get_publisher`.
_publisher: Any = None


def event_id(symbol: str, state: str, day: date | None = None) -> str:
    """Deterministic id per (symbol, UTC day, state). Same inputs ⇒ same id, so a
    re-published transition the same UTC day dedupes downstream."""
    d = (day or datetime.now(timezone.utc).date()).strftime("%y%m%d")
    return f"MTX-{str(symbol or '').upper()}-{d}-{state}"


def _iso_expiry(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return str(value)


def _get_publisher() -> Any:
    global _publisher
    if _publisher is None:
        from google.cloud import pubsub_v1     # optional dep — imported lazily
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def _publish_blocking(topic_path: str, attributes: dict[str, str]) -> None:
    publisher = _get_publisher()
    future = publisher.publish(topic_path, b"", **attributes)
    future.result(timeout=10)


async def publish_transition(symbol: str, state: str, expiry: Any = None,
                             *, day: date | None = None,
                             extra_attributes: dict[str, str] | None = None) -> bool:
    """Publish one Matrix state transition. Returns True only if actually sent.

    Best-effort: disabled/unconfigured ⇒ log + return False; any client/network
    failure ⇒ log + return False. Never raises — a Pub/Sub hiccup must not stall
    the evaluator sweep."""
    settings = get_settings()
    if not getattr(settings, "matrix_events_enabled", False):
        log.debug("[matrix-events] disabled — %s %s not published", symbol, state)
        return False
    topic = getattr(settings, "matrix_events_topic", "") or ""
    project = getattr(settings, "gcp_project", "") or ""
    if not topic or not project:
        log.info("[matrix-events] topic/project unset — %s %s not published", symbol, state)
        return False

    eid = event_id(symbol, state, day)
    attributes = {
        "product": "matrix",
        "symbol": str(symbol or "").upper(),
        "state": str(state),
        "expiry": _iso_expiry(expiry),
        "event_id": eid,
    }
    if extra_attributes:
        attributes.update({str(k): str(v) for k, v in extra_attributes.items()})
    try:
        publisher = _get_publisher()
        topic_path = publisher.topic_path(project, topic)
        await asyncio.to_thread(_publish_blocking, topic_path, attributes)
        log.info("[matrix-events] published %s (%s %s)", eid, symbol, state)
        return True
    except Exception:
        log.exception("[matrix-events] publish failed for %s %s", symbol, state)
        return False
