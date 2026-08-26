# Simmer — Earnings Analyzer (plan)

A standalone, opt-in "earnings mode" for Simmer. It lets Simmer **sell fat premium
into earnings** with the homework done first, instead of hard-vetoing every expiry
that falls in an earnings window.

Status: **plan only, not built.** Supersedes the current unconditional catalyst
veto for *earnings* (SEC 8-K / macro gates are unchanged).

## Why

The current catalyst gate hard-vetoes any expiry inside an earnings window. The
original spec (`docs/simmer_requirments.txt` L31–33) only ever asked for a *caution*
— "never sell inside an **unpriced** catalyst window **unless IV rank is
astronomically high**." "Unpriced" means the event isn't reflected in IV/premium yet
(no compensation) — not that quotes are missing. So earnings should be a **flagged,
homework-gated opportunity**, not a block.

## Boundary (the key decision)

A separate module — the analyzer does the **verdict**; Simmer keeps the **options**.

| Earnings analyzer (`earnings_engine.py`) | Simmer (unchanged core) |
|---|---|
| Input `(ticker, expiry)` | Owns chain, strike selection, premium, IV/skew |
| Output `{go, direction, confidence, rationale}` | Picks the structure/strikes leaning the analyzer's direction |
| News + sentiment → directional bias | Confirms premium is rich enough, scores it |
| **Never touches options data** | **Never forms fundamental bias** |

Decoupled like `strike_profiles` / `torque` already are: earnings is a plug-in Simmer
*calls*, not code tangled into the 5-min sweep.

## Dependencies

- **Reuse** — Alpaca (news) + Gemini (bias synthesis). No new keys for a news-driven bias.
- **Not needed** — options data (Simmer's side); Google Search (optional; the news feed covers most).
- **New, deferred** — a consensus estimates/guidance feed (EPS / revenue / guidance / price targets) for a real fundamental read. Phase 3 only.

Note: historical earnings-move is **not** a decision input (past move ≠ future move) —
at most a small cautionary label.

## Trigger

Simmer already resolves each expiry's earnings window (catalyst data). When an expiry
falls inside it, Simmer calls the analyzer instead of vetoing.

## Cache

Own table `simmer_earnings_bias`, keyed by `(symbol, earnings_date)`:
`{ direction, confidence, go, rationale, computed_at }`. Quarterly cadence — no reason
to touch the sweep. The card toggle **never deletes** this; it only decides whether the
verdict feeds the score, so flipping back is instant.

## Score hook

Earnings bias enters the composite score as a **weighted component** (like
sentiment / regime), **never a veto**:
- Aligned bias + rich IV/skew → lifts the score.
- Mixed / low-confidence signals → stays flagged, not sellable.
- Toggle **off** → recompute the score **without** the earnings component (weights
  renormalized); the cached analysis is untouched.

## Card UX

An **"Earnings" toggle auto-appears only** when the expiry is in an earnings window.
- **On** (default TBD) → bias folded into the score; the card shows the earnings read.
- **Off** → plain ex-earnings VRP score, so you can compare.
- Flipping back is instant (cached), no rerun.

## Division of labor (important — Simmer is NOT a direction forecaster)

Simmer decides **WHEN** a name is conditioned to sell premium, picks the **structure
from dealer positioning** (GEX/put-call walls, expected move, regime) and manages the
trade. It does **not** forecast fundamental direction, and it must not become a
signal/analysis terminal — that is edgelane-matrix's job.

So the *directional bias* for the earnings play comes from a **dedicated source**, not
Simmer inventing it:
- **The earnings analyzer** (this doc) supplies the news/sentiment/fundamental read.
- **Future option:** leverage **edgelane-matrix's bias engine** (its dealer-GEX +
  confidence scoring is more developed) rather than duplicating it in Simmer.

Simmer then maps that bias onto side + strikes + timing + guardrails. Symmetric by
design: **bullish read → bull put; bearish read (miss / guidance-cut narrative) →
bear call;** murky → no play.

## Phasing

1. **Analyzer module + cache table** — news + sentiment bias only. ✅ shipped.
2. **Integration — earnings as a folded factor** ✅ shipped. The sweep runs the
   `consider` view by default (bias folds, no hard earnings veto); a `no-go` /
   cold-cache read HOLDS the name back so it can't reach "ready". The card shows
   both verdicts (`consider` + `ignore` via `earnings_alt`) and the toggle is a
   pure display swap persisted in localStorage — no re-analysis. **Alerts: an
   earnings-window name that genuinely reaches "ready" (a consider+go run-up play)
   DOES fire an alert + email** (`process_alerts`), carrying the "close before the
   print" label so it's never mistaken for a hold-through. The `held_back` cap is
   the safety: a no-go / cold-cache read stays below the ready band, so only a
   deliberate go read can notify. (Earlier this path skipped all earnings-window
   names; that starved an earnings-heavy watchlist of alerts, so it was changed to
   alert-with-label.)
3. **Fundamentals (later)** — add consensus estimates / guidance / price targets for a
   stronger read (the `source` field distinguishes news vs news+fundamentals).
4. **Earnings run-up mode — "sell the drift, close before the print"** (future; the real
   payoff of the analyzer). A NEW structure/management mode, not a change to the
   hold-through veto (which stays correct):
   - **Enter T-1 / morning of earnings → force-close BEFORE the announcement.** Never
     hold the binary gap; the analyzer's *direction* is edge for the pre-print drift, not
     the post-print gap (sell-the-news makes the gap ~unedgeable).
   - **Bias source** = analyzer (or matrix), cross-checked against price/velocity/GEX so a
     bullish *narrative* on a chart already rolling over into the event does NOT fire.
   - **Guardrails (the whole point — avoid getting run over):**
     1. defined-risk vertical only (capped loss = width − credit; never naked);
     2. short strike far OTM, below support / the put-GEX wall AND outside the implied move;
     3. hard time-exit before the bell — no exceptions;
     4. stop while held (~2× credit, or short-strike delta breaching ~0.45);
     5. skip names already de-risking into the event (bullish fundamentals + bearish tape).
   - **Step-zero before any code:** the go/no-go **score threshold must be
     paper-calibrated** via the simmer_outcomes harness (win-rate/EV by score bucket) —
     no guessed cutoff to real money. It's a compound gate: analyzer confidence + `go` +
     signal consistency + Simmer's structural score, not one dial.

## Open decisions (before any build)

1. **Default toggle On or Off?** (Phase 2 shipped as On, on-demand only.)
2. Phase 4: **own earnings-bias analyzer, or call edgelane-matrix for the bias/structure?**
3. Phase 4: **the paper-calibration run** to set the score threshold — the prerequisite.
