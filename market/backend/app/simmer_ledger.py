"""The engine → poster hand-off: one Supabase row per postable Simmer event.

When the watcher decides a transition is worth posting (``watch_entered`` /
``ready``, in-hours or the off-hours-catalyst exception), this module FREEZES
everything the post needs at that moment — the Pub/Sub attributes, the state
blocks the copy is written from, and the card's standalone HTML — and writes it
to ``public.simmer_event_ledger`` (migration 0019). Only then is the Pub/Sub
message published, so the poster never wakes up to a row that isn't there.

Why freeze: the poster used to call back into EdgeLane (/simmer/state for the
numbers, the snap service loading /simmer/snap for the image), both rendered from
the readiness cache at FETCH time — a post could show a later state than the
event it described. Now the row is the event, as it was.

Simmer-specific vs Matrix (docs/simmer_events_update.md §3): EXACTLY-ONCE publish.
The engine publishes only when its INSERT created a NEW row — a transition
re-seen across sweeps, or watched by many users, publishes once per
``(symbol, expiry, state, UTC day)`` (the ``event_id``). ``record`` reports the
new-row/duplicate distinction; ``fire`` publishes only on new. The poster's
``done`` keeps the row as a payload-cleared tombstone so a same-day re-fire can't
insert-and-republish.

Posture matches ``simmer_events`` / ``matrix_ledger``: best-effort, NEVER raises,
and the actual write + publish run OFF the poll path (``asyncio`` background task)
so a slow Supabase/Pub-Sub call can't stretch the sweep.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timezone
from typing import Any

from . import simmer_config, simmer_events, supabase_admin

log = logging.getLogger("edgelane.simmer.ledger")

TABLE = "simmer_event_ledger"

# The state blocks the poster composes from — the exact shapes /simmer/state
# returns today, so the poster's existing card mapping is reused unchanged.
_DATA_BLOCKS = ("card", "score", "gates", "sentiment", "evolution")


def freeze_snapshot(env: dict | None) -> dict:
    """A deep copy of the readiness envelope, taken synchronously at event time so
    a later poll can't mutate what the row describes. The envelope is already the
    self-contained readiness result (no giant quote maps to trim, unlike Matrix's
    snapshot)."""
    return copy.deepcopy(env or {})


def build_row(*, event_id: str, symbol: str, state: str, expiry: Any,
              attrs: dict, env: dict, db: Any = None,
              event_at: datetime | None = None) -> dict:
    """Assemble the ledger row from a FROZEN envelope. Pure apart from the
    evolution block, which reads readiness history (the same read /simmer/state
    does) — that history is frozen into the row here. Runs off the poll path."""
    from .routes.simmer import _state_block          # lazy: avoid import cycle
    from .simmer_snap import render_snap_card

    sym = str(symbol or "").upper()
    bands = simmer_config.decision_bands()
    ready = float(bands.get("ready", 70.0))
    watch = float(bands.get("watch", 50.0))

    data: dict[str, Any] = {}
    for block in _DATA_BLOCKS:
        try:
            data[block] = _state_block(env, block, db)
        except Exception:
            log.exception("[simmer-ledger] block %s failed for %s", block, sym)

    try:
        html = render_snap_card(env, ready=ready, watch=watch)
    except Exception:
        log.exception("[simmer-ledger] snap render failed for %s", sym)
        html = None

    return {
        "event_id": event_id,
        "product": "simmer",
        "symbol": sym,
        "state": str(state),
        "expiry": str(expiry or ""),
        "event_at": (event_at or datetime.now(timezone.utc)).isoformat(),
        "attributes": attrs or {},
        "data": data,
        "snap_html": html,
        "status": "pending",
    }


async def record(row: dict) -> bool | None:
    """Write the row, REPORTING whether it was new. True = created (publish),
    False = the event_id already existed (dedupe — do NOT publish), None = write
    error/unconfigured (do NOT publish; a later sweep re-fires). Best-effort,
    never raises."""
    return await supabase_admin.insert_row_report_created(TABLE, row, "event_id")


# Publishing is handed to the BACKGROUND; the sweep only decides there is
# something to say. record()+publish_transition() each wait on a network round
# trip (up to ~10s for the Pub/Sub ack), and process_alerts runs inside the poll
# path — awaiting them inline would stretch the whole sweep (the lesson Matrix
# learned). Safe to fire-and-forget: the ledger's unique event_id is the dedupe,
# so a retry can't double-post.
_INFLIGHT: set[asyncio.Task] = set()


def fire(symbol: str, state: str, expiry: Any = None, *, env: dict,
         db: Any = None, extra_attributes: dict[str, str] | None = None,
         takeaways: dict | None = None, force: bool = False) -> str:
    """Freeze the event and hand the write+publish to a background task; return
    immediately with the state name (what was HANDED OFF, not confirmed sent).

    The envelope is deep-copied HERE, synchronously, before any later poll can
    touch it. In the background: build the row (off the loop), INSERT it, and —
    only if the insert created a NEW row (exactly-once §3), or ``force`` — publish
    the Pub/Sub message with ``ledger=1`` and ``event_at`` added. No new row ⇒ no
    message.
    """
    frozen = freeze_snapshot(env)
    event_at = datetime.now(timezone.utc)
    extra = {str(k): str(v) for k, v in (extra_attributes or {}).items()}
    frozen_takeaways = copy.deepcopy(takeaways) if takeaways else None

    async def _run() -> None:
        try:
            if not simmer_events.is_enabled():
                return                              # dark until provisioned
            eid = simmer_events.event_id(symbol, state, expiry)
            row_attrs: dict[str, Any] = {
                "product": "simmer",
                "symbol": str(symbol or "").upper(),
                "state": str(state),
                "expiry": simmer_events._iso_expiry(expiry),
                "event_id": eid,
                **extra,
            }
            if frozen_takeaways is not None:
                row_attrs["takeaways"] = frozen_takeaways
            row = await asyncio.to_thread(
                build_row, event_id=eid, symbol=symbol, state=state,
                expiry=expiry, attrs=row_attrs, env=frozen, db=db,
                event_at=event_at)
            created = await record(row)
            if not created and not force:
                # Duplicate (exactly-once did its job) or a write error — either
                # way, no new row means no message.
                log.info("[simmer-ledger] %s not published (created=%s force=%s)",
                         eid, created, force)
                return
            msg_attrs = {**extra, "ledger": "1", "event_at": event_at.isoformat()}
            # The ledger already deduped, so bypass publish_transition's own
            # DuckDB dedupe (force) and don't write the legacy table (db omitted).
            await simmer_events.publish_transition(
                symbol, state, expiry, force=True, extra_attributes=msg_attrs)
        except Exception:                           # belt-and-braces; callees swallow
            log.exception("[simmer-ledger] publish task failed for %s %s", symbol, state)

    task = asyncio.create_task(_run())
    _INFLIGHT.add(task)                             # hold a ref; asyncio only weakrefs
    task.add_done_callback(_INFLIGHT.discard)
    return state


async def drain(timeout: float = 10.0) -> None:
    """Await any in-flight publishes. For tests and orderly shutdown — never
    called on the poll path, which is the whole point."""
    pending = set(_INFLIGHT)
    if pending:
        await asyncio.wait(pending, timeout=timeout)
