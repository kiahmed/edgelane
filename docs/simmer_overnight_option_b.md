# Option B — use the Matrix engine to pick the play for an overnight mover

**Status: exploration / proposal for review (2026-10-12). NOT built.** Option A
(the premium-ratio decision analyzer, `docs/simmer.md` → "Overnight movers") is
built and tested. This is the alternative route the owner asked me to scope
overnight: instead of hand-rolling a premium-ratio heuristic, hand an RQ ticker
to the **Matrix engine** and let its calibrated composite scorer tell us the
best credit play and how good it is.

## TL;DR

**Feasible, and likely a richer answer than the heuristic — but it answers a
different question, so the best design uses BOTH.** Recommend making the overnight
analyzer's engine configurable (`premium` | `matrix` | `both`) at the
environment/deploy level, defaulting to Option A now and adding Matrix behind the
flag.

## Why it's feasible (confirmed in code)

Matrix exposes ONE symbol-parametrized entry point — it is not SPX/NDX-locked:

```python
# market/backend/app/engine.py
compute_engine_output(symbol, spot, chain_contracts, chosen_expiration,
                      chosen_dte, expected_move=None)
```

It:
- estimates `expected_move` from the chain's **ATM straddle** when not supplied
  (`_estimate_expected_move`), so it needs no index-specific EM feed;
- computes dealer **GEX bias signals** and **bilateral walls** from the chain's
  per-strike rows (`bias_engine.compute_bias_signals`, `walls.py`);
- runs `strategy_engine.generate_candidates(...)` for each structure and returns
  per-strategy best candidates, each with a **composite_score** (EV, structural
  health, liquidity, limit-order feasibility, POP) and a `composite_verdict`,
  plus the single **engine pick**.

Everything it needs — a normalized chain, spot, expiry/DTE — Simmer already
fetches for the overnight name (the premium tracker pulls the same chain). So the
call is essentially: `compute_engine_output(sym, spot, chain, exp, dte)` →
ranked plays + the pick + a calibrated score. The 56 parity tests already pin
that math.

## What each route actually answers

| | Option A — premium tracker | Option B — Matrix engine |
|---|---|---|
| Question | Is the overnight reaction **holding / building / fading**? (dynamic, time-evolving) | What is the **best structural credit play right now**, and **how good** (composite score)? (snapshot) |
| Output | sell_bear_call / sell_bull_put / wait / skip + premium trend | ranked strategies + strikes + engine pick + composite score + verdict |
| Strength | conviction & **timing** — is the move real or a fade | **strike/structure selection** + a calibrated quality number |
| Calibration | heuristic, unproven day one | the existing calibrated Matrix composite |

They're complementary, not redundant: Matrix picks the **structure + strikes +
quality**; the premium/narrative read tells you **whether now is the time** (is
the move holding or being rejected). A high Matrix composite on a name whose
overnight premium is *holding* its move is a much stronger signal than either
alone.

## Recommended design (if we build it)

1. Keep Option A as the **conviction/timing overlay** (holding/fading + the
   thesis).
2. Add Option B as the **strategy selector**: once open, for each overnight name
   call `compute_engine_output` on the live chain; store the engine pick +
   composite score alongside the overnight row; surface the pick in the Overnight
   tab when its composite clears a bar.
3. Combine into one verdict: *Matrix pick + composite, gated/weighted by the
   premium read* — e.g. only surface "take this play" when Matrix's composite is
   high AND the premium read isn't screaming fade.
4. **Config:** `OVERNIGHT_ANALYZER = premium | matrix | both` (env/deploy), so it
   is switchable without code changes, defaulting to `premium` (Option A, live
   now) and promoting to `both` after we see Matrix behave on single names.

## Caveats to validate before building

- **Single-name GEX is noisier than index GEX.** Matrix's bias/walls were tuned
  on SPX/NDX deep OI. Nasdaq-100 single names (what RQ surfaces) have real OI but
  thinner, event-distorted books overnight. Needs a look at wall quality on a few
  real movers.
- **Bias may lag the news.** Matrix derives direction from dealer GEX; an
  overnight news gap can run ahead of the GEX picture. The overnight direction
  (gap + premium read) is the better directional prior — we can pass it in rather
  than trust Matrix's own bias on day 1.
- **Market-hours only.** `compute_engine_output` wants a live chain; the 9:25
  baseline is pre-open (stale), so Option B runs post-open, same cadence as the
  premium refresh.
- **Product boundary.** Matrix is a separate product; calling its engine from
  Simmer is an internal function call (no cross-service hop), but we should keep
  the invocation behind the Simmer overnight module, not couple the two products'
  UIs.

## Effort estimate

Small-to-medium: a thin adapter in `simmer_overnight.py` that maps an overnight
name's chain → `compute_engine_output` → a stored pick/score, plus a verdict
merge and the config flag. No new math (reuses Matrix's). The UI already has the
Overnight tab to show it in.

## Ask for the morning

Decide: ship Option A as-is for tomorrow (done), and greenlight building Option B
behind the `OVERNIGHT_ANALYZER=both` flag as a fast follow? I'd want to eyeball
Matrix's walls/pick on 2–3 real RQ movers during market hours first to confirm
single-name quality before wiring it into the verdict.
