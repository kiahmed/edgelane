# Simmer → Postiz: persist events to a ledger, stop calling back

**Scope: the event persist + posting ARCHITECTURE only.** Simmer keeps every one
of its own posting rules exactly as they are — which states post
(`watch_entered`, `ready`), the readiness bands, the earnings/off-hours-catalyst
allowance, the poster's min-gaps and `POST_ON_STATES`, email fan-out. Nothing
here changes *what* Simmer decides to post; it changes *how an event travels*
from the engine to the poster.

Matrix made this exact move on 2026-09-29 and it is live — see
`docs/matrix_events_update.md` › "The ledger — engine records, poster reads".
This doc is the Simmer version of that change, adjusted for the one thing
Simmer has that Matrix doesn't (an exactly-once publish guarantee, §3).

Ownership: **simmer-development** owns every file named below, in both repos.

---

## 1. Why

Today a Simmer event is a Pub/Sub message with attributes only. To build the
post, the GCP side calls **back** into EdgeLane over the tunnel:

| step | today |
|---|---|
| numbers + wording | `simmer-poster` → `GET /simmer/state/<SYM>?block=card\|score\|gates\|sentiment\|evolution` |
| image | `simmer-snap` → loads `GET /simmer/snap/<SYM>` in Chromium and screenshots it |
| dedupe | EdgeLane DuckDB `simmer_published_events` (exactly-once topic publish) |

Two problems:

1. **Stale posts.** Both calls render from the readiness cache *at fetch time*,
   not at event time. A post can describe a later state than the transition
   that triggered it.
2. **Coupling.** The engine has to serve the poster over the tunnel on every
   post; the poster can't work without EdgeLane being reachable at that moment.

## 2. Target flow

```
engine detects a postable transition
  └─ freezes: attributes + state blocks + card HTML   (at THIS moment)
  └─ INSERT into Supabase simmer_event_ledger          (unique event_id)
       └─ only if it was a NEW row → publish Pub/Sub   (attributes + ledger=1, event_at)
                                          │
simmer-poster (Cloud Run, push) ◄─────────┘
  └─ claim row (atomic) → compose from row.data
  └─ simmer-snap renders row.snap_html  (set_content, network blocked)
  └─ post → mark row done → prune rows older than 1 day
```

The engine **only detects and records**. It no longer serves the poster.
Nothing in the normal path calls `/simmer/state` or `/simmer/snap`.

## 3. The one Simmer-specific difference: exactly-once publish

`simmer_published_events` exists so a transition re-seen across sweeps (or
watched by many users) is published to the topic **once** per
`(symbol, expiry, state, UTC day)`. The ledger must keep that guarantee — the
`event_id` (`SMR-<SYM>-<YYMMDD>-<expiryYYMMDD>-<state>`) is already the key.

Two consequences for the design, both different from Matrix:

- **Publish only on a NEW row.** Insert with
  `Prefer: return=representation,resolution=ignore-duplicates` and
  `on_conflict=event_id`. PostgREST returns `[row]` when it inserted and `[]`
  when the id already existed. Publish on `[row]` only. (Matrix's helper,
  `supabase_admin.insert_row_ignore_duplicates`, returns `True` for both —
  Matrix re-publishes on purpose and dedupes downstream. Simmer needs the
  distinction, so use or add a variant that reports it.)
- **"Done" keeps a tombstone, not nothing.** If the poster *deleted* the row
  after posting, a same-day re-seen transition would insert a fresh row and
  publish again — losing exactly-once. So `done` **clears the payload**
  (`data`, `snap_html`, `attributes` → empty) and sets `status = 'posted'`,
  keeping the `event_id` row as the dedupe key. `prune` then removes rows older
  than **1 day**, which outlives the UTC-day scope of the id.

  This still honours "get rid of the data after posting": the posted row
  carries no payload, only the key that stops a duplicate.

## 4. Schema — `public.simmer_event_ledger` (new migration)

Own table, own functions, own token — **not** Matrix's. Same separation as the
topics (`facades.ticker-events` vs `facades.matrix-events`).

