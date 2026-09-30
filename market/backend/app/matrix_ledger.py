"""The engine → poster hand-off: one Supabase row per postable Matrix event.

When matrix_signals decides an event is worth posting, this module FREEZES
everything the post needs at that moment — the event attributes, the data
blocks the copy is written from, and the card's standalone HTML — and writes it
to ``public.matrix_event_ledger`` (migration 0017). Only then is the Pub/Sub
message published, so the poster never wakes up to a row that isn't there.

Why freeze: the poster used to call back into EdgeLane (/matrix/state for the
numbers, the snap service loading /matrix/snap for the image), and both were
rendered from the snapshot at FETCH time — a post could show a later state than
the event it described. Now the row is the event, as it was.

The engine only detects and records. Reading, rendering to PNG, posting, and
keeping the ledger rolling (delete after posting, prune stale rows) are the
poster's job.
"""
from __future__ import annotations

import copy
import logging
from datetime import datetime, timezone
from typing import Any

from . import supabase_admin

log = logging.getLogger("edgelane.matrix.ledger")

TABLE = "matrix_event_ledger"

# Which card each post moment shows. pick_result and daily_recap show a PAST
# pick, so their card is rendered from that pick (card_pick), never from
# whatever the engine happens to be showing now.
STATE_VIEW = {
    "pick_selected":    "engine_pick",
    "pick_result":      "engine_pick",
    "daily_recap":      "engine_pick",
    "bias_aligned":     "bias_chip",
    "bias_diverged":    "bias_chip",
    "win_rate_notable": "win_eval_grid",
    "session_open":     "walls_chip",
    "grid_digest":      "strategy_grid",
}

# The snapshot keys a card or a data block can read. Copied (deep) at the moment
# the event fires so later polls can't change what the row describes.
_SNAP_KEYS = ("symbol", "expiration", "spot", "expected_move", "polled_at",
              "bias", "engine_pick", "strategies")


def freeze_snapshot(snap: dict | None) -> dict:
    """A deep copy of just the parts of the snapshot a post can use."""
    snap = snap or {}
    return copy.deepcopy({k: snap.get(k) for k in _SNAP_KEYS if k in snap})


def build_row(*, event_id: str, symbol: str, state: str, expiry: Any,
              attrs: dict, snap: dict, card_pick: dict | None = None,
              event_at: datetime | None = None) -> dict:
    """Assemble the ledger row. Pure apart from reading the grader's record (the
    same read the /matrix/state API does). Runs off the poll path."""
    from .routes.matrix import _accuracy_view, _state_block

    sym = str(symbol or "").upper()
    view = STATE_VIEW.get(state, "engine_pick")
    card_snap = dict(snap)
    if card_pick:                       # a past pick: its own card, not today's
        card_snap["engine_pick"] = card_pick

    data: dict[str, Any] = {}
    for block in ("pick", "grid", "bias", "win_eval", "walls"):
        try:
            data[block] = _state_block(card_snap, block, sym)
        except Exception:
            log.exception("[matrix-ledger] block %s failed for %s", block, sym)
    if card_pick:
        data["card_pick"] = card_pick

    trust = stats = None
    if view in ("bias_chip", "win_eval_grid"):
        trust, stats = _accuracy_view(sym)
    from .matrix_snap import render_snap_card
    html = render_snap_card(card_snap, view, trust=trust, stats=stats)

    return {
        "event_id": event_id,
        "product": "matrix",
        "symbol": sym,
        "state": state,
        "expiry": str(expiry or ""),
        "event_at": (event_at or datetime.now(timezone.utc)).isoformat(),
        "attributes": {str(k): str(v) for k, v in (attrs or {}).items()},
        "data": data,
        "snap_view": view,
        "snap_html": html,
    }


async def record(row: dict) -> bool:
    """Write the row. A row already there for this event_id counts as success
    (a re-fired event). Best-effort, never raises."""
    return await supabase_admin.insert_row_ignore_duplicates(TABLE, row, "event_id")
