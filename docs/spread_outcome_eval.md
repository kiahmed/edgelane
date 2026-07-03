# Spread-outcome evaluation + bias trust state (spec)

Replaces the current **spot-difference** self-eval with one that grades the
**engine-picked spread's own net premium** moving in its favor — and gates how
picks/accuracy are shown behind a single *bias trust* state.

Status: SPEC — not yet implemented. Review before code.

## Why

Today `/accuracy` grades only the bias *direction*: did spot move >0.05% the way
the label predicted, 3 min later. It's blind to the actual recommended trade and
gives a naked win-rate % even when the bias is mid-meltdown. The user's ask:
grade the **called-out spread** (the single top/highest-composite engine pick),
in real premium, and stop publishing numbers when the bias is out of sync.

## The one metric (no branches)

For the **engine pick only** (the highest-composite spread shown on top — NOT the
also-ran cards):

1. **At decision (each poll):** save the pick's legs, its entry **mid net
   premium**, and its `spread_type` (credit | debit) onto the `bias_decisions`
   row.
2. **At eval (~3 min later):** re-price the *same saved legs* from the current
   cached chain → new mid net premium. (Re-mark the original legs, not whatever
   pick is on top now — the bias may have rotated.)
3. **Favorable delta** = premium change in the spread's profit direction:
   - **debit** (paid to open): wins as the spread **expands** → `fav = mid_now − mid_entry`
   - **credit** (collected): wins as the spread **decays toward 0** → `fav = mid_entry − mid_now`
