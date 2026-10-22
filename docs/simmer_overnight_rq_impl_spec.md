# Simmer Overnight (RelativeQs movers) — implementation spec

**Single source of truth for the overnight-mover pipeline.** Merges the former
`simmer_overnight_llm_spec.md` (LLM catalyst analyzer) and `simmer_overnight_option_b.md`
(Matrix-engine route), adds the multi-day live findings, and specs the drift work
(Phase A built, Phase B planned). Related: `docs/simmer.md` → "Overnight movers",
`docs/simmer_off_hours_catalyst.md`.

Status legend: **BUILT** = shipped & tested · **PLANNED** = specced, not built ·
**PARKED** = explored, not pursued.

---

## 0. What it is

RelativeQs (`edge-relativeq.facades.trade`) is a sibling proprietary scanner that
surfaces the overnight Nasdaq-100 pre-market movers in one board. Simmer imports that
board each morning, and for each mover decides a **premium-selling call** —
`sell_bear_call | sell_bull_put | wait | skip` — from three deterministic legs plus a
grounded LLM catalyst read:

1. **Premium/price engine** (`simmer_premium_tracker.py`) — the toward-move option
   premiums vs their 09:25 baseline (building/holding/fading) and the underlying path
   (extending/holding/retracing). Deterministic. This is the master signal.
2. **Catalyst LLM read** (`simmer_overnight_catalyst.py` + `simmer_llm/`) — *why* it
   moved, deserved vs overreaction, web/social vibe, sustain. An INPUT, never the math.
3. **Combiner** (`simmer_overnight_combiner.py`) — pure fn that folds the LLM read onto
   the premium call. Catalyst can confirm / veto / (in blended mode) promote a wait;
   it never computes a number and never sizes a trade.

Core rules (CLAUDE.md): **never delegate math/decisions to the LLM**; **catalyst never
promotes** a trade the deterministic engine didn't already allow (except the explicit
blended-promote path). Imported RQ tickers are deleted EOD; **the analysis history is
kept** (`simmer_overnight_analysis` rows are never deleted — they are the forward test).

---

## 1. Persistence