Use the next free migration number. **Check `ls supabase/migrations/` first:**
parallel sessions have collided on numbers before (Matrix's ledger was
renumbered to 0017/0018 after Torque took 0014–0016).

| column | type | notes |
|---|---|---|
| `id` | uuid pk | `gen_random_uuid()` |
| `event_id` | text **unique** | `SMR-…` — the exactly-once key |
| `product` | text | `'simmer'` |
| `symbol`, `state`, `expiry` | text | `state` ∈ `watch_entered`, `ready` |
| `event_at` | timestamptz | when the ENGINE detected it |
| `attributes` | jsonb | every Pub/Sub attribute, incl. `off_hours_catalyst` |
| `data` | jsonb | the `card` / `score` / `gates` / `sentiment` / `evolution` blocks, frozen |
| `snap_html` | text | the card, standalone HTML, frozen |
| `status` | text | `pending` → `claimed` → `posted` |
| `claimed_at`, `claimed_by`, `posted_at` | | |
| `created_at` | timestamptz | `now()` |

RLS **on, no policies** — browser and signed-in users can't read it. The
backend writes with `service_role`.

`SECURITY DEFINER` functions, each gated by a ledger token (store only its
sha256, in a single-row table with RLS and no policies), `grant execute` to
`anon, authenticated`, helper not callable:

| function | does |
|---|---|
| `simmer_ledger_peek(token, event_id)` | read, don't consume — for dry-runs |
| `simmer_ledger_claim(token, event_id, worker)` | atomic `pending → claimed`, returns the row; a claim older than 15 min is re-claimable |
| `simmer_ledger_done(token, event_id)` | clear payload, `status = 'posted'` (the §3 tombstone) |
| `simmer_ledger_prune(token, before)` | delete rows with `event_at < before`, never a claim held in the last 15 min |

Copy `supabase/migrations/0017_matrix_event_ledger.sql` + `0018_matrix_ledger_peek.sql`
as the template; change the table/function names, the `done` semantics (§3),
and add `posted_at`.

**Why not the service key on the poster:** it can decrypt users' broker tokens
(`get_broker_secret`). The poster gets a ledger-only token that can touch this
one table and nothing else. Secret Manager name: `simmer-ledger-token`.

## 5. EdgeLane changes (engine)

At both publish call sites in `app/simmer_watcher.py` (the in-hours transition
loop, ~L1424, and the off-hours-catalyst path, ~L1488) — the readiness envelope
`env` is already in hand there, which is exactly what needs freezing:

1. **Freeze synchronously**: deep-copy `env` before anything else can mutate it.
2. **Build the row** off the event loop (`asyncio.to_thread`):
   - `data` = each block from `routes/simmer.py::_state_block(env, block, db)`
     (`evolution` reads readiness history — freeze that too).
   - `snap_html` = `simmer_snap.render_snap_card(env, ready=…, watch=…)`, the
     same function `/simmer/snap` serves today.
   - `attributes` = the existing publish attributes, plus the `takeaways`.
3. **Insert**; publish **only if the row is new** (§3), adding `ledger=1` and
   `event_at` to the attributes. No row → no message.
4. Stays best-effort and never raises, like today — and must not block the
   sweep (Matrix learned this: a slow Supabase/Pub/Sub call inline on the poll
   path stretches the whole cycle; hand it to a background task).

Suggested home: a small `app/simmer_ledger.py`, mirroring `app/matrix_ledger.py`.

## 6. soljet-postiz changes (poster + snap)

Mirror what Matrix did (`src/lib/sources/matrix_ledger.py`,
`bin/matrix_poster.py`, `ops/matrix/snap/main.py`):

- **Ledger client** — `peek` / `claim` / `done` / `prune` over the four RPCs.
  Env: `SUPABASE_URL`, `SUPABASE_ANON_KEY` (public/publishable — the token is
  what authorizes), `SIMMER_LEDGER_TOKEN` (secret).
