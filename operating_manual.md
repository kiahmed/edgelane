# EdgeLane Operating Manual

What every badge, label, and number on the page means — pulled directly from
the classifier code in `spread_optimizer_v4_7_html.jsx` (no docs drift).

---

## ⚡ Composite score — one number per candidate (v4.7.3+)

Every candidate gets a single **Composite score (0–100)** integrating EV, structural health, liquidity, limit-order feasibility, and POP into one number.

| Score | Verdict | Meaning |
|---|---|---|
| **≥ 60** *(emerald)* | **tradeable** | EV positive, structure healthy, liquidity workable. The engine pick. |
| **40 – 59** *(amber)* | **marginal** | No setup is clearly tradeable. Consider waiting on a limit-order fill or moving to a different expiration. |
| **< 40** *(rose)* | **do not trade** | No candidate has both EV edge and acceptable structure. Move on. |

A page-level banner above the cards shows the top composite score with the verdict color-coded so you can decide *whether to engage at all* before drilling into individual cards.

### Composite formula

```
score = 50  (center)
        + EV component        ∈ [0, +40]   = max(0, min(40, ev_adjusted × 2))
        + badge component     ∈ [−30, +15] = healthy +15, thin/directional −5,
                                              broken/capital_trap −30
        + liquidity component ∈ [−10, +10] = high +10, mid 0, low −10
        + limit feasibility   ∈ [0, +30]   = feasibility × (30 if EV<0, else 10)
                                              feasibility = 1 − |delta|/current
        + POP tiebreaker      ∈ [−5, +5]   = (POP − 50) / 10
```

Then clamped to [0, 100]. EV is already wall-adjusted via `ev_adjusted`, so the wall verdict is implicitly counted through EV without double-billing.

### Button semantics (v4.7.3+)

| Button | Refreshes | When to click | Atlas cost |
|---|---|---|---|
| **Detect bias from Greeks** | Bias only (Stock-Quote + Greek-Exposures) | First analysis of a ticker, or after major market move | 2 calls |
| **↻ Refresh chain & re-rank** *(formerly "Optimize")* | Chain only — fresh quotes/IV | Every few minutes while watching a setup; before placing a limit order | 1 call |
| **↻ Reload all data** *(in the status row)* | Both bias + chain | Ticker change, or "start fresh" | 3 calls |

Bias data is slow-moving (GEX walls stable for 30–60 min). Chain prices are tick-by-tick. Use the chain-only refresh frequently; bias refresh only when warranted.

The status row also shows the **last-fetched timestamp** for each cache so you can see at a glance how stale the data is.

### How card glow color is determined

The candidate card border + glow signals only two things at the page level (independent of badges):

| Card state | Glow | Meaning |
|---|---|---|
| ★ Engine Pick | **emerald** | This is the optimizer's top recommendation given your bias and inputs. |
| Health is `broken` | **strong rose** | Hard skip — the structure has a fatal flaw. |
| Anything else | none (stone border) | Inspect the badge + wall stripe for nuance. |

So a "non-glowing" card isn't bad — it just means it's not the engine pick and not structurally broken. The badge inside the card tells the rest of the story.

---

## Spread Health Badges

Computed by `classifyHealth(strategyType, netDelta, dte, maxLoss, maxProfit)`. The classifier branches on **credit vs debit** because the failure modes are different.

### For credit spreads (bull_put, bear_call, iron_condor, iron_butterfly)

The play is theta harvest — you want premium decay to deliver before price moves.

| Badge | Trigger | What it means |
|---|---|---|
| **✓ Healthy** *(emerald)* | Net Δ ∈ [0.10, 0.30], DTE > 5, max_loss ≤ 5× max_profit | Working zone — both theta and modest delta drift contribute; capital risk is bounded. |
| **⚠ Thin** *(amber)* | Net Δ < 0.10 | Theta is doing all the work. Needs days, not hours. Don't pair with short DTE. |
| **⚠ Directional** *(amber)* | Net Δ > 0.30 | Behaves more like a directional bet than premium harvest. Win/lose tracks price direction. |
| **⚠ Broken** *(rose)* | Net Δ < 0.05 AND DTE ≤ 5 | Gamma will dominate before theta delivers. Skip. |
| **⚠ Capital Trap** *(rose)* | max_loss > 5× max_profit | One loss = 5+ winners undone. Math doesn't survive a normal hit rate. |

