# Simmer — DTE-tiered rules + debit handoff (plan)

**Status: tier LOGIC built + tested; intraday DATA PATH deferred.** The
DTE-tiered gates, vol references, catalyst-weight-by-DTE, structure steering and
debit-handoff notice (§1–6) are implemented in `simmer_engine.py` /
`simmer_config.py` as a pure function of (inputs, cfg) and covered by
`tests/simmer/test_dte_tiers.py`. They activate only when a caller injects the
short-tenor inputs (`research.rv_intraday`, `research.iv_change`); with those
absent, every tier falls back to today's daily path (6–45 DTE byte-identical).
**Not yet built:** the intraday OHLC + IV-snapshot capture that would feed those
inputs in production (see Prerequisite), and per-DTE-bucket paper calibration of
the thresholds. Sibling of `docs/simmer_earnings.md` and the (future) debit mode.
One doc — each tier / topic is a section below.

## The problem this fixes

Simmer applies **one daily-timeframe vol model to every expiry**:

- **RV = 20-day Yang-Zhang** (`simmer_config.VOL.yz_window = 20`, daily bars).
- **IV percentile = 252-day rank** (`iv_history_days = 252`); the engine notes
  "intraday recompute is meaningless" and only stores one daily `atm_iv`.
- The vol gates (`vrp_ratio_floor = 1.15`, `iv_percentile_floor = 40`) do **not
  branch on DTE** — there is no tenor-conditional path anywhere in the vol logic.

For a 0–1DTE that's a **timeframe mismatch**: a catalyst bar 1–2 days ago inflates
the 20-day RV and vetoes today's expiry (VRP < 1.15) even after the stock has
settled intraday, and a 252-day IV percentile can't see IV *richening today* into
a catalyst. NVDA on a catalyst day is the canonical case: 20-day RV ≈ 36%, VRP
0.94, IVP 0 → double veto, on a read that isn't the right one for a 0DTE.

## Prerequisite (the real blocker) — intraday data path