- `simmer_overnight` — the lean core row the Overnight tab renders: premium baseline +
  the live premium verdict. Deleted EOD by the RQ reset. Carries an `opp` jsonb
  (migration 0030) with the OPPOSITE (reversal) side's `{side, strikes, baseline,
  latest}`, captured from the SAME chain the toward side fetches (zero extra broker
  calls) → powers the two-sided skew (§5).
- **`simmer_overnight_analysis`** (keyed `(symbol, session_date)`, never deleted — the
  per-day history / forward test):
  - `readings` jsonb — `[{t:"09:25"|"10:00"|"16:00", spot, premiums:{strike:mid}}]` (3/day).
  - `premarket` jsonb — the 09:25 anchor `{spot, gap, premiums}`.
  - `close` jsonb — 16:00 snapshot + `intraday_pct` + the per-candidate scored calls &
    `_right` grades (see §4).
  - `outcome` text — realized `held | faded | extended` (+ `direction_right`).
  - `premium` jsonb — the premium-only verdict snapshot (now carries the two-sided
    `opp_mean_ratio`, `skew_shift`, and `gap_retraced` leading signals, §5).
  - `llm` jsonb — the live provider's catalyst verdict (+ provider/model/tokens/queries).
  - `llm_shadow` jsonb — **RETIRED** (see §3); historical rows retain it.
  - `combiner` jsonb — `{mode, final, llm_only, combiner_blended, premium_reactive,
    decided_at?}` (see §4).

Reading job is a child of `overnight_loop`: 09:25 (baseline + LLM), ~10:00 (reading +
combiner freeze), 16:00 (close reading + outcome + grading). `refresh_analysis` updates
the live premium verdict every loop tick while open.

---

## 2. Config (all DB-held in `simmer_engine_settings`, no restart)

API **keys** stay in `edgelane_market.config`; everything else is a DB section,
re-read right before each run (`simmer_config.refresh_setup()`).

`overnight_llm` section (`simmer_config.overnight_llm()`):

| key | default | notes |
|---|---|---|
| `enabled` | `false` | master switch |
| `provider` / `model` | `openai` / `gpt-6-luna` | live catalyst model |
| `thinking` / `context_size` | `medium` / `medium` | reasoning effort / web_search context |
| `premium_context_to_llm` | `true` | feed the one-line premium trend into call 1 |
| `decision_mode` | `llm_only` | which logged call is `final`: `llm_only` \| `combiner_blended` |
| `shadow_provider` / `shadow_model` | `""` / `""` | **RETIRED 2026-10-20** (was gemini) |

`overnight_drift` section (`simmer_config.overnight_drift()`) — Phase A, see §5:

| key | default | notes |
|---|---|---|
| `enabled` | `true` | off → reactive call mirrors the frozen call |
| `flip_ratio` | `0.12` | toward-move premium must move ≥12% vs baseline to flip a frozen call |
| `require_underlying` | `true` | also require the tape to be retracing/holding, not extending with the thesis |
| `min_premium_conf` | `45` | live premium verdict needs this conviction to override |
| `reclaim_frac` | `0.6` | **leading:** ≥60% of the gap given back intraday = reversal |
| `skew_flip` | `0.10` | **leading:** opposite side building ≥10% more than the toward side |

**Thresholds are calibrated from `simmer_overnight_analysis` outcomes, not hand-fiddled.**
They're graded every day (§4) so we see whether they're right rather than guessing.

---

## 3. Multi-day findings (2026-10-13 … 10-20) and the shadow retirement

Six graded sessions, both models live:

- **Directional hit-rate ≈ coin flip:** `llm_only` ~47% (21/45 graded), `combiner_blended`
  ~46% (17/37). The blend does **not** beat the raw LLM — several days it's worse
  (10/20: llm 4/8 vs combiner 2/5). `decision_mode` stays `llm_only`.
- **Days are regime-driven, not name-driven:** the whole board extends together (10/14,
  10/16) or fades together (10/15 — 8/8 faded). The pre-market call wins when the market
  trends with the gap and loses when it mean-reverts. → the "stuck on the pre-market read"
  problem, confirmed. This is what Phase A targets.
- **The drift signal already exists, unused:** premiums are snapshotted 09:25/10:00/16:00,
  so decay is recorded, but the scored call freezes at 10:00 and the LLM fires once at
  09:25 and is never revisited.
- **LRCX 10/20 (the motivating case):** bear-call at 09:25 (263.37); by 10:00 it popped
  to 265.29 with call premiums inflating — thesis visibly contested — then round-tripped
  to 263.46 (net held, call graded right). The doubt signal sat in the 10:00 reading
  unused.
- **Shadow / second LLM — RETIRED 2026-10-20:** across the 4 days it ran it produced
  **0 disagreements with the live model on 33 names**, and it wasn't running the last 2
  days. Pure cost, zero added signal. `shadow_provider`/`shadow_model` set to `""`.

---

## 4. Scoring / bake-off (BUILT)

The 16:00 close scores EVERY candidate independently into `close`, so we can compare
modes no matter what `decision_mode` is set to — each call is made before the outcome
window:

- `llm_only_call` / `_right` — the catalyst call, frozen 09:25.
- `combiner_blended_call` / `_right` — the premium-gated blend, frozen 10:00.
- `premium_reactive_call` / `_right` — **Phase A**, the reactive call FROZEN at the
  noon checkpoint and graded from the spot AT that freeze (`spot_at`) to the close, so
  it's credited only for the move made after the decision — never the pre-noon move. §5.
- `premium_reactive_live_call` / `_right_nonpredictive` — the last live reactive call
  (post-noon). Logged for observing the path; **never** the reactive score — a late
  flip toward the realized move must not be graded as a pre-outcome decision.
- top-level `direction_right` — the `decision_mode` `final`.

Promotion is decided by these grades, not by argument: the reactive call becomes the
user-facing default only if it out-scores the frozen ones over the forward test.

---

## 5. Phase A — premium-is-master drift override (BUILT)

**Problem:** the scored call freezes at 10:00 for bake-off fairness, which is exactly
what leaves it stuck on the pre-market read when the tape turns intraday.

**Fix:** a SEPARATE, never-frozen **reactive call** recomputed every refresh tick from
the live premium verdict, while the frozen `final` is left untouched for the bake-off.
Premium is the master signal — when it materially contradicts the frozen call it
cancels the stale catalyst / pre-market / 09:25-LLM read.

`reactive_call(frozen, live_premium, drift_cfg)` (pure fn, `simmer_overnight_combiner.py`):

- live premium still names the **same side** → **hold** the frozen call (`frozen_hold`).
- live premium **materially** flips to the **opposite** side — drift `|mean_ratio-1| ≥
  flip_ratio`, `confidence ≥ min_premium_conf`, and (if `require_underlying`) the tape
  retracing/holding → **flip** to the tape (`premium_override`).
- live premium stopped backing the thesis (went wait/skip, or opposite but not yet
  material) → **stand down** to `wait` (`premium_drift_standdown`) — don't hold a decaying
  call, don't chase a wiggle.

Wired in `refresh_combiner`: the live reactive call is recomputed every tick (frozen
combiner rows included; the frozen `final`/`decided_at` are preserved, writes only on an
actual change — no churn). The **scored** reactive call freezes at a **noon checkpoint**
(`reactive_lock`) — later than the combiner's 10:00 lock so it gets the morning to
react, but well before the close so its grade carries no lookahead. The live call's path
is kept in `combiner.premium_reactive_history` — appended ONLY on a change (a handful of
entries/day, capped at 12), non-predictive, for observability and the future UI's
always-live call. Graded at close: `premium_reactive_call`/`_right` (the noon freeze)
plus a labelled-non-predictive `premium_reactive_live_*`.

**Leading signals (so it isn't late).** Toward-side premium magnitude is a *confirming*
signal — it lags the move. Two leading inputs, both free from the chain already fetched,
let the override react *before* the toward premium deflates:

- **Layer 1 — price reclaim (`gap_retraced`).** Fraction of the overnight gap the
  underlying has given back intraday. Price reclaiming the gap is the fastest honest
  reversal tell; `reclaim_frac` (0.6) trips the flip/stand-down.
- **Layer 2 — two-sided skew (`skew_shift`).** We now snapshot BOTH sides from the same
  chain and compare the opposite (reversal) side building vs the toward side fading. A
  debunk shows up first as the opposite side bid up *before* the toward side deflates;
  `skew_flip` (0.10) trips it. Computed on EVERY analysis, not just on a reversal.

`reactive_call` flips to the opposite side when the live verdict names it AND (toward
magnitude **or** a leading signal) clears the bar; it **stands down early** the moment a
leading signal fires even while the verdict still names the frozen side — the fix for
"by the time it reacts it's too late." On a violent reversal the premium verdict yields
`skip`, so the reactive call stands down (`wait`) rather than selling the opposite side
into the repricing; a confident opposite flip needs the reversal to settle (premium
crushing while the level holds).

**Still only reacts to the market's read, not the news itself** — the news-triggered LLM
recall is Phase B. And if a reversal keeps the toward premium fat (a vol spike) with no
price reclaim or skew tip, it stands down rather than flips: premium/price must actually
move, not just the headline.

**Tests:** `test_overnight_combiner.py` (pure-fn edges) and `test_overnight_catalyst.py`
(`test_premium_drift_simulation_reactive_tracks_the_tape` drives a full intraday premium
path: confirm→mild-drift→material-flip→re-commit, asserting the reactive call reacts at
each step while the frozen `final` never moves).

---

## 5b. One verdict everywhere (card + alert)

The Overnight card shows a SINGLE reconciled call — `simmer_overnight_combiner.reconcile()`
folds the premium/price read, the catalyst read, and the intraday drift override into
one `call` + the ordered `steps` that produced it (setup → premium → catalyst →
intraday; the intraday override is the final call). No second verdict, no llm_only↔
combiner toggle (they resolved to the same call). The internal bake-off candidates stay
in `simmer_overnight_analysis` for grading — not user-facing.

The **≥min in-app alert** (`overnight_alert_fanout`) fires on that SAME reconciled call,
not the frozen `combiner.final`. It keeps **one current alert row per user per symbol per
session**, kept up to date (`_overnight_alert_action` decides insert / update-in-place /
nothing):

- **Acknowledged + same verdict → never re-pops.** (Fixes "I ack all my alerts and they
  come back minutes later" — the fan-out used to read only `acknowledged=false`, so once
  you cleared them the query was empty and it re-inserted.)
- **Unacknowledged + new verdict, or a meaningful conviction rise → overwrites the open
  row in place** (same id; the card shows the current call, no duplicate rows).
- **New verdict after you acknowledged the old one → a fresh alert** (new id → it beeps).
- A **flip must HOLD `flip_confirm_sec` (default 540s ≈ 9 min)** before it re-alerts, so a
  name oscillating around the flip/skew thresholds can't beep every tick. NEW alerts are
  capped at `cap` (3) per user per symbol per session.
- The read **fails closed**: a `None`/error tick touches nothing; `[]` (scoped to today)
  = no alert yet = insert.

All four knobs live in the DB `overnight_alert` section (`min_conf`, `rise`,
`flip_confirm_sec`, `cap`) — so "how long to watch a direction change" is tunable without
a redeploy. Guards (`flip_seen` timestamps, per-session NEW-alert count) are in memory and
reset on restart (acceptable). Per-user opt-out (`notify_overnight`) unchanged.

## 5c. Daily lifecycle (import / analysis / reset)

- **07:00 ET — import + baseline + RQ crowd.** `_should_import` opens at 07:00 (quotes
  are the prior close pre-open), front-loading the board so it's ready before the open.
  The **day's first import first clears yesterday's RQ list** (`reset_rq_end_of_day`,
  analysis preserved) — gated by `_new_day_reset_action(today_rows)`: `[]` → reset, rows
  present (restart) → skip, **`None` (read error) → do nothing** (never wipe a live board).
- **~09:20 — near-open re-import.** RelativeQs grows its board through the morning, so one
  idempotent pass near 09:20 catches names added after 07:00 (run_import skips the rest).
- **09:25 — LLM catalyst starts** (`_should_catalyst` floor; an LLM read before the open
  is pointless). No 10:30 ceiling: a **late start** (backend up mid-session) still imports
  and analyzes directly; the loop winds the catalyst down on run_catalyst's `complete`.
- **Late reads are non-predictive.** The LLM read is stamped `llm.at`; a read after ~09:45
  (late start) has its `llm_only`/`combiner_blended` close grades set to `None` + `late_llm`
  so it never contaminates the bake-off as a pre-open call (the reactive candidate is
  already graded from its own `spot_at`).
- **After 16:00 — items stay.** No EOD reset; RQ items remain on the watchlist (market
  closed → nothing analyzes them). They're cleared by the next morning's first import.

## 6. Phase B — premium-triggered LLM recall (BUILT, default OFF)

A second, targeted catalyst call — **only when the premiums say the story changed**, so
the LLM can re-read the "why" (is the market treating it as an overreaction, is social
turning) against the new tape. `simmer_overnight_catalyst.run_recall()`, wired into the
loop after the combiner/alerts, gated by `overnight_llm.recall_enabled` (default **false**
— it's a paid call).

- **Trigger (`_recall_target`, pure):** fire a recall only when Phase A drove the
  reconciled call to the OPPOSITE committed side of the 09:25 read (`premium_reactive`
  `source == "premium_override"` and the call contradicts `llm.call`). De-duped by an
  `llm_recall.for_call` marker so a standing override never re-spends; capped at
  `recall_cap` (2) per name per session. Calm day = 0 recalls.
- **Premium stays master.** Phase A already moved the call on premium alone; the recall
  only **annotates** the catalyst reasoning (why_moved / deserved / sentiment / sustain)
  and is surfaced in the card (route merges `llm_recall` over the stale `llm`; a
  `↻ re-checked` chip shows it). It never overrides the reconciled call.
- **Input:** a recall-flavoured grounded-read context stating the tape has reversed
  against the 09:25 read, with the premium engine's live words + gap-retraced. Reuses the
  existing two-call adapter; no new provider.
- **Cost:** ≤ `recall_cap` successful calls on the minority of names that genuinely
  reverse. A recall that keeps FAILING persists nothing (its dedup never sets), so it is
  separately **attempt-capped** (`_MAX_RECALL_ATTEMPTS`, 3, in memory per session/name/
  target) — otherwise a broken provider would re-pay every tick while the reversal holds.
- Persisted to `simmer_overnight_analysis.llm_recall` (migration **0031** — required, or
  the upsert fails PGRST204 and silently drops).

## 6b. Provider reliability (OpenAI rate limits)

Grounded `web_search` reads are ~30K input tokens each and take ~26–30s. A burst of 9
names tripped OpenAI's per-minute **token** limit — the old adapter didn't check the HTTP
status, so a 429/402 was parsed as an empty read and **silently swallowed** (no log, no
retry), leaving 2/9 names analyzed on 2026-10-22. Fixes:
- **`openai._post`** now checks status: retries **429/5xx** up to `_MAX_RETRIES` (3) with
  exponential backoff + jitter, honoring **`Retry-After`** (OpenAI's guidance); a **402**
  (quota/credit) or 401/403 is logged loudly and **not** retried; timeout raised to 90s.
- **Loop-safety** (the LLM pass runs INLINE in `overnight_loop` before readings/refresh/
  alerts): a single backoff/`Retry-After` is capped at `_MAX_RETRY_WAIT` (30s) — anything
  longer gives up this tick (the per-name attempt cap retries next tick); only
  **connect-type** errors are retried, a **read timeout** returns None (it may already be
  billed, and retrying would block); and `run_catalyst`/`run_recall` carry a wall-clock
  budget `overnight_llm.call_budget_sec` (480s) — once elapsed they stop starting new
  names and resume next tick, so worst-case retries can't starve the 10:00/16:00 readings
  or the alert fan-out.
- **`overnight_llm.call_spacing_sec`** (3) spaces successive grounded reads in
  `run_catalyst`/`run_recall` so the burst stays under the token-per-minute limit.
Separately, the Gemini **news** path is out of prepaid credit (HTTP 402) — a billing
top-up, unrelated to the overnight catalyst (which is OpenAI).

---

## 7. Option B — Matrix engine as strategy selector (PARKED)

Explored 2026-10-12. Hand an RQ ticker's live chain to Matrix's
`compute_engine_output(symbol, spot, chain, expiration, dte)` and let its calibrated
composite pick the structure + strikes + a quality score. Feasible (Matrix is not
index-locked; it estimates expected-move from the ATM straddle), and **complementary**,
not redundant: the premium read answers *is now the time* (holding/fading); Matrix
answers *what's the best structure and how good*.

Not pursued yet. Caveats to validate first: single-name GEX/walls are noisier than the
SPX/NDX books Matrix was tuned on; dealer bias can lag an overnight news gap (pass the
gap+premium direction in rather than trust Matrix's own bias day one); market-hours only
(the 09:25 baseline is pre-open). If built: a thin adapter in `simmer_overnight.py`
behind an `OVERNIGHT_ANALYZER = premium | matrix | both` flag, defaulting to `premium`.

---

## 8. Non-goals / constraints

No per-minute premium recording (3 readings/day). No LLM-computed numbers. No catalyst
promoting a trade the engine disallows (outside blended-promote). No framework
dependency (thin adapter, mirrors reflex's gemini/openai split). Thresholds live in the
DB and are graded, never hardcoded guesses.
