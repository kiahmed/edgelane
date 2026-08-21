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

## Phasing

1. **Analyzer module + cache table** — news + sentiment bias only. Standalone, testable
   in isolation (no Simmer changes yet).
2. **Integration** — score hook + card toggle.
3. **Fundamentals (later)** — add consensus estimates / guidance for a stronger read.

## Open decisions (before any build)

1. **Default toggle On or Off?**
2. **Phase 1 news-only, or wait and include estimates?**
