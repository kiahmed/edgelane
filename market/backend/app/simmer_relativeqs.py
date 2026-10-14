"""RelativeQs overnight-movers client (docs/simmer.md → "Overnight movers").

Reads the sibling RelativeQs app's pre-open board — `GET {base}/api/premarket` —
and normalizes the `movers[]` it returns into the fields Simmer's import job uses.

That endpoint is UNAUTHENTICATED today (the RelativeQs UI calls it with no token;
only its Pro routes need a bearer). So `fetch_movers` needs only the base URL,
read from `public.app_config` key `relativeqs_api_base`. A bearer is accepted and
sent when configured, so a future lockdown on the RelativeQs side is a one-liner
here (tracked in the RelativeQs repo's operating manual checklist).

Best-effort, like the rest of the off-hours path: any failure logs and returns an
empty list — a missing board just means no import that session, never a crash.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Any

import httpx

log = logging.getLogger("edgelane.simmer.relativeqs")

_TIMEOUT = 20.0        # per-attempt; RelativeQs can be slow computing the board at the open
# Hard caps / shape guards on an EXTERNAL board we don't fully trust (even a
# sibling app can be compromised or buggy): a well-formed ticker only, a finite
# gap, and a bounded number of rows — so a hostile board can't inject junk
# symbols or flood the import. Eligibility against Simmer's own DB-held universe
# is enforced downstream in simmer_overnight.eligible_movers.
_SYM_RE = re.compile(r"^[A-Z]{1,6}$")
_MAX_MOVERS = 100
_MAX_BODY_BYTES = 1_000_000           # reject an oversized response body


def normalize_movers(payload: Any) -> list[dict]:
    """The RelativeQs premarket board → a flat list of movers. Pure; safe on junk.

    Each normalized mover: {symbol, gap (signed fraction), dir ('up'/'down'),
    crowd, sector, vs_tape, sustain}. Symbols must match ^[A-Z]{1,6}$ and gaps
    must be finite; malformed rows are dropped and the list is capped at
    `_MAX_MOVERS`. `crowd` is RelativeQs's peer tag, which Simmer reuses."""
    movers = (payload or {}).get("movers") if isinstance(payload, dict) else None
    out: list[dict] = []
    for m in movers or []:
        if not isinstance(m, dict):
            continue
        sym = str(m.get("symbol") or "").strip().upper()
        gap = m.get("gap")
        if not sym or not _SYM_RE.match(sym) or gap is None:
            continue
        try:
            gap = float(gap)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(gap):
            continue
        out.append({
            "symbol": sym,
            "gap": gap,
            "dir": str(m.get("dir") or ("up" if gap > 0 else "down")),
            "crowd": m.get("crowd"),
            "sector": m.get("sector"),
            "vs_tape": m.get("vs_tape"),
            "sustain": m.get("sustain"),
        })
        if len(out) >= _MAX_MOVERS:
            break
    return out


async def fetch_movers(base_url: str | None, token: str | None = None,
                       timeout: float = _TIMEOUT, attempts: int = 5) -> list[dict]:
    """GET {base}/api/premarket and normalize it into the full board.

    RelativeQs serves the WHOLE board (it tracks movers from ~04:00) in one
    response, so a short board here means our read was disrupted, not that the
    board is small. We therefore retry SYNCHRONOUSLY until we get a CONFIRMED
    complete payload — HTTP 200 with a well-formed body whose `movers` is a list —
    rather than silently accepting a truncated/timed-out read. A transient failure
    (timeout, 5xx, connection drop, malformed/partial JSON) is retried with
    backoff; a definitive 4xx (e.g. 401) or an oversized body is NOT retried. Only
    after all attempts fail do we return [] (no import that cycle, logged loudly)."""
    base = (base_url or "").rstrip("/")
    if not base:
        log.info("[relativeqs] no base URL configured — skipping overnight import")
        return []
    url = f"{base}/api/premarket"
    headers = {"User-Agent": "edgelane-simmer/1.0", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(max(1, attempts)):
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(url, headers=headers)
            if 400 <= r.status_code < 500:       # auth/not-found → no point retrying
                log.warning("[relativeqs] %s -> HTTP %s (not retried)", url, r.status_code)
                return []
            if r.status_code >= 500:             # server busy computing → retry
                raise RuntimeError(f"HTTP {r.status_code}")
            clen = r.headers.get("content-length")
            if (clen and int(clen) > _MAX_BODY_BYTES) or len(r.content) > _MAX_BODY_BYTES:
                log.warning("[relativeqs] response too large (%s bytes) — skipping",
                            clen or len(r.content))
                return []
            payload = r.json()
            # Confirmation of a COMPLETE board: a dict whose `movers` is a list. A
            # partial/garbled read fails this and is retried, not accepted as-is.
            if not isinstance(payload, dict) or not isinstance(payload.get("movers"), list):
                raise ValueError("payload missing a movers list (incomplete read)")
            movers = normalize_movers(payload)
            log.info("[relativeqs] board confirmed on attempt %d: %d movers", attempt + 1, len(movers))
            return movers
        except Exception as e:                   # noqa: BLE001 — transient; retry with backoff
            wait = min(2.0 * (attempt + 1), 8.0)
            log.warning("[relativeqs] fetch attempt %d/%d failed (%s): %s — retry in %.0fs",
                        attempt + 1, attempts, url, e, wait if attempt + 1 < attempts else 0)
            if attempt + 1 < attempts:
                await asyncio.sleep(wait)
    log.error("[relativeqs] no complete board after %d attempts — no import this cycle", attempts)
    return []
