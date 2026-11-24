# Level Engine — spec (v1)

Status: **spec — not built.** Code will live in `market/backend/level_engine/`
(its own container); this document is the design it must follow.

## 0. Why

Matrix's engine is **reactive**: every poll it re-derives a bias from the GEX
walls, net DEX, the GEX profile and call/put premium flow, picks a strategy
family from that bias, sets strikes by delta / expected move, and ranks the
candidates by `composite_score`. It has no notion of *"if price holds X it goes
to Y"* — it only changes its mind after walls, flow and premiums have already
moved.

The operator draws a set of **levels** for SPX/SPY each day (1 ATR, Golden Gate,
long / short trigger, prior-day high/low, pre-market high/low, monthly pivot,
10-min 21/200 EMA, …). Observed behaviour: when price **reclaims and holds** a
level it tends to travel to the **next** level; when it **fails** a level it
falls back to the **previous** one. The Level Engine turns that into a
deterministic, watchful decision engine that knows the map ahead of time.

## 1. Principles (non-negotiable)

1. **Deterministic.** Every decision is plain code with configured thresholds;
   same inputs → same output. No LLM in any decision path (project rule: LLMs are
   prose-only). An LLM may later (a) narrate the manager's trace, (b) translate a
   level's `condition_text` into a structured rule **once, at entry, human-approved**.