4. **Friction band** = ½ × current bid/ask width of the spread (noise floor, so a
   bid/ask bounce isn't scored as a win).
5. **Result:**
   - `fav >  +friction` → **win**
   - `fav <  −friction` → **loss**
   - else → **neutral** (flat / inside noise)

No fixed %/points threshold — *any* real mid improvement that clears fill
friction counts. This is the agreed "3+1 combined" rule: progress on mid,
gated by the bid/ask noise band.

Because the pick is built *from* the bias, a winning spread = the bias direction
paying off in the actual recommended structure. Spot-diff eval is removed.

### outcomes columns (additive)
`entry_net_premium`, `eval_net_premium`, `favorable_delta`, `friction_band`,
`spread_type` — alongside the existing `result` (win|loss|neutral). Keep
`actual_move_pct`/spot fields for continuity or drop after migration (TBD; lean
keep, they're cheap context).

### bias_decisions columns (additive)
`pick_legs` (json), `pick_entry_mid` (double), `pick_spread_type` (varchar),
`pick_strategy` (varchar). Null when no pick that poll → not evaluated.

## Cadence (unchanged, clarified)

- A decision is **born every poll** (~15–20s), timestamped.
- The evaluator **wakes every 30s** only to ask "any decision now older than
  `EVAL_WINDOW_MIN` (3m) that I haven't graded?" — grades those **once**, sleeps.
- 30s ≠ grading frequency; it's the check interval (≤30s slack past the 3-min
  mark). Each decision is graded exactly once. It does not re-grade prior evals.

## Bias trust state (drives banner + pick presentation)

A single state computed from graded outcomes + the existing regime counters
(`REGIME_ALERT_CONSEC_LOSSES=3`, `REGIME_CLEAR_CONSEC_WINS=2`; neutrals don't move
counters). The regime evaluation **keeps running** in every state — only what's
*displayed* changes.

| State | When | "Recent outcomes" header | Pick card |
|---|---|---|---|
| **Calibrating** | fewer than `min_graded` (~10) graded outcomes | `Calibrating — N/min graded` (no %) | tag: *confirming edge — sizing in* |
| **In sync** | enough samples, no regime alert | the rolling win-rate % (earned) | normal |
| **Out of sync — paused** | regime alert ON (≥3 consec losses) | `⏸ recalibrating — bias re-syncing (K confirming wins to resume)` | tag: *bias re-syncing — wait for confirmation before sizing up* |
| **Low confidence** | bias card confidence = low (independent of streak) | win-rate shown but muted | tag: *lower-conviction read — confirm before sizing up* |

`K` = wins still needed to clear (`REGIME_CLEAR_CONSEC_WINS − current consec_wins`).

**Paused = quiet, not off:** stop *publishing* the win-rate, keep grading
silently underneath (the clearing wins are detected from those very evals). When
`consec_wins` reaches the clear threshold → alert off → win-rate returns.

### Pick-card hint (under the top pick)
Shown in Calibrating / Paused / Low-confidence — confidence framing, never
"don't trade" (the tool exists to find the winning play):
> *Bias re-syncing — wait for a confirming win before sizing up.*

Hidden in **In sync**.

## API / UI surface

- `/accuracy/{symbol}` returns: `state` (calibrating|in_sync|paused|low_conf),
  `win_rate` (null unless in_sync/low_conf), `graded`, `wins_to_resume`,
  `display_text`, plus the last N outcome rows (with premium deltas).
- UI: "Recent outcomes" header renders `display_text` verbatim; pick card shows
  the hint when `state != in_sync`. No naked % unless earned.

## Open decisions for the user

1. `min_graded` for leaving Calibrating — propose **10**.
2. Keep the legacy spot-diff columns on `outcomes`, or drop them? Propose keep.
3. Paused picks: relabel + hint only, or also visually dim the cards? Propose
   relabel + hint (don't hide — user may still want to watch).

## Tests

- Unit: favorable-delta + friction-band logic for credit vs debit (win/loss/flat
  across a mid move and a pure bid/ask-bounce that must score neutral).
- Unit: state machine (calibrating→in_sync→paused→resume) off synthetic outcome
  streaks.
- Re-price: saved legs re-marked from a chain where the bias has since rotated to
  a different pick (must mark the ORIGINAL legs).
- Parity: existing 56 JSX-parity tests untouched (engine math unchanged).

## Daily archive + data-quality (implemented)

Raw `bias_decisions`/`outcomes` are **never flushed** — they are the only labeled
ground-truth dataset. Instead the live reads are scoped (regime = today only;
accuracy = rolling window) and each **completed** ET day is rolled up once into
`outcome_daily_summary` (same DuckDB), written by `evaluator.archive_completed_days`
at startup and on ET-day rollover (idempotent; runs even when market closed).

Per `(session_date, symbol)`: `n / wins / losses / neutrals / accuracy_pct`,
`first_ts / last_ts / span_min`, `max_gap_min`, `coverage_pct`, and a **`complete`**
flag = covered ≥75% of the 390-min RTH session **and** no polling gap > 15 min.
Partial days (backend left off, frontend never launched) are **kept but flagged**
`complete=false` so modeling can require full sessions (`WHERE complete`). Tunable
constants live in `evaluator.py` (`_SESSION_MINUTES`, `_MAX_GAP_MIN`, `_MIN_COVERAGE`).

## Roadmap: historical modeling (future features)

The archive above exists to make these answerable — none should ever trigger a
daily flush; they all need longitudinal history:

1. **Threshold tuning** — is `regime_alert_consec_losses=3` / `clear=2` optimal?
   Replay real streaks. Same for `friction_band` and `neutral_band_pct`.
2. **Confidence calibration** — does bias `confidence=low` actually lose more than
   `high`? Today it's an assumption (it's the `low_conf` branch that still
   publishes the win-rate). Prove or kill it from `(confidence → result)` data.
3. **Per-strategy / per-regime edge** — e.g. "does `iron_condor` on NDX in a
   low-conviction regime actually win?" Join `pick_strategy` + regime state to
   outcomes over time. (`pick_strategy` is already persisted per decision.)
4. **Engine drift detection** — is accuracy trending down week-over-week (model
   going stale)? Chart `outcome_daily_summary.accuracy_pct` over `complete` days.

Note: the live regime streak is intentionally **strategy-agnostic** (a bias-trust
signal, not a per-strategy tracker); (3) is an offline modeling view, not a change
to the live gate.