### For debit spreads (bull_call, bear_put, call_butterfly, put_butterfly)

The play is directional — you pay premium up front and need price to move into the profit zone.

| Badge | Trigger | What it means |
|---|---|---|
| **✓ Healthy** *(emerald)* | Payoff/debit ratio ≥ 0.6, DTE ≥ 3 | Reasonable debit for the width and time; manage size by the debit paid. |
| **⚠ Thin** *(amber)* | Payoff/debit ratio < 0.6 | Mediocre return per dollar of debit. Needs a strong directional move to justify. |
| **⚠ Capital Trap** *(rose)* | Payoff/debit ratio < 0.30 | Paid too much premium relative to width — breakeven hard to reach even on a correct call. |
| **⚠ Broken** *(rose)* | DTE ≤ 2 | Not enough time for the directional move to play out before gamma dominates. |

The **Directional** badge does not apply to debit spreads (debits ARE directional by design).

---

## Liquidity Tiers

Shown as `high liq` / `mid liq` / `low liq`. Computed by `classifyLiquidity` from the **minimum** open interest across all legs.

| Tier | Trigger | What it means |
|---|---|---|
| **high** *(emerald)* | min OI > 200 | Bid/ask should be tight; you can usually fill at mid or one penny inside. |
| **mid** *(amber)* | 50 < min OI ≤ 200 | Workable but expect a few cents of slippage. Don't chase. |
| **low** *(rose)* | min OI ≤ 50 | Wide spread, partial fills, painful exits. Skip on multi-leg structures. |

---

## GEX Wall Verdicts

The wall stripe at the bottom of each card explains how the dominant gamma wall affects this specific spread. Computed by `computeWallPenalty(strategy, strikes, breakevens, wallStrike, wallStrength)`.

**Mental model:** walls are *attractors* — price tends to drift toward them. Whether that's good or bad depends on where you need price to go.

### Per strategy

| Strategy | Wall in profit zone | Wall above profit zone | Wall below profit zone |
|---|---|---|---|
| **bull_put** *(credit)* | bad — pin risk on short put | **good** — wall above as upward support | bad — no support; price can drift through |
| **bear_call** *(credit)* | bad — pin risk on short call | bad — drags price up into loss | **good** — wall below as resistance cap |
| **iron_condor** *(credit)* | **good** if centered between shorts — ideal pin | bad — drags price out | bad — drags price out |
| **iron_butterfly** *(credit)* | **good** if AT center strike | bad if off-center | bad if off-center |
| **bull_call** *(debit)* | **good** — pulls price into profit | **good** — pulls past max-profit cap | **bad** — pulls AWAY from breakeven |
| **bear_put** *(debit)* | **good** — pulls price into profit | **bad** — pulls AWAY from breakeven (upward) | **good** — pulls past max-profit cap |
| **call/put butterfly** *(debit)* | **good** if AT center | bad if off-center | bad if off-center |

### Verdict colors

| Verdict | Factor (multiplied into EV) |
|---|---|
| **good** *(emerald)* | 1.0 — wall helps the structure |
| **neutral** | 1.0 — wall doesn't materially help or hurt |
| **warn** *(amber)* | 0.7–0.95 — wall is close to working against you |
| **bad** *(rose)* | 0.5–0.6 — wall actively hurts the structure |

**Key correction (v4.7.1):** the bull_call and bear_put wall logic was previously inverted — calling "wall in profit zone" a barrier (bad) and "wall below breakeven" supportive (good) for bull_call. Both now match the attractor model used everywhere else: a wall in the profit zone *pulls* price into profit (good); a wall below breakeven on a bull_call *pulls* price away from your target (bad).