2. **Side pipeline.** Today's engine, its tiles, its evaluator, its parity tests
   and its Pub/Sub signals keep working unchanged. The Level Engine plugs in at the
   start (it consumes the backend's data) and at the end (it supplies a pick).
3. **One data puller.** Only the existing backend calls Tradier. The Level Engine
   reads the backend's cached data; it never calls Tradier itself.
4. **No UI rearrangement.** The Matrix page keeps its tiles and layout; the level
   pick fills the existing engine-pick tile when switched on.
5. **Replayable.** Every tick and every decision is persisted, so any past day can
   be re-run offline to tune rules without waiting for the market.
6. **Scope v1:** SPX 0DTE, levels entered for SPY and/or SPX, regular session
   09:30–16:00 ET. NDX / QQQ / IWM are *not* modelled — the market moves with SPX;
   mirroring onto other indices is a later, separate feature.

## 2. What exists today (reused, not rewritten)

| Need | Existing code | Use |
|---|---|---|
| Spot + 0DTE chain every `POLL_INTERVAL_SEC` (20 s) for `SYMBOLS=SPX,NDX` | `poller.poll_symbol` → `state.latest_by_symbol[sym]` (engine output incl. `bias`, `engine_pick`, `strategies`, `expected_move`, `_quote_by_symbol`) | feed source (§3.2) |
| Normalised contracts (strike, side, bid/ask/mid, greeks, OI, volume) | `poller._normalize_tradier_contract` (local `contracts` list in `poll_symbol`) | **cache it** in `PollerState.latest_contracts_by_symbol` (one line) |
| GEX/DEX/VEX/TEX by strike, walls | `dealer_exposures`, `walls.find_gex_walls_bilateral`, `bias` dict | Derivatives agent evidence |
| Call/put premium flow | `bias_engine.compute_flow_direction_score` (EdgelaneProvider payload `call_premium`/`put_premium`) | Derivatives agent evidence |
| Distance / pull to a price | `bias_engine._reach`, magnet scores | Up/Down agents ("pull toward level") |
| Premium build vs a frozen baseline | `simmer_premium_tracker` (baseline snapshot → live comparison → one verdict) | pattern for the premium-build signal (§8.2) |
| Score a structure from explicit legs | `strategy_engine.score_vertical(short, long, type_, dte)`, `score_condor(...)`, `score_butterfly(...)`, `compute_limit_premiums`, `composite_score`, `composite_verdict` | Strategy agent — **called with level-anchored contracts**, unchanged |
| Pick object the UI renders | `engine_pick` fields: strategy, name, short, spread_type, label, composite_score, composite_verdict, structure_text, net_premium, max_profit, max_loss, pop_pct, ev, ev_adjusted, health, liquidity, strikes, legs, predicted_direction | level pick uses the **same shape** (§11) |
| Projection "if spot = X at time T" | Matrix page `_buildProjectionGrid` (Black-Scholes, `market/ui/index.html`) | **port to Python** for the Risk/Reward agent (the one real port) |
| Transition-once publishing | `matrix_signals` pattern | level events (later) |
| Grading infra | `evaluator.py` (reprice legs from `_quote_by_symbol`, `EVAL_WINDOW_MIN=3`, `EVAL_ROLLING_WINDOW=20`) | untouched; level grading is separate and **event-based** (§12) |

## 3. Architecture

```
Tradier ── backend poller (only caller) ──► PollerState (spot, 0DTE chain, engine output, SPY quote)
                                │
                     GET /internal/feed/SPX  (docker network only)
                                │
                     level-engine container ── tick loop (every poll) ──► agents ──► manager
                                │                                                   │
                         Supabase (levels, ticks, evidence, beliefs, legs, day summary)
                                │                                                   │
              backend /levels/* proxy ◄──── level-engine read API (latest decision, day, history)
                                │
              Matrix page (engine-pick tile, MATRIX_DECISION_SOURCE)      Torque (later: exits, reach)
```

### 3.1 Container and listeners

New compose service `facades-levels-engine` (same image build context, own
entrypoint). It runs **two separate listeners** (two ASGI apps), so a route can
never be reached through the wrong door:

| Listener | Container bind | Published | Serves |
|---|---|---|---|
| **Read API** | `0.0.0.0:8791` | **not published** — compose network only | `GET /levels/decision/{symbol}`, `GET /levels/day/{date}`, `GET /levels/trace/{symbol}`, `GET /levels/stats/{symbol}` (recent closed legs + hit rates, for the outcomes tile) — read-only |
| **Admin** | `0.0.0.0:8790` | `127.0.0.1:8790:8790` (host loopback only) | the admin page (§10) and every write (levels add / edit / delete / import) |

Own config `facades_levels_engine.config` (§4). Database access through a least-privilege
role (§5.1), never the service_role key.

### 3.2 Data feed (no duplicate API calls)

- The backend's port 8789 is what the Cloudflare tunnel serves publicly and is
  also published on the host, so the feed route **is internet-reachable** and must
  be locked down in code:
  - `GET /internal/feed/{symbol}` requires header `X-Internal-Token`; compared with
    `hmac.compare_digest`. **Fails closed:** when `INTERNAL_FEED_TOKEN` is unset on
    the backend, the route returns 404 for every request.
  - Requests that arrived through the tunnel (`Cf-Connecting-Ip` or `Cf-Ray`
    header present) are refused (404) even with a valid token.
  - Returns only market data (no account, user or broker data).
- Payload: `ts`, `spot`, `expiration`, `dte`, `expected_move`, `contracts`
  (normalised 0DTE chain), `bias` (as computed), `engine_pick` (legacy), `spy_spot`.
- `PollerState` gains `latest_contracts_by_symbol` and `latest_spy_spot`, set in
  `poll_symbol`. SPY rides the **existing quote call** (Tradier batches symbols:
  `SPX,SPY`) — no extra request — only when `LEVEL_FEED_ENABLED=true`.
- The level engine reads the feed every `TICK_SEC` (default 20 s = the poll) and
  ignores a feed whose `ts` hasn't advanced (no double ticks).

### 3.3 Outputs

- All state is written to the `lvl_*` tables (§5).
- The read API (§3.1) serves the latest pick, a day, and the trace.
- The backend exposes `/levels/*` to Matrix as an **allow-list proxy**: exactly
  the four GETs above, forwarded to the read listener only (never the admin
  port), behind the **same auth / entitlement as `/snapshot`**. Anything else under
  `/levels/` is 404. No Cloudflare change.

### 3.4 Matrix switch (revised 2026-11-23 — "levels" means everything follows levels)

- `MATRIX_DECISION_SOURCE=engine|levels` (backend config, default `engine`).
  `engine` = today, byte-identical. `levels` = **every decision Matrix shows or
  emits comes from the level engine**; the legacy engine is kept only as a **data
  service** (GEX/DEX/walls/flow — the bias computation the level engine already
  consumes through the feed). Legacy code stays in place, inactive; it is retired
  after a few good runs.
- With `levels`:
  - **Symbols:** only SPX is polled (NDX and any other `SYMBOLS` entry are
    dropped while `levels` is on; `engine` keeps the configured list).
  - **Engine-pick tile:** the level pick. **Strategy grid:** the level candidates
    (§8.4 set) in the grid's existing shape (`strategies[strategy].best`, one card
    per strategy; the `extreme` sibling in `all_widths`). Wait / done → empty grid.
  - **Health slot** on the cards shows the R/R-based health (§8.4).
  - **Outcomes / self-eval tile, win rate, trust state** (`/accuracy/SPX`, same
    contract): from level legs (§12) via the read API's stats route.
  - **Signals / posts (`matrix_signals`), Matrix ledger, snap cards
    (`/matrix/state`, `/matrix/snap`), daily email:** follow the level pick and
    level grading (`pick_result` keyed by `pick_id`).
  - **Legacy evaluator does not grade** (no silent shadow grading); the poller
    stops capturing a gradable legacy pick. Its archive / daily-email loop keeps
    running, fed by level grading.
  - **Bias, walls, spot tiles** render as today (they are the data service).
  - The poller's cached snapshot is never mutated (the internal feed keeps
    sending the legacy output to the level engine); each consumer gets a
    level-view copy.