> **Status (shipped):** the intraday feed now populates `research.rv_intraday`
> (annualized RV from today's 5-min OHLC via the Yahoo v8 chart) and
> `research.iv_change` (change vs the session's first ATM-IV snapshot, stored in
> `simmer_iv_intraday`). The watcher fetches both ONLY during market hours; the
> Tradier provider has no intraday endpoint, so under it the short tiers keep
> falling back to the daily gates. Knobs live in `simmer_config.INTRADAY`.

The tiers below need data Simmer does **not** currently collect. Today the sweep
ingests **Yahoo daily bars only**: no intraday OHLC, no intraday IV snapshots.
Before any 0–5DTE rule can compute:

1. **Intraday OHLC** (e.g. 5-min bars, session-to-date) → an intraday realized-vol
   estimate for the short-tenor VRP.
2. **Periodic IV snapshots** (ATM IV every sweep, stored with a timestamp) → an
   **IV-change / richening** signal (is IV rising into the catalyst, or bleeding
   out post-event?). This is distinct from the 252-day percentile and must not
   replace it — it's a *second lens* for short tenors.

Until this path exists, 0–5DTE tiers can't be honestly gated; the daily gates
stay in force and keep vetoing (which is the safe default).

## Tiers overview

| Tier | RV reference | IV reference | Catalyst weight | Default stance |
|---|---|---|---|---|
| **0–1 DTE** | intraday realized (today's range) | IV-change (richening) + level | **highest** | gamma-dangerous; sell only on a clean intraday-VRP + rich/rising IV + strike beyond the expected intraday move & GEX wall |
| **2–5 DTE** | blend: intraday + short (3–5 day) RV | IV-change + short percentile | **high** | transitional; catalyst still dominates, term structure starts to matter |
| **6–21 DTE** | 20-day YZ (current) | 252-day percentile (current) | medium | the theta sweet spot — current daily gates are appropriate |
| **22–45 DTE** | 20-day YZ (current) | 252-day percentile (current) | low | standard credit; catalyst mostly priced/irrelevant to the drift |
| **> ~45 DTE** | — | — | — | **credit discouraged** → debit handoff (see below) |

## Section 1 — 0–1 DTE

The most different trade; deserves the most separate logic.

- **VRP is intraday, not 20-day.** Compute a same-session realized vol (intraday
  OHLC) and compare to the 0DTE ATM IV. A stale multi-day RV must not gate a
  0DTE. Keep a floor, but on the *right* timeframe.
- **IV rising is a GO, not a veto.** If IV is *richening* into a live catalyst
  (positive IV-change signal), that's premium getting fatter to sell — the
  opposite of the current "IVP = 0 → veto." Gate on IV-change + absolute level,
  not the 252-day percentile.
- **Catalyst carries the most weight here** (see Section 5): on a 0DTE the news
  read is a first-class input to *side + strike*, not a 0.05 also-ran. A positive
  catalyst that already moved the stock → bull put **below** the move, beyond the
  GEX put wall and outside the expected *intraday* move.
- **EV/scoring uses the same intraday RV as the gate.** When `rv_intraday` is
  present, it becomes the `rv_forecast` that drives EV, the POP edge and the
  credit-quality score — the *same* reference the intraday VRP gate reads.
  Gating on intraday RV but scoring EV off the stale 20-day RV would
  manufacture a `no_forecast_edge_over_market_iv` exactly where the intraday
  gate found edge. An explicitly injected `rv_forecast` still wins; daily YZ is
  the fallback when no intraday RV is supplied.
- **Hard guardrails (non-negotiable for 0DTE):** defined-risk vertical only;
  short strike beyond the intraday expected move AND the GEX wall; an intraday
  **time-stop** and a **delta/loss stop** (gamma can run the short strike fast);
  skip on a velocity burst (unpriced two-way risk).

## Section 2 — 2–5 DTE

A progression, not a copy of either neighbor.

- **RV = blend** of the intraday estimate and a short (3–5 day) realized, so a
  single catalyst bar doesn't dominate but recent movement still counts.
- **IV** = IV-change signal + a short-window percentile (not the full 252-day).
- **Term structure earns its keep:** `term_slope = iv_far/iv_near`. Backwardation
  (near IV > far, slope < 1) = the market pricing near-term stress → a sellable
  spike *if* it's compensated; contango = calmer. Weight it more than at 0DTE
  (where there's barely a term to speak of).
- **Catalyst weight: high but decaying** (Section 5).
- **RV = the same blend for both gate and score:** the intraday+YZ blend that
  feeds the 2-5 VRP gate is also the `rv_forecast` driving EV (see Section 1),
  unless a caller injects an explicit `rv_forecast`.

## Section 3 — 6–21 DTE (the theta sweet spot)

The current engine is already right here — this section exists to say *don't
change it*. 20-day YZ RV and 252-day IV percentile are the correct references at
this tenor; theta decay is meaningful; catalyst effect on the drift is fading.
Standard `vrp_ratio_floor`/`iv_percentile_floor` gates apply unchanged.

## Section 4 — 22–45 DTE (standard credit)

Same daily gates. Catalyst weight is low: by this tenor a known catalyst is
priced and the edge is structural (VRP + walls), not directional. No special
logic; this is the baseline the other tiers are measured against.

## Section 5 — Catalyst weight as a function of DTE

A catalyst's relevance to a *credit* trade is inversely related to tenor: it
matters enormously at 0DTE (you're trading the event window itself) and little at
45DTE (priced, and the drift washes out). So the news/earnings contribution
should **scale down with DTE** rather than be a flat 0.05:

- A **decay schedule** (illustrative, to be paper-calibrated — not final numbers):
  0–1DTE heaviest, tapering to ~current weight by ~21DTE, ~zero by 45DTE.
- Applies to **both** the news `sentiment_lean` and the earnings fold — the same
  event should not lift a 45DTE the way it informs a 0DTE.
- **Direction still never *promotes* a credit sell on its own** (that boundary
  holds); higher weight means catalyst can veto/steer *harder* at short tenor and
  drive side + strike, not that it becomes a buy signal.

## Section 6 — Longer-DTE credit → warn / hand off to debit

Your point: past a certain tenor, **selling a credit spread stops making sense** —
theta bleed is too slow to be the edge, and a catalyst can swing the whole
position the other way before decay does anything. At that tenor the *same*
conditions Simmer keeps vetoing on (cheap IV, live catalyst) are a **debit /
directional** setup, not "nothing here."

- **Trigger:** the user pins (or the auto-pick lands on) a **long tenor** — first
  cut ~**> 21 DTE**, firmly by **> 45 DTE** — **and** either IV is cheap
  (low percentile / VRP < 1) or a live directional catalyst is present.
- **Behavior:** instead of a bare veto, surface a **non-blocking notice**: *"At N
  DTE, credit theta is thin and a catalyst can overwhelm it — consider a **debit**
  (directional) structure."* Informational, like `avoid_if` — never auto-places
  anything.
- **Boundary:** a debit play needs a **direction call**, which Simmer deliberately
  does not make — that's Matrix's dealer-GEX bias engine (or the earnings
  analyzer). So this is a **handoff**, not a new Simmer forecast. The full debit
  mode is its own spec; this section only defines the *warning + handoff trigger*.

## Calibration (prerequisite before any tier ships)

Every threshold above (intraday VRP floor, IV-change cutoff, the catalyst decay
curve, the debit-handoff DTE line) must be **paper-calibrated per DTE bucket**
through the `simmer_outcomes` harness — win-rate / EV by tenor — not guessed. 0DTE
especially: no short-tenor cutoff goes to real money on intuition. This mirrors
the earnings-mode step-zero.

## Phasing

1. **Intraday data path** — capture intraday OHLC + timestamped ATM IV snapshots
   (the prerequisite; nothing else computes without it).
2. **Tier plumbing** — DTE-tiered vol references + gates (Sections 1–4), behind
   the existing gates so behavior is opt-in and comparable.
3. **Catalyst decay by DTE** (Section 5).
4. **Debit handoff notice** (Section 6) — informational only.
5. **Per-bucket paper calibration** → promote a tier from advisory to gating.

## Known limits

- **The "0-1" tier is effectively a 1-DTE tier today.** Time-to-expiry is
  CALENDAR-DTE (`t = dte / 365`), so a *pure* 0DTE means `t = 0`: the greeks
  degenerate (`d1/d2` collapse to a step function, every |delta| snaps to 0 or
  1), no strike falls in the 0.20-0.35 short-delta band, and `build_candidate`
  finds nothing to sell. So the intraday branch is exercisable only from ~1 DTE
  up until a **sub-day, fractional time-to-expiry** is available. A true
  intraday 0DTE (hours-to-expiry) needs that fractional `t`, which is part of
  the deferred intraday data path (the same prerequisite as `rv_intraday` /
  `iv_change`). Until then, treat "0-1" as "the shortest honestly-priceable
  tenor," not literal same-session expiry.

## Open decisions

1. Exact tier boundaries (0–1 vs 2–5 vs 6–21 — the seams are judgment calls).
2. The catalyst decay curve shape (linear vs exponential) — falls out of calibration.
3. The debit-handoff DTE line, and whether it's DTE-only or DTE × (cheap-IV | catalyst).
4. Intraday bar granularity (5-min vs 15-min) and IV-snapshot cadence vs the sweep.
