"""Best-effort Pub/Sub publisher for Simmer ticker state transitions.

One message per state transition to the topic `facades.ticker-events`
(env `FACADES_EVENTS_TOPIC`), consumed by the soljet-postiz posting pipeline —
see that repo's `docs/simmer.md` "upstream contract". This is the ONLY EdgeLane
piece that touches GCP; it needs `roles/pubsub.publisher` on that topic.

Posture mirrors `emailer.py`: sending is best-effort and NEVER raises. When the
feature is disabled (`simmer_events_enabled=False`, the default), the topic or
project is unset, or the `google-cloud-pubsub` client / credentials are
unavailable, `publish_transition` logs and returns False — an events outage can
never fail or slow the sweep beyond a swallowed exception.

Message shape (attributes only; body empty):
    product   "simmer"
    symbol    e.g. "NVDA"
    state     "watch_entered" | "ready"
    expiry    the option expiration (YYYY-MM-DD), or "" when unknown
    event_id  DETERMINISTIC per (symbol, UTC day, state, expiry) so a re-publish
              carries the SAME id and the downstream dedupes it:
                  SMR-<SYM>-<YYMMDD>-<expiryYYMMDD>-<state>
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

from .config import get_settings

log = logging.getLogger("edgelane.simmer.events")

# Lazy singleton PublisherClient. Kept module-level (not per-call) so the gRPC
# channel is reused; tests monkeypatch `_get_publisher` to inject a fake.
_publisher: Any = None


def _yymmdd(value: Any) -> str:
    """Render a date/datetime/ISO-string as YYMMDD, or "" when unparseable."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%y%m%d")
    if isinstance(value, date):
        return value.strftime("%y%m%d")
    try:
        return date.fromisoformat(str(value)[:10]).strftime("%y%m%d")
    except (TypeError, ValueError):
        return ""


def event_id(symbol: str, state: str, expiry: Any = None,
             day: date | None = None) -> str:
    """Deterministic id for one (symbol, UTC day, state, expiry) transition.

    Same inputs ⇒ same id, so re-publishing a transition already sent earlier
    the same UTC day is a downstream no-op (dedupe key). `day` defaults to the
    current UTC date."""
    d = (day or datetime.now(timezone.utc).date()).strftime("%y%m%d")
    sym = str(symbol or "").upper()
    exp = _yymmdd(expiry)
    return f"SMR-{sym}-{d}-{exp}-{state}"


def _get_publisher() -> Any:
    """The PublisherClient, created lazily. Isolated so tests can stub it and so
    an import/credential failure surfaces as a swallowed exception, not a crash
    at module import."""
    global _publisher
    if _publisher is None:
        from google.cloud import pubsub_v1     # optional dep — imported lazily
        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def _publish_blocking(topic_path: str, attributes: dict[str, str]) -> None:
    publisher = _get_publisher()
    future = publisher.publish(topic_path, b"", **attributes)
    future.result(timeout=10)                  # surface publish errors here


async def publish_transition(symbol: str, state: str, expiry: Any = None,
                             *, day: date | None = None, db: Any = None,
                             force: bool = False,
                             extra_attributes: dict[str, str] | None = None,
                             takeaways: dict | None = None) -> bool:
    """Publish one ticker state transition. Returns True only if actually sent.

    Topic publishes are EXACTLY-ONCE per `event_id` (= per symbol, expiry, state,
    UTC day): when `db` is given, an already-recorded event_id is skipped (logged
    "deduped", returns False) so many watchers / re-seen sweeps of the same
    transition publish once. `force=True` bypasses the skip (the admin fire tool).
    A successful publish records the row. Email fanout is separate and per-user —
    it is NOT deduped here.

    Best-effort: disabled/unconfigured ⇒ log + return False; any client or
    network failure ⇒ log + return False. Never raises."""
    settings = get_settings()
    if not getattr(settings, "simmer_events_enabled", False):
        log.debug("[events] disabled — %s %s not published", symbol, state)
        return False
    topic = getattr(settings, "facades_events_topic", "") or ""
    project = getattr(settings, "gcp_project", "") or ""
    if not topic or not project:
        log.info("[events] topic/project unset — %s %s not published", symbol, state)
        return False

    eid = event_id(symbol, state, expiry, day)

    # Centralized topic dedupe (best-effort — a DB hiccup must not block a
    # publish, only its dedupe). Skip only when NOT forced and the row exists.
    if not force and db is not None:
        try:
            if db.simmer_published_event_exists(eid):
                log.info("[events] deduped %s (%s %s) — already published", eid, symbol, state)
                return False
        except Exception:
            log.exception("[events] dedupe check failed for %s — publishing anyway", eid)

    attributes = {
        "product": "simmer",
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
        log.info("[events] published %s (%s %s)", eid, symbol, state)
    except Exception:
        log.exception("[events] publish failed for %s %s", symbol, state)
        return False

    # Record the exactly-once key (best-effort — a record failure only means a
    # future re-publish, never a lost message).
    if db is not None:
        try:
            db.insert_simmer_published_event({
                "event_id": eid,
                "symbol": str(symbol or "").upper(),
                "expiration": _iso_expiry(expiry) or None,
                "state": str(state),
                "fired_at": datetime.now(timezone.utc).replace(tzinfo=None),
                "takeaways": takeaways or {},
            })
        except Exception:
            log.exception("[events] recording published event %s failed", eid)
    return True


def _iso_expiry(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return str(value)