- **Order dialog:** places **the pick / card that is shown**, resolved from the
  level engine by `pick_id` (tile and grid); never falls back to a legacy candidate.
- No layout change.

## 4. Config — `facades_levels_engine.config` (KEY=VALUE, gitignored, `.example` committed)

| Key | Default | Meaning |
|---|---|---|
| `BACKEND_FEED_URL` | `http://edgelane-backend:8789/internal/feed` | data feed |
| `INTERNAL_FEED_TOKEN` | — | shared secret with the backend's feed route (required; empty = feed refused) |
| `SYMBOL` | `SPX` | v1 symbol |
| `TICK_SEC` | `20` | loop cadence |
| `SESSION` | `09:30-16:00` | ET, Tradier calendar respected (holidays / early close) |
| `RATIO_LOCK_TIME` | `09:35` | SPX/SPY ratio locked at this time (§6) |
| `BUFFER_PTS` | `2.5` | hysteresis band half-width around a level (SPX points) |
| `TEST_PTS` | `6.0` | approach distance: outside the band but within this = *testing* (must be > `BUFFER_PTS`) |
| `HOLD_MIN` | `10` | minutes beyond the band to count as *held* |
| `GRACE_SEC` | `60` | a re-entry into the band shorter than this doesn't reset the hold timer |
| `KNIFE_PTS` | `5.0` | one-tick move through a level larger than this = knife (must be ≥ 2×`BUFFER_PTS`) |
| `CLUSTER_PTS` | `5.0` | levels closer than this are merged into one cluster (≥ 2×`BUFFER_PTS`) |
| `CONTEST_CROSSES` | `3` | crosses within `CONTEST_WINDOW_MIN` → *contested* |
| `CONTEST_WINDOW_MIN` | `30` | |
| `DETAIL_WINDOW_MIN` | `30` | agents' zoom window (memory is the whole day) |
| `EVIDENCE_HALF_LIFE_MIN` | `15` | decay of evidence weight |
| `EVIDENCE_SCALE` | `1.0` | log-odds added per unit of evidence weight |
| `LEAN_LOGODDS` | `0.5` | prior log-odds the §7.2 lean adds to its scenario |
| `THIN_FLOOR` | `0.15` | Σ|weight| below this in a tick = thin evidence (re-pass) |
| `DECIDE_MARGIN` | `0.20` | probability lead needed to call a scenario |
| `FLIP_MARGIN` | `0.30` | probability lead needed to *change* the call |
| `MIN_DWELL_MIN` | `5` | minimum time between flips |
| `MAX_REPASS` | `2` | manager follow-up passes per tick |
| `REACH` | `both` | risk/reward computes `near` and `extreme` |
| `HIT_TOL_PTS` | `2.0` | grading (§12): a directional leg wins when spot comes within this many SPX points of its target before the leg ends |
| `LVL_DB_URL` | — | Postgres DSN of the `facades_levels_engine_rw` role (§5.1) — **not** the service_role key |
| `LVL_REPLAY_DB_URL` | — | DSN of the `facades_levels_engine_replay` role (§14) |
| `READ_PORT` / `ADMIN_PORT` | `8791` / `8790` | listeners (§3.1) |

## 5. Data model (Supabase, new tables, prefix `lvl_`)

