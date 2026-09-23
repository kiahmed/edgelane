# Matrix → Postiz: events, read-only API, snapshot rendering

**This is written for the Postiz integration, not a change request against
Matrix's own product.** Nothing here needs to ship before soljet-postiz's own
build starts — it exists so that when someone *does* build it, the engine
side and the Postiz side agree on the contract in advance, the same way
Simmer's `docs/simmer.md` › "Snapshot render endpoint (simmer-snap)" and the
event-publisher section already do for Simmer. Postiz's side of this same
plan (post moments, screenshot design, gating philosophy) is
`soljet-postiz/docs/matrix_integration.md` — read that first for the "why,"
this doc is the "what the engine needs to expose."

Matrix's engine and UI already exist and need no redesign for this
(`strategy_engine.py`, `bias_engine.py`, `evaluator.py`, `dealer_exposures.py`,
`app/routes/accuracy.py`, `market/ui/`). What's missing is purely additive: a
Pub/Sub event publisher, a read-only API, and snapshot-render endpoints —
exactly the three things Simmer's engine had to add on top of its own
already-working readiness logic (see `docs/simmer.md`'s "What EdgeLane must
provide" for that precedent).

## 1. Event publisher — `app/matrix_events.py`

Mirror `app/simmer_events.py` almost exactly: a lazy-singleton
`PublisherClient`, best-effort and **never raises** (posture matches
`emailer.py` — an events outage can never fail or slow anything else), a
deterministic `event_id` so retries/re-publishes dedupe downstream.

**Own topic, not Simmer's** — this was an explicit design decision on the
Postiz side, not an oversight: `facades.matrix-events`, not
`facades.ticker-events`. New config (`config.py`, alongside the existing
`simmer_events_enabled` / `facades_events_topic`):

```python
matrix_events_enabled: bool = Field(default=False)
matrix_events_topic: str = Field(default="facades.matrix-events")
```

Message shape (attributes only, body empty — same convention):
```
product   "matrix"
symbol    e.g. "NVDA"
state     one of the 6 states in §2
expiry    the option expiration (YYYY-MM-DD), or "" when unknown
event_id  MTX-<SYM>-<YYMMDD>-<state>   (deterministic per symbol+UTC day+state)
```

IAM: whichever SA publishes needs `roles/pubsub.publisher` on
`facades.matrix-events` specifically — it does **not** need to be
`facades-poster-sa` (that's the Postiz-side runtime SA); reuse EdgeLane's own
backend SA and add the one binding, same as Simmer's requirement.

## 2. The six states, and where each one's signal already lives

| state | fires when | already-computed source |
|---|---|---|
| `pick_selected` | the engine's top strategy pick changes | `strategy_engine.py::pick_best_candidate()` — fire when its result differs from the last-published pick for that symbol |
| `bias_aligned` / `bias_diverged` | the bias-trust relationship to the engine pick changes | `app/routes/accuracy.py`'s bias-trust `state` field (`in_sync` ↔ `low_conf`/`calibrating`/`paused`) — fire on a transition, not on every poll |
| `win_rate_notable` | a recovery (loss streak → win) or a high-win-frequency stretch — **not a daily post, only when earned** (see §3) | `evaluator.py`'s per-symbol `consec_wins_by_symbol` / `consec_losses_by_symbol` / `regime_alert_active_by_symbol`, and `accuracy.py`'s rolling `win_rate` (already tiered at `pill_green_pct=60.0` / `pill_red_pct=40.0` in `config.py` — reuse those thresholds, don't invent new ones) |
| `session_open` | start of trading day | `dealer_exposures.py`'s `key_levels: {call_wall, put_wall, vex_wall, tex_wall}` — skip silently if nothing about that day's walls is worth a chip |
| `grid_digest` | periodic full-grid share, target ~2×/week | fire only when enough of the 8-strategy grid changed since the last digest — engine's call, not a blind cadence |
| `daily_recap` | best/worst composite-score pick of the day, once/day | the same composite/tag data `pick_best_candidate()` and the grid's status pills (BROKEN/HEALTHY/LIQ HIGH/MARGINAL/etc.) already carry |

The engine-pick chip's existing `_HINT_TEXT` in `accuracy.py`
("Bias re-syncing — wait for a confirming win before sizing up.") is a real
example of what `pick_selected` and `bias_diverged` posts would show — it's
already user-facing copy, not something new to write.

## 3. `win_rate_notable`, concretely

Per the brainstorm this was built from: **no daily obligation, only fire on
something earned.** Two concrete rules, either one sufficient:

1. **Recovery**: `regime_alert_active_by_symbol[sym]` transitions
   `True → False` — i.e. `consec_wins_by_symbol[sym]` just reached
   `regime_clear_consec_wins` (default 2) after a losing streak had tripped
   `regime_alert_consec_losses` (default 3). This is literally "recovered
   from losses to a win."
2. **High-frequency win stretch**: rolling `win_rate` (from `accuracy.py`)
   crosses into the `green` tier (`>= pill_green_pct`, default 60%) from
   below, with `graded >= eval_min_graded` (default 10) so it's not noise on
   a tiny sample.

**Where to compute this: augment the existing `evaluator.py` sweep, don't add
a separate watcher.** That sweep already runs every ~30s, already updates
`consec_wins`/`consec_losses`/`regime_alert_active` per symbol, and already
has the before/after state needed to detect exactly these transitions — it's
the one place that can tell "did this just change" without re-deriving state
from scratch. A standalone watcher would just re-read the same state a moment
later on its own clock: redundant infra, and a second place that can drift out
of sync with the grading logic itself. The one precaution worth taking: make
the `matrix_events.publish_transition(...)` call at the end of that sweep
best-effort and non-blocking — mirror `simmer_events.py`'s own posture (log
and swallow, never raise) — so a transient Pub/Sub hiccup can never stall
grading. Same interval, not a separate loop.

## 4. Read-only API

Same shape as Simmer's (`GET /simmer/ready`, `GET /simmer/state/<SYM>?block=`),
bearer `MATRIX_API_TOKEN` (a new, separate token from `simmer-api-token`):

- `GET /matrix/state/<SYM>?block=pick|grid|bias|win_eval|walls` — the data
  each post moment's copy template needs, split by block the same way
  Simmer's `?block=card|score|gates|sentiment|evolution` is.

## 5. Snapshot render endpoints — the lesson from Simmer's own bug

Simmer's first snap implementation screenshotted the live
`simmer.facades.trade` SPA directly and it silently only ever captured the
sign-in dialog — that page sits behind a user-login session a headless
browser doesn't have. The fix (already shipped, see `docs/simmer.md` ›
"Snapshot render endpoint") was a dedicated, server-rendered, bearer-authed
endpoint that never touches the SPA or a user session. Matrix should launch
with that pattern from day one instead of repeating the bug:

```
GET /matrix/snap/<SYM>?view=engine_pick|strategy_grid|bias_chip|walls_chip|win_eval_grid
Authorization: Bearer MATRIX_API_TOKEN
→ standalone HTML card (inline CSS, no SPA, no user session) —
  app/matrix_snap.py::render_snap_card, mirroring app/simmer_snap.py
→ [data-snap="<view>"] wraps just that one crop
```

`engine_pick` and `strategy_grid` can be built directly from the two UI panes
already shared with Postiz (the "ENGINE PICK" chip and the 8-card "STRATEGIES"
grid); `bias_chip`, `walls_chip`, `win_eval_grid` render data that already
exists (`accuracy.py`, `dealer_exposures.py`, the eval grid) but has no
standalone crop view yet.

## Status — shipped 2026-09-13

All of it is now in the repo; this section is the map, the rest of the doc stays
as the rationale.

| Piece | Where |
|---|---|
| §1 publisher | `app/matrix_events.py` + `matrix_events_*` / `matrix_api_token` in `config.py` |
| §2 six states | `app/matrix_signals.py` — transition detection, one publish per real change |
| §3 evaluator hook | end of `evaluator.py::evaluate_pending` (no separate watcher, as specified) |
| poll-side hook | end of `poller.py::poll_symbol`, gated on `persist` |
| §4 read-only API | `app/routes/matrix.py` → `GET /matrix/state/{SYM}?block=` |
| §5 snap render | `app/matrix_snap.py` + `GET /matrix/snap/{SYM}?view=` |
| provisioning | `ops/matrix/edgelane_provision.sh`, `make matrix-postiz-integrate` |
| tests | `market/backend/tests/matrix/` |

Config to set in `edgelane_market.config` (all dark by default — the publisher
stays off and the API returns 401 until these are filled):

```
MATRIX_EVENTS_ENABLED=true
MATRIX_EVENTS_TOPIC=facades.matrix-events
MATRIX_API_TOKEN=<Secret Manager: matrix-api-token>
GCP_PROJECT=<project>
```

Two notes for the Postiz side:

* **`pick_selected` keys on the structure, not the score.** Strategy + label +
  strikes. Composite score drifts every poll, so including it would make every
  poll look like a new pick.
* **`event_id` can now carry a discriminator** — `MTX-<SYM>-<YYMMDD>-<state>`
  optionally followed by `-<8 hex>`. States that truly happen once a day
  (`session_open`, `daily_recap`, `grid_digest`) keep the bare id. States that
  can legitimately recur carry one: `pick_selected` hashes the pick's legs,
  `win_rate_notable` the milestone reason, `bias_*` the transition. The suffix
  is stable for the same real event, so a retry still dedupes — but the day's
  second genuine pick is no longer swallowed as a duplicate of the first, which
  is what happened before this.
* **A pick must hold `pick_min_dwell_polls` (default 3, ~48s) to count.** The
  engine's top pick flickers — it can change and change back inside a minute.
  The same knob gates the win rate (`db._EPISODE_CTE`) and the `pick_selected`
  chip, so Matrix never posts a pick its own score ignores. Measured on real
  history the win rate is unchanged by it (44–53% W/(W+L) at every threshold
  from 1 to 8 polls); it only removes flickers.
* **`grid_digest` needs both a cooldown and a real change** (~60h, ≥3 of the 8
  cards). Cadence alone posts a grid nobody is looking at; change alone fires
  several times on a choppy session.

## Closed (2026-09-17): `pick_selected` fired on a BROKEN/diverged pick

**Incident:** on 2026-09-17 SPX sat in a losing Bear Put with bias diverged.
The engine kept re-striking new legs on it every poll — legitimately a new
`_pick_key` each time (legs/strikes changed, which is exactly what that key
is supposed to catch) — so `pick_selected` fired repeatedly, each post
carrying `health: "BROKEN"` and the same "edge assumption didn't hold up /
Bias re-syncing" copy, just a different composite score (61.3, then 59.0).
Postiz has a local min-gap backstop now (`MATRIX_MIN_GAP_HOURS_PICK_SELECTED`
in `soljet-postiz/products/facades/matrix_tier.config`), but that's insurance
only — the real fix belongs here.

**Root cause:** `on_snapshot()`'s `# 1. pick_selected` block gates purely on
`_pick_key` changing + `pick_min_dwell_polls`. It never looks at the pick's
own `health` field (`strategy_engine.py` sets `"HEALTHY"` / `"BROKEN"` / `"DO
NOT TRADE"` / etc. — see `routes/matrix.py:104`) or at bias-trust state
(`state.last_trust_state[sym]`, already tracked in this same module, updated
by `on_evaluation()` a few lines below). So a structurally "new" pick posts
regardless of whether it's actually any good.

**Policy decision:** `pick_selected` should not be a raw signal feed — it
exists to show the tool is sharp, not to broadcast every re-strike. Target is
a couple of these a day, not one per poll.

**Fix needed, in `on_snapshot()`'s pick_selected block:**
1. Skip firing (still update `cur_pick_key`/`cur_pick_polls`/`last_pick_key`
   bookkeeping so a later recovery isn't swallowed as "already announced",
   just don't call `_fire`) whenever `pick.get("health")` is `"BROKEN"` or
   `"DO NOT TRADE"`, **or** `state.last_trust_state.get(sym) != "in_sync"`.
2. Prefer not to fire the instant health/bias flips back to good either —
   reuse the earned-recovery pattern `win_rate_notable` already has
   (`recovered` / `crossed_green`, gated on real graded wins, not just a
   state flip) so a pick only gets announced once it's proven itself again,
   not the moment it re-syncs.

**Fixed as specified** (`matrix_signals._pick_block_reason`). A pick is now
announced only when ALL of these hold:

| gate | suppressed when |
|---|---|
| structure | `health` is `broken` / `capital_trap`, or the verdict is `do not trade` |
| bias | `last_trust_state[sym] != "in_sync"` — **unknown counts as not in sync**, so nothing posts before the grader has an opinion |
| recovery | previously suppressed and `_recovery_earned()` is false: the regime pause must be off AND `consec_wins >= 1` — a real graded win, not a flag flip |

Two deliberate choices worth knowing:

* **`last_pick_key` is only set when a chip actually fires.** It means "last
  ANNOUNCED", so a pick held back while broken can still be announced later if
  it recovers, rather than being swallowed as already-said. The run counters
  (`cur_pick_key` / `cur_pick_polls`) do advance while suppressed, as specified.
* **The chip now carries the takeaway** — `win_rate`, `graded` and
  `trust_state` ride along as attributes, so the post can say how the engine has
  been doing rather than being a bare signal with nothing to judge it against.

Every suppression logs its reason (`pick_selected suppressed (health=broken)`),
so the quiet is auditable rather than mysterious. Postiz's
`MATRIX_MIN_GAP_HOURS_PICK_SELECTED` backstop can stay as insurance.

## Closed (2026-09-22): `win_rate_notable` fired on a bad rolling win rate

**Incident:** on 2026-09-22, three `win_rate_notable` posts went out with
headline numbers of 45% (SPX), 15% (NDX), and 10% (NDX) — all over a 20-trade
rolling window. `win_rate_notable` exists to show the tool proving itself
("no daily obligation, only fire on something earned" — §3 above), and a 10%
or 15% win rate is not that; announcing it as "notable" reads as the opposite
of the intended showcase.

**Root cause:** `matrix_signals.py::on_evaluation()`, the `# 6. win_rate_notable`
block (currently ~lines 417–434):

```python
alert = bool(evaluator_state.regime_alert_active_by_symbol.get(sym, False))
prev_alert = state.last_regime_alert.get(sym)
tier = _tier(pct, graded, green, red)
prev_tier = state.last_win_tier.get(sym)

recovered = prev_alert is True and alert is False
crossed_green = (prev_tier is not None and prev_tier != "green"
                 and tier == "green" and graded >= min_graded)
if recovered or crossed_green:
    reason = "recovery" if recovered else "win_streak"
    published.append(_fire(sym, "win_rate_notable", expiry, ...))
```

`recovered` only checks that `regime_alert_active_by_symbol` just flipped
`True → False` — i.e. `consec_wins_by_symbol[sym]` reached
`regime_clear_consec_wins` (default 2) after a losing streak. It never checks
`pct` (the rolling `win_rate` over the `eval_rolling_window`, default 20
trades). So a symbol can go 2-for-20 recently, clear the alert flag purely on
those 2 consecutive wins, and get announced as "notable" while the actual
20-trade win rate is still 10–15%. `crossed_green` doesn't have this problem
(it explicitly requires `tier == "green"`, i.e. `pct >= pill_green_pct`) —
only the `recovered` branch is unguarded on `pct`.

**Fix needed:** add a floor on `pct` to the `recovered` condition — e.g.
`recovered = prev_alert is True and alert is False and pct >= red` (using the
same `pill_red_pct` threshold already in scope, default 40.0), or a fixed bar
like `>= 50.0` if `red` is too low a bar for what counts as "notable." Either
way, "the losing streak just ended" should not by itself be sufficient — the
rolling number the post actually displays needs to clear some real bar too.
Postiz's `compose_matrix()` win_rate_notable template just renders whatever
`win_rate`/`graded` the card carries verbatim, so whatever bar is chosen here
is the only gate — there's no downstream filtering on the Postiz side for this
one (unlike `pick_selected`, which now also has a local min-gap backstop).

**Fixed.** Confirmed from the logs first: all five `win_rate_notable` publishes
on 2026-09-22 were the `recovery` branch (event-id suffix `8b60e9d7` =
`sha1("recovery")`), none were `win_streak`. `recovered` now also requires
`graded >= eval_min_graded` **and** `pct >= 50.0`
(`matrix_signals._NOTABLE_MIN_WIN_PCT`). 50 rather than `pill_red_pct` (40):
the post *headlines* this number, and a 41% "notable" is still not a showcase.
Because `pct` is wins/n with neutrals in the denominator, 50% means wins at
least match losses-plus-pushes. A lifted-but-unqualified recovery logs
`win_rate_notable suppressed (recovery at 15% over 20 graded — below the 50% bar)`.
`crossed_green` was already correct and is unchanged.

## New state (2026-09-22): `pick_result` — closing the loop

The feed announced picks but never said how they turned out, which is the one
thing that actually shows the tool's performance. `pick_result` reports the
outcome of **each pick that `pick_selected` announced**, once its run ends.

| | |
|---|---|
| fires when | the announced pick's run closes (engine moves to a different pick) and its final poll is graded (grace: 15 min, then the last grade it did get) |
| grade | the run's **last** grade before the engine moved on — identical to how the win rate grades an episode (`db._EPISODE_CTE`), so the two never disagree |
| posts | `win` and `loss`. **Losses are posted on purpose** — a feed that only reports its wins is marketing. `neutral` is skipped: "didn't clear the bid/ask noise" has no takeaway |
| event_id | `MTX-<SYM>-<YYMMDD>-pick_result-<8 hex>`, where the suffix is the **same pick hash as its `pick_selected`** — the poster can thread the result under the original post |
| attributes | everything `pick_selected` carried, plus `result`, `entry_premium`, `exit_premium`, `favorable_delta`, `held_minutes`, `announced_at` |
| gives up | if the run never closes within 8h (logged) |

Only announced picks get a result — so it inherits every `pick_selected` gate
(health, bias in sync, earned recovery, dwell). Nothing that wasn't posted is
ever reported on.

**Postiz side needs:** a `pick_result` template (it's a 7th state, beyond the
§2 table), and there's no dedicated snap crop yet — the attributes carry the
full story, but `engine_pick` shows the *current* pick, not the graded one.

Known limitation: pending results live in memory, like `daily_recap`'s tally —
a backend restart between announcement and outcome drops that result.

## Summary for whoever picks this up

Net new in this repo: `app/matrix_events.py`, the `matrix_events_*` config
keys, a hook in `evaluator.py`'s existing sweep, `app/matrix_snap.py`,
`/matrix/state/*` + `/matrix/snap/*` routes, and the `matrix-api-token`
equivalent of `simmer-api-token`. Nothing about Simmer's own topic, publisher,
or endpoints changes.
