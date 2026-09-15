# Simmer → Postiz: the off-hours catalyst exception

**Written for the Postiz integration, not a change request against Simmer's
own product** — same posture as `docs/matrix_events_update.md`. Nothing here
needs to ship immediately; it documents the flag Postiz's poster now honors,
so whenever this is built the two sides agree on the contract.

## What changed on the Postiz side

`bin/simmer_poster.py` now gates every event on US market hours (9:30–16:00
America/New_York, Mon–Fri, via `src/lib/market_hours.py` — the same window
`poller.py::_is_market_open()` already computes here, no holiday calendar
either side). A `ready`/`watch_entered` event that arrives while the market
is closed is **skipped by default** — a credit-spread call read against a
stale chain isn't something to publish as current.

**The one exception**: if the Pub/Sub message carries the attribute
`off_hours_catalyst=true`, the poster still posts — but
`compose_simmer()` appends a fixed disclaimer line:

> Alert generated while markets were closed, based on a catalyst and the
> last available options chain — check back at the next market open.

The poster **never decides this itself** — it has no news feed to judge "is
this a real catalyst" from. It only ever honors a flag EdgeLane's own engine
sets. No flag on an off-hours event → silently skipped, same as it would be
with no exception at all. This is intentionally identical in spirit to how
`matrix_events_update.md` §3 describes `win_rate_notable`: the engine is the
only thing that can make this call.

## What this repo would need, if built

Nothing here is required — Simmer already works within market hours with no
changes. This is only relevant if a real off-hours catalyst alert is wanted.

1. **The judgment call**: the existing catalyst-detection pipeline
   (`docs/simmer.md` › "Catalyst detection — the hard gate", SEC EDGAR +
   news) currently only **vetoes** a `ready` call. Firing an off-hours alert
   would be the same detector used the other direction: a catalyst strong
   enough to matter, checked against the **last available options chain**
   (the most recent snapshot before close — not live, and the poller already
   knows this: `poller.py::poll_loop`'s `market_open`/`state.market_reason`
   are exactly this signal).
2. **The publish-side change**: in `app/simmer_events.py`'s
   `publish_transition(...)` (or wherever the `ready`/`watch_entered` event
   gets built), when `not _is_market_open(...)` and the catalyst check above
   passes, add one more Pub/Sub attribute:
   ```
   off_hours_catalyst   "true"
   ```
   Omit it (or set anything else) for a routine off-hours event — the
   default is silence, not a post.

## Summary

Net new here, if ever built: one attribute on the existing publisher, gated
by the existing catalyst detector against the last chain read. Postiz's side
is already live and waiting for it — no coordination needed beyond setting
that one attribute correctly.