| Table | Key columns | Notes |
|---|---|---|
| `lvl_levels` | `id`, `trade_date`, `symbol` (SPY\|SPX), `price` (nullable for SPX), `name`, `kind` (optional: support/resistance/pivot/trigger), `condition_text` (free text, unused v1), `condition_rule` (jsonb, null v1), `source` (`entered`\|`converted`), `created_at`, `updated_at` | editable only while `trade_date = today` (enforced server-side) |
| `lvl_level_audit` | `level_id`, `action`, `before`, `after`, `at` | every add / edit / delete |
| `lvl_ticks` | `trade_date`, `ts`, `spx`, `spy`, `ratio`, `zone`, `chain` (jsonb: every 0DTE contract with strike inside [lowest level − 2 EM, highest level + 2 EM] — strike, side, bid, ask, mid, last, delta, gamma, theta, vega, iv, OI, volume), `bias` (jsonb, the feed's) | ~1,200 rows/day; the replay source — enough for the strategy and R/R agents to be reproduced exactly |
| `lvl_level_state` | `trade_date`, `level_id`, `ts`, `state`, `since`, `crosses`, `last_held_side` | per-level state machine (§7), one row per transition |
| `lvl_evidence` | `trade_date`, `ts`, `agent`, `scenario` (up\|down\|range), `weight` (−1…+1), `kind`, `detail` (jsonb), `reason` (text) | the shared board |
| `lvl_beliefs` | `trade_date`, `ts`, `p_up`, `p_down`, `p_range`, `call`, `target_level_id`, `invalidation_level_id`, `confidence`, `trace` (text) | one row per tick |
| `lvl_picks` | `trade_date`, `ts`, `pick` (jsonb, `engine_pick` shape + level fields) | on change only |
| `lvl_legs` | `id`, `trade_date`, `opened_at`, `call`, `target_level_id`, `invalidation_level_id`, `entry_spot`, `exit_spot`, `target_level` / `invalidation_level` (jsonb {price,name}), `pick` (jsonb), `closed_at`, `result` (win\|loss — binary, §12; old flipped\|unresolved rows tolerated), `close_reason` (target\|invalidation\|flipped\|session_close\|zone_held\|zone_exit), `progress_pct`, `mae_pts`, `time_to_target_min`, `strategy_pnl` | grading (§12) |
| `lvl_day_summary` | `trade_date`, `close`, `final_zone`, `levels_outcome` (jsonb: per level held/failed/broken/contested/untested), `legs` (counts), `notes` | end of day (§13), read-only after close |

### 5.1 Database roles (least privilege)

- `facades_levels_engine_rw`: LOGIN, no superuser / bypassrls; `select, insert, update,
  delete` on `lvl_*` only (and `usage` on the schema). Its DSN is the only
  credential the container gets. It has no access to any other table — notably
  not broker or user data. (Same pattern as the poster `poster_dedupe_rw` role.)
- `facades_levels_engine_replay`: `select` on the live `lvl_*` tables, all privileges on
  schema `lvl_replay` only. It **cannot write live tables**, by grant (§14).
- RLS on every `lvl_*` table with policies for these roles only; no anon /
  authenticated grants (Matrix reads via the backend proxy, not PostgREST).
- The admin page and past-date read-only rule are enforced in the level engine
  **and** by a trigger refusing writes to `lvl_levels` where `trade_date < current_date`
  (ET).

## 6. Levels: entry and SPY→SPX conversion

- The operator enters levels for **SPY and/or SPX**. SPX levels entered directly
  (e.g. the day's highest / lowest boundaries) are used as-is.
- Missing SPX levels are **converted**: `spx_level = spy_level × ratio`.
  - Before 09:35: `ratio = prior close SPX / prior close SPY` (provisional).
  - At `RATIO_LOCK_TIME` (09:35): `ratio = live SPX / live SPY`, **locked for the
    day** so level prices — and the strikes built on them — never drift.
  - Converted rows are stored with `source=converted` and the ratio used.
- If an SPY level and an entered SPX level convert to within `BUFFER_PTS` of each
  other, they are merged (the entered SPX price wins; both names kept).
- Strikes: each level maps to the **nearest listed SPX strike** (5-pt grid).

## 7. The level state machine (deterministic core)

### 7.0 Clusters

Levels (after SPY→SPX conversion) closer than `CLUSTER_PTS` are merged into one
**cluster**: price = mean, names joined, strongest `kind` kept. The state machine
runs on clusters, so bands never overlap. Below, "level" means cluster.

### 7.1 Per-level state

For level `L`, each tick computes the signed distance `d = spot − L` and
`side(d) = +1 if d > BUFFER_PTS, −1 if d < −BUFFER_PTS, 0 otherwise` (inside the
band; `spot == L` is `d = 0`, side 0).

States (directional ones carry `up` = crossed from below, `down` = from above):

| State | Meaning |
|---|---|
| `away_above` / `away_below` | `|d| > TEST_PTS`, no active event |
| `testing` | `BUFFER_PTS < |d| ≤ TEST_PTS` or side 0, coming from `away_*`, no active event |
| `crossed_up` / `crossed_down` | side flipped to ±1 from the other side (or from the band, entered from the other side); hold timer running |
| `held_up` / `held_down` | side stayed ±1 for `HOLD_MIN` (timer, see below) |
| `failed_up` / `failed_down` | from `crossed_*` before `held_*`: side went back to the origin side (−1 for `up`) |
| `broken_up` / `broken_down` | a held level lost on the other side (rule 6), or a knife (rule 2) |
| `contested` | `CONTEST_CROSSES` crosses within `CONTEST_WINDOW_MIN` |

Transition rules, applied **in this order** each tick (first match wins):

1. **Contested.** A *cross* is one change of `side(d)` from −1 to +1 or +1 to −1
   (passing through 0 doesn't count on its own). If the level's crosses within
   the last `CONTEST_WINDOW_MIN` ≥ `CONTEST_CROSSES` → `contested`. It leaves
   `contested` only when side stays ±1 for `HOLD_MIN` → `held_up` / `held_down`.
2. **Knife.** If in one tick spot moved through `L` from one side to the other by
   more than `KNIFE_PTS` (|Δspot| > `KNIFE_PTS` and the sign of `d` changed) →
   `broken_down` (downward) / `broken_up` (upward), from **any** non-contested state.
3. **Cross.** Side changes to +1 from −1 (or from 0 after last being −1) →
   `crossed_up`; to −1 from +1 → `crossed_down`. Starts the hold timer.
4. **Hold timer.** While `crossed_*`: the timer runs while side stays on the
   crossed side. If side returns to **0** (band) the timer **pauses**; a stay in the
   band longer than `GRACE_SEC` **resets** it to zero. Side going to the origin
   side → `failed_*` (rule 5). Timer ≥ `HOLD_MIN` → `held_*`.
5. **Fail.** From `crossed_up`: side becomes −1 → `failed_up` (price failed to hold
   above and is back below). Mirror for `crossed_down` → `failed_down`.
6. **Break of a held level.** Each level keeps a `last_held_side` memory (+1 after
   `held_up`, −1 after `held_down`; cleared only by a knife or the end of day),
   because rule 3 overwrites the state with `crossed_*` before `held_*` is reached
   again. With `last_held_side = +1` (support from above): a new `crossed_down`
   that then reaches `held_down` → `broken_down` (lost support).
   Mirror: from `held_down` a `crossed_up` reaching `held_up` → `broken_up`
   (resistance taken). A knife (rule 2) breaks it immediately.
7. **Settle.** `failed_*`, `broken_*` and `held_*` decay back to `away_*` /
   `testing` only after `|d| > TEST_PTS` for `HOLD_MIN` with no new cross —
   so the event stays visible to the agents for the rest of its relevance.
8. Otherwise: `away_above` / `away_below` / `testing` from `d`.

**Zone** = the nearest level below spot (`lower`) and above (`upper`). Above the
highest → `above_all`; below the lowest → `below_all`. A level whose band
contains spot counts as **both** the zone's `lower` and `upper` for that tick.

### 7.2 Scenario mapping (complete)

`T_up` = the next level above the reference level; `T_down` = the next below.
Up-side events are mirrored exactly for down-side events.

| Event (on the level named) | Up target | Down target | Lean |
|---|---|---|---|
| no events in the zone | `upper` | `lower` | none |
| `upper` → `crossed_up` | next above `upper` | `upper` | up (pending hold) |
| `upper` → `held_up` | next above `upper` | `upper` | **up** |
| `upper` → `failed_up` | `upper` | `lower` | **down** |
| `upper` → `broken_up` (knife or taken resistance) | next above `upper` | `upper` | **up** |
| `lower` → `crossed_down` | `lower` | next below `lower` | down (pending hold) |
| `lower` → `held_down` | `lower` | next below `lower` | **down** |
| `lower` → `failed_down` | `upper` | `lower` | **up** |
| `lower` → `broken_down` (knife or lost support) | `lower` | next below `lower` | **down** |
| any bracketing level `contested` | `upper` | `lower` | **range** between them |
| `above_all` / `below_all` | — | — | **done** (no new call; retest logic later) |

**Both bracketing levels have events:** the **most recent** event sets the lean;
`contested` on either always wins (range). Ties within one tick: the larger |d|
move wins.

## 8. Agents and the shared board

Agents are **plain functions**, not processes. Each tick, one loop runs them in a
fixed order; each reads the board (this tick's earlier posts + the persisted day
memory) and posts evidence. That ordering guarantees one agent's output is the
next agent's input.

```
tick → [1] state machine → [2] derivatives → [3] up watcher, [4] down watcher
     → [5] risk/reward → [6] strategy → [7] manager (may re-pass 2–6, ≤ MAX_REPASS)
```

Evidence: `{agent, scenario, weight ∈ [−1,1], kind, detail, reason}`.

### 8.1 Derivatives agent (reuses today's engine output)

Reads the feed's `bias` dict (walls, net GEX/DEX, flow premiums, magnet). Posts:
- `wall_pull`: put/call wall between spot and a target → support / resistance for
  that scenario (weight from wall strength × reach).
- `dex_skew`: net DEX sign → small weight toward up/down.
- `flow`: call vs put premium share → weight toward up/down.
It **does not decide**; up/down watchers read its posts and confirm or discount
them against price action.

### 8.2 Up watcher and Down watcher (mirror images)

For their side's target level `T` (from §7.2):
- `progress`: distance covered toward `T` since the zone was entered, and pace
  (points per minute over `DETAIL_WINDOW_MIN`, `_reach`-style scaling).
- `hold`: the hold timer of the level just crossed toward `T`.
- `premium_build`: when a level is crossed, **freeze a baseline** of the mid at
  `T`'s strike (calls for up, puts for down); each tick compare live vs baseline
  (the `simmer_premium_tracker` pattern) → building / fading.
- `retouch`: price returned to the level it came from → negative for its side.
- `derivatives_agreement`: does the derivatives agent's evidence point the same
  way while price confirms? Agreement amplifies; disagreement without price
  confirmation is discounted.

### 8.3 Risk / Reward agent

For the strategy agent's candidates, and for each reach (`near` = next levels,
`extreme` = the day's highest / lowest levels):
- `cost_now` (debit), `value_at_target` (Black-Scholes projection of the legs at
  spot = target level, at the projected time — ported from the Matrix Lookup grid),
  `payoff_multiple = value_at_target / cost_now`, distance in expected moves.
Posts `rr` evidence (doesn't move direction beliefs; informs the pick).

### 8.4 Strategy agent

- Candidate set (**level engine only**): long call, long put, bull call, bear put,
  call fly, put fly, **straddle**, **strangle**, iron condor (range days only).
  No credit verticals (bull put / bear call) — today's engine keeps them.
- Strikes come **from levels** (nearest listed strike), fixed for the day:
  - up: long at current zone's `lower`/spot strike, short at `T_up`; fly centred on `T_up`.
  - down: mirror.
  - straddle: at the nearest level to spot. Strangle: call at next-up level, put at
    next-down level (`near`); `extreme` variant at the day's highest/lowest.
  - condor: wings at the contested zone's two levels.
- Scoring: existing `score_vertical` / `score_butterfly` / `score_condor` with these
  contracts, `compute_limit_premiums`, `composite_score`, `composite_verdict` —
  **unchanged**. New: a small `score_straddle_strangle` producing the same fields
  (net debit, breakevens, max loss = debit, EV, POP) so `composite_score` works.
- Strikes are **rebuilt only** when every level on one side has been passed
  (targets achieved) or the day's levels are edited.
- **Health = R/R to the level** (revised 2026-11-23). The legacy health rule
  marks every debit at DTE ≤ 2 `broken` and measures max profit at expiry over the
  full width, so it never fits a 0DTE level trade. Level picks set
  `health` from `rr_profit = (value_at_target − cost) / cost` (§8.3, near reach):
  `≥ 0.60` healthy, `0.30–0.60` thin, `< 0.30` capital_trap (the legacy debit
  bands, applied to the level target); no DTE veto. Set before `composite_score`
  (so the badge ranks on it); the scorer's own value is kept as `health_legacy`.
  The card shows R/R in the health slot. Limit-premium tiers unchanged.

### 8.5 Manager (orchestrator)

Who posts `range` evidence: the state machine (`contested` → +0.6), both watchers
when pace toward **both** targets is below a floor (`stall` → +0.3), and the
derivatives agent when spot is pinned at a strong positive-gamma wall inside the
zone (`pin` → +0.3).

Belief update, per tick, per scenario `s ∈ {up, down, range}` in log-odds `ℓ_s`:

1. **Decay:** `ℓ_s ← λ · ℓ_s`, with `λ = 0.5 ^ (TICK_SEC / (EVIDENCE_HALF_LIFE_MIN·60))`.
2. **Lean:** the §7.2 lean adds `LEAN_LOGODDS` to its scenario (none → no add).
3. **Evidence:** `ℓ_s += EVIDENCE_SCALE · Σ w` over this tick's evidence for `s`
   (each `w ∈ [−1, 1]`).
4. **Probabilities:** `p = softmax(ℓ)`.

Decision (margins are on **probabilities**):
- Call the top scenario when `p_top − p_second ≥ DECIDE_MARGIN`; else `wait`.
- To **change** an existing call: `p_new − p_current ≥ FLIP_MARGIN` **and**
  `MIN_DWELL_MIN` since the last change.
- **Thin evidence:** `Σ|w| < THIN_FLOOR` this tick → re-run the derivatives and
  watcher agents over `2 × DETAIL_WINDOW_MIN` and re-apply step 3, at most
  `MAX_REPASS` times; still thin → keep the current call.
- `done` when the zone is `above_all` / `below_all` (§7.2).

Output per tick: call (up / down / range / wait / done), target and invalidation
levels (from §7.2), confidence (`p_top`), the pick, and a plain-English **trace**
listing the evidence that moved `ℓ` most, in order.

## 9. Situations the spec must handle (acceptance cases)

| Case | Expected |
|---|---|
| Opens inside a zone, drifts | `wait` / range lean; no pick until evidence |
| Reclaims `upper`, holds 10 min, premium at next-up building | **up**, target next-up, invalidation `upper` |
| Crosses `upper`, falls back within 10 min | `upper` → `failed_up`; lean **down** to `lower` |
| Knifes through `lower` (> `KNIFE_PTS` in one tick) | `broken_down`; **down** to next below |
| Zigzags across one level 3× in 30 min | `contested`; **range** (condor/fly between neighbours) |
| Two levels 3 pts apart | merged into one cluster (§7.0) |
| Wick back into the band for 30 s while `crossed_up` | timer pauses, not reset (`GRACE_SEC`) |
| Derivatives say up, price flat | discounted; no call change |
| Up call open, put premium builds and price retouches origin | down evidence accumulates; flip only past `FLIP_MARGIN` + dwell |
| Passes the highest level | `done`; no new call (retest agent later) |
| Levels edited mid-session | re-read next tick; strikes rebuilt; audit row |
| Feed stale (ts not advancing) | no tick, no decision; status shows stale |

## 10. Admin page — "Level Engine Management"

Single server-rendered HTML page on the level engine's **admin listener** (§3.1:
container `0.0.0.0:8790`, published to the host as `127.0.0.1:8790` only; never
proxied, never on the tunnel).
- **Today:** table of levels (symbol, price, name, kind, condition text, source);
  add, edit, delete; **bulk paste** (`SPY 595.20 PM high` per line); converted SPX
  values shown once the ratio is set.
- **Past dates:** date picker → levels, each level's outcome, legs and the day
  summary, **read-only** (server refuses writes for `trade_date < today`).
- Every write → `lvl_level_audit`.
- Make targets (backup / automation): `make levels-show DATE=…`,
  `make levels-import FILE=levels.csv`.

## 11. Pick contract (what Matrix renders)

Same keys as `engine_pick` (§2), plus:
`source: "levels"`, `pick_id` (for the order dialog, §3.4), `call` (up/down/range/wait/done), `target_level` {price, name},
`invalidation_level` {price, name}, `confidence`, `reason` (one line from the trace),
`rr` {near: {cost, value, multiple, target}, extreme: {…}}.
The tile shows one compact line for `rr`:
`Near: risk $310 → +$900 at 7,280 · Extreme: risk $85 → +$1,400 at 7,320`.

## 12. Grading (event-based, by level — binary, revised 2026-11-23)

- A **leg** opens when the manager makes a call (up/down/range) with a pick.
- **Directional legs are binary:** `win` if spot comes within `HIT_TOL_PTS`
  (default 2.0 SPX pts ≈ daily ATR × 0.03, the Confluencer `levelTol`; SPY levels
  are graded in SPX space, so ≈ $0.20 on SPY) of the target level before the leg
  ends; otherwise `loss`. The leg ends on the first of: target hit (`win`),
  invalidation broken (`loss`), call flips (`loss` unless the target was hit),
  session close (`loss` unless hit). `close_reason` records which
  (`target | invalidation | flipped | session_close`).
- **Range legs:** `win` if price holds the zone until the call changes or the
  close; `loss` if it leaves (`close_reason` `zone_held | zone_exit`).
- Recorded: time to target, max adverse excursion, progress %, strategy P&L at
  close (legs repriced from the feed's contracts).
- Win rate = wins / (wins + losses). Stats: hit rate per call type and **per level
  name**. Served by `GET /levels/stats/{symbol}` for Matrix's outcomes tile.
- With `MATRIX_DECISION_SOURCE=levels` the legacy evaluator does not grade (§3.4).

## 13. End of day

At session close: `lvl_day_summary` — close, final zone, each level's outcome
(held as support / resistance, failed, broken, contested, untested), leg counts.
Read-only afterwards. (Using it to seed the next day is a later feature.)

## 14. Replay harness

`make levels-replay DATE=YYYY-MM-DD [CONFIG=…]` re-runs the whole pipeline over
that day's `lvl_ticks` (spot, ratio, the stored `chain` and `bias`) and that day's
levels with a given config. It connects as `facades_levels_engine_replay`, which can only
**read** live `lvl_*` tables and **write** schema `lvl_replay` (§5.1) — so a replay
can't touch live data by construction, not just by a parameter. It prints the
legs and results and can diff two configs. Because ticks carry the chain, the
strategy and R/R agents replay exactly. Built in phase 1, before any decision logic.

## 15. Torque (later phase)

- Reads `lvl_levels` (today) and the latest decision.
- Strike auto-fill snaps to level strikes; a **reach** selector (`Next level` /
  `Extreme`) on strangle / long call / long put.
- TP at the target level, SL at the invalidation level; the guardian exits when
  price reaches them (reusing its re-pricing / exit machinery).

## 16. Phases

1. **Foundation:** tables, admin page + make targets, SPY→SPX conversion, backend
   feed endpoint, level-engine container with tick recording, state machine,
   replay harness. *No picks yet.* Matrix shows nothing new.
2. **Brain:** agents + manager + traces; decisions recorded and graded in shadow;
   read API; `/levels/*` proxy.
3. **Matrix:** strategy agent (incl. straddle/strangle scorer), Risk/Reward
   (projection port), `MATRIX_DECISION_SOURCE=levels`, the tile line.
4. **Torque:** level-based exits and reach.
5. **Later:** `condition_text` → `condition_rule` (LLM at entry, approved),
   overshoot / retest agent, next-day use of the day summary, NDX mirror,
   Reflex/Simmer consumers.

## 17. Risks and guards

- **Bad levels** (typo): admin validates price within ±5% of the prior close.
- **Feed outage:** no tick, no decision; status `stale`; legs stay open.
- **Ratio drift:** locked at 09:35 by design.
- **Thresholds untested:** every default (`BUFFER_PTS`, `KNIFE_PTS`, `HOLD_MIN`,
  margins) is a starting point. `KNIFE_PTS=5` in particular can fire on an
  ordinary 20 s SPX move — tune all of them in replay (§14) before going live.
- **Over-trading:** flip margin + dwell + `wait` default.
- **Exposure:** the feed is token-guarded, fail-closed and refuses tunnel traffic;
  the proxy is a three-GET allow-list behind Matrix auth; admin + writes live on a
  separate loopback-only listener; the DB role sees `lvl_*` only.
- **Regression:** legacy engine, evaluator, parity tests and signals untouched;
  the backend changes are the cached contracts / SPY quote, the feed endpoint,
  the `/levels/*` allow-list proxy, the response-only pick swap and the order
  dialog's level-pick lookup — all behind `LEVEL_FEED_ENABLED` /
  `MATRIX_DECISION_SOURCE`.