- **Poster** — on an event with `ledger=1`: `claim` (or `peek` when dry-run —
  a claim would block the real run), build the card from `row.data` (the
  blocks are the exact shapes `/simmer/state` returned, so the existing
  `simmer_source` card mapping can be reused), `done` after a successful post,
  and `prune` rows older than **1 day** on every run. Events without `ledger=1`
  (published before the cutover) may keep using the legacy callback path.
- **Snap** — accept `{"html": …}` and render it with `page.set_content()`,
  aborting every network request (the card is self-contained). Keep the
  URL path for legacy.
- **Deploy** — add the two env vars and `SIMMER_LEDGER_TOKEN=simmer-ledger-token:latest`;
  grant `facades-poster-sa` `secretAccessor` on `simmer-ledger-token`.

The poster's **existing Simmer rules stay untouched** — `POST_ON_STATES`,
min-gaps, the Firestore dedupe, the `off_hours_catalyst` honouring (read it from
`row.attributes`).

## 7. Past-tense wording

The ledger makes an event's age explicit (`event_at`). When the poster composes
a post about an event that is no longer "now" — a delayed or redelivered
message — the wording must say so ("entered watch" / "was ready" rather than
"is ready"), and live advice should be dropped. Matrix uses a 10-minute
threshold; pick what suits Simmer's cadence.

## 8. Retiring the DuckDB table

After the ledger is live and verified (§9), in EdgeLane:

- Remove `simmer_published_events` usage: `db.insert_simmer_published_event`,
  `db.simmer_published_event_exists`, the calls in `simmer_events.py`, and the
  dedupe check in `tools/simmer_fire_event.py` (point it at the ledger).
- Drop the table: add `DROP TABLE IF EXISTS simmer_published_events;` to the
  DuckDB schema in `app/db.py` (it runs on every boot, so it's idempotent), and
  remove its `CREATE`.
- Move `publish_transition`'s `db=`/`force=` dedupe parameters onto the ledger
  (`force` = skip the new-row check, for the admin fire tool).

Do this **after** cutover, not with it, so there is a rollback path.

## 9. Testing — lessons from the Matrix build

- **Isolate tests from real services.** `get_settings()` reads the real
  `edgelane_market.config`, which has events enabled and the Supabase service
  key. During the Matrix build the test suite wrote 14 fixture rows into the
  **live** ledger before anyone noticed. Add a `conftest.py` that starts from
  default `Settings()` and stubs the lowest real layers (the Pub/Sub client and
  the Supabase HTTP write) to **fail the test** if reached — see
  `market/backend/tests/matrix/conftest.py`.
- **End-to-end without posting.** Test the *deployed* poster on a Cloud Run
  revision with `--no-traffic --tag=…`, `SIMMER_POSTER_DRY_RUN=true`, and (if it
  has a market gate) the market gate off via an env override — then POST a
  Pub/Sub push envelope to the tag URL with an identity token.
- **⚠ Cloud Run traffic pinning.** Creating that `--no-traffic` revision pins
  traffic, and afterwards "latest ready" can point at the **dry-run** revision.
  Do NOT `--to-latest` blindly: route traffic explicitly to the clean revision
  first, confirm its env (no dry-run), *then* switch back to follow-latest.
  Matrix nearly shipped a silently-dry-run poster this way.
- **Prove the decoupling.** During the test, point the poster's legacy API base
  at an unreachable host and confirm EdgeLane's access log shows zero
  `/simmer/state` or `/simmer/snap` requests.

## 10. Checklist

- [ ] migration: `simmer_event_ledger` + 4 functions + token table (next free number)
- [ ] apply it; create `simmer-ledger-token` in Secret Manager; store its sha256
- [ ] engine: freeze → row → insert → publish-if-new, at both call sites
- [ ] tests: isolation conftest; freeze/new-row-only/no-row-no-message cases
- [ ] poster: ledger client, claim/peek, compose from row, done (tombstone), 1-day prune
- [ ] snap: accept `html`
- [ ] deploy: env + secret + IAM; no-traffic dry-run test; restore traffic safely
- [ ] verify: real row → deployed poster dry-run from ledger → zero callbacks
- [ ] **then** retire `simmer_published_events` (§8)