---

## Bias Labels & Directional Score

The bias card at the top shows your dealer-positioning read. The score is in `[-100, +100]`, computed deterministically in JS by `_computeBiasSignals` (v4.7+).

| Bias label | Score range | Strategy intent |
|---|---|---|
| **Bullish** | ≥ +60 | Strong upward setup. Favors bull_put or bull_call. |
| **Mild Bullish** | +20 to +60 | Slow drift up. bull_put workhorse; iron_condor or call_butterfly fit. |
| **Neutral / Range-bound** | −20 to +20 | Range-bound flat tape. iron_condor or iron_butterfly maximize premium. |
| **Mild Bearish** | −60 to −20 | Slow drift down. bear_call workhorse; iron_condor or put_butterfly fit. |
| **Bearish** | ≤ −60 | Strong downward setup. Favors bear_call or bear_put. |

### Score formula

```
score = (spot − wall_strike) / wall_strike × 200
       × wall_strength_multiplier  (1.5 / 1.0 / 0.5)
       × gamma_regime_multiplier   (0.7 if pinning, 1.2 if amplifying)
       + dex_skew_contribution     (±10 max)
```

Clamped to [−100, +100].

## Wall Strength

| Strength | Trigger (#1 |GEX| vs #2) | Meaning |
|---|---|---|
| **high** | > 2× | Dominant; pin behavior reliable. |
| **medium** | 1.2× to 2× | Soft level; other strikes are close in magnitude. |
| **low** | < 1.2× | No real wall; don't lean on wall-aware logic. |

---

## Confidence

| Confidence | Trigger |
|---|---|
| **high** | Wall strength is high AND |score| > 30 AND DEX direction agrees with wall-position direction |
| **medium** | Wall present, score has clear sign, signals not fully aligned |
| **low** | Wall is weak OR |score| < 15 |

Low confidence means the structured signals aren't telling a coherent story. Fade or reduce size.

---

## Strategies

| Key | Name | Type | Fits bias |
|---|---|---|---|
| `bull_put` | Bull Put Spread | credit | bullish, mild_bullish, neutral |
| `bear_call` | Bear Call Spread | credit | bearish, mild_bearish, neutral |
| `iron_condor` | Iron Condor | credit | neutral, mild_bullish, mild_bearish |
| `iron_butterfly` | Iron Butterfly | credit | neutral |
| `bull_call` | Bull Call Spread | debit | bullish, mild_bullish |
| `bear_put` | Bear Put Spread | debit | bearish, mild_bearish |
| `call_butterfly` | Call Butterfly | debit | neutral, mild_bullish |
| `put_butterfly` | Put Butterfly | debit | neutral, mild_bearish |

---

## Width Preferences

| Pref | Factor | Behavior |
|---|---|---|
| **Tight** | 1.0× expected move base | Min credit, min capital. Theta-only — needs time. |
| **Balanced** *(recommended)* | 1.5× | Both delta and theta contribute. Default working zone. |
| **Generous** | 2.5× | More capital, smoother P&L curve, lower max profit but lower max loss. |

Expected-move base scales by DTE: 0.4× at DTE 0, 1.0× at DTE 1–7, 1.5× at DTE > 7.

---

## Limit-Order Target Premium

When the market doesn't currently price a spread at positive EV, the optimizer shows you **the exact premium it would take** to make it tradeable. Set a GTC limit order at that price; if the market ever drifts there, you fill on your terms.

Each card with a feasible target shows:

```
◎ LIMIT ORDER TARGET
Sell limit @ $1.95 ≥ for EV ≥ +$5     (breakeven $1.65)
Currently quoted at $1.23; needs $0.72 more credit for +$5 EV.
```

### Math (under the hood)

**Credit spreads** (sell premium — bull_put, bear_call, condors, iron_butterfly):

```
breakeven_credit = (1 − POP) × width                # premium where EV = 0
target_credit    = breakeven + targetEV / wall_factor
```

Place a **limit SELL** at `target_credit` or higher. Higher fills only.

**Debit spreads** (pay premium — bull_call, bear_put, call/put butterflies):

```
breakeven_debit = POP × width
target_debit    = breakeven − targetEV / wall_factor
```

Place a **limit BUY** at `target_debit` or lower. Lower fills only.

Default target EV is **$5**. Wall factor is included so the target accounts for any wall penalty/boost already applied.

### Feasibility

The card only shows the target if it's mathematically achievable:
- Credit: target < width (can't collect more than the full width as credit)
- Debit: target > 0 (can't pay negative premium)

When infeasible (rare — usually means the structure is fundamentally unrewardable for your target EV), no panel shows. Adjust width pref or try a different strategy.

The panel highlights **green** when the target is realistic (close to current price); **grey** when the target is far from current — you'd need a substantial price move to fill, more "patient limit" than "tradeable now".

### Card actions (v4.7.7+)

Every candidate card has two small icon buttons at the bottom right:

| Icon | What | Behavior |
|---|---|---|
| **⎘** | Copy trade ticket | Copies a single-line trade ticket to clipboard. Format depends on the card's verdict: if `ev_adjusted ≥ 0` → `MARKET` ticket; if EV negative but limit-feasible → `LIMIT @ target GTC`; otherwise → `LIMIT @ breakeven` with a `[WARN]` flag appended. Shows `✓` for 1.5s on success. |
| **↗** | Push to broker | Disabled placeholder. Future hook into a configurable broker API (under user settings). Currently no-op. |

### Composite breakdown tooltip

Hover the composite hero number on any card to see the per-component breakdown — Center, EV, Badge, Liquidity, Limit feasibility, POP — and the totals. Useful when a card scores 55 vs 62 and you want to know which signal moved it.

### Workflow

1. Run Detect Bias → Optimize. All candidates score, most are likely negative EV.
2. Pick the structure whose target is *closest* to current premium AND has good liquidity (high or mid).
3. Note the strikes, side, and the target limit price from the card.
4. In your broker, set up the same spread. Enter price = the card's target. Make it GTC.
5. If the market ever moves your way, you fill at your terms. If not, you didn't trade — which is also the right answer.

This is the rare disciplined options play: instead of taking what the market gives, you tell the market what you'd accept. Most days you don't fill. The days you do, you've extracted real edge from a market that briefly mispriced your structure.

---

## Engine Pick / Selected / Recommended / Fits Bias / Off-Bias

| State | Visual | Meaning |
|---|---|---|
| **★ Engine Pick** | emerald glow + ★ badge | Optimizer's #1 recommendation given current bias. |
| **Selected** | white pill | The strategy you've explicitly clicked. |
| **Recommended** | emerald | In `recommended_strategies` top 3 but not the #1. |
| **Fits bias** | white border | `.fits` array contains current bias label, but not in top 3. |
| **Off-bias** | dimmed | Doesn't fit current bias. Clicking still works. |

---

## When candidates show "no candidates"

In likelihood order:
1. All strikes filtered out for `bid > 0` rule (untradeable as short legs).
2. Width-prefs too tight — flip to *Generous* and re-run.
3. Strategy doesn't fit bias (e.g. bull_call on bearish bias = no scoreable setups). Click ★ Engine Pick.
4. Chain didn't load — check Network tab; Atlas error or proxy down means contract list is empty.

---

## When EV is negative across ALL candidates

This means there's no edge in the current setup. Diagnose in this order:

1. **Strategy mismatch** — confirm ★ Engine Pick. Forcing the wrong-direction structure produces uniformly bad EV.
2. **Wall fighting** — if all wall verdicts are `bad`/`warn`, the GEX wall is positioned against this strategy regardless of strikes. Try the opposite-direction strategy.
3. **Width/EM misaligned** — toggle width pref Tight ↔ Generous. If neither helps, this expiration is genuinely untradeable.

If after those three nothing turns positive, **don't trade**. Try the next expiration, a different ticker, or wait. Sitting out negative-EV setups is what separates traders who survive.
