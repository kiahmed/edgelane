# Simmer → Postiz: the off-hours catalyst exception

> **Status (shipped 2026-09-15):** the EdgeLane side is built —
> `simmer_watcher.process_off_hours_catalyst_events` publishes a ready/watch
> event with `off_hours_catalyst="true"` when the market is closed and the name
> has a live catalyst (`_off_hours_catalyst`, read from the engine's earnings
> block). No email/alert-state, no event-band mutation; the closed-hours freeze
> is untouched. See §"What this repo would need" for the design.

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

## Related: the overnight-movers importer + premium analyzer (planned)

A separate, larger pre-open feature shares this document's two core ideas — the
**market-closed / last-available-chain** posture and the **catalyst drives it,
never promotes it** rule — but it is an IMPORT + ANALYSIS feature, not a posting
one, so the off-hours *posting* contract above is unchanged.

The full implementation contract is in **`docs/simmer.md` → "Overnight movers —
RelativeQs import + premium-decision analyzer"**. In brief:

- At **09:25 ET** (pre-open, market closed) Simmer imports the overnight's
  biggest movers from the sibling **RelativeQs** app (`GET /api/premarket`,
  |gap| ≥ 2%, Simmer-eligible) into each opted-in user's watchlist for the next
  expiry, badged **(RQ)**, and researches a one-line thesis for why each moved
  (Gemini + grounded web / our news). A duplicate (user, symbol, expiry) is
  **overridden** with the fresh overnight overlay, not skipped.
- Once open, a standalone **premium-decision analyzer**
  (`app/simmer_premium_tracker.py`) compares live option premiums on strikes
  toward the move against the **frozen 9:25 baseline** (last-night close) and
  collapses it into ONE **decision per name — sell bear-call / sell bull-put /
  wait / skip** — shown decision-first in a new "Overnight" card tab. Like the
  off-hours flag here, it **never auto-promotes** a credit sell; it only flags a
  side the deterministic engine has already built and gated.

If an off-hours *post* is ever wanted for an RQ overnight name, it flows through
the exact `off_hours_catalyst="true"` attribute described above — no new posting
path.

## Summary

Net new here, if ever built: one attribute on the existing publisher, gated
by the existing catalyst detector against the last chain read. Postiz's side
is already live and waiting for it — no coordination needed beyond setting
that one attribute correctly.
