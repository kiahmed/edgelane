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

### Button semantics (v4.7.3+, v4.7.15)

| Button | Refreshes | When to click | Provider cost |
|---|---|---|---|
| **Detect bias from Greeks** | Bias only (Stock-Quote + Greek-Exposures) | First analysis of a ticker, or after major market move | 2 calls |
| **↻ Re-detect bias from Greeks** *(label flips once bias is cached, v4.7.15)* | Bias only — **force-refetches** past the cache | When the tape clearly moved and you want a fresh GEX read on the same symbol/expiration. Prior to v4.7.15 the second click was a silent no-op. | 2 calls |
| **↻ Refresh chain & re-rank** *(formerly "Optimize")* | Chain only — fresh quotes/IV | Every few minutes while watching a setup; before placing a limit order | 1 call |
| **↻ Reload all data** *(in the status row)* | Both bias + chain | Ticker change, or "start fresh" | 3 calls |

Bias data is slow-moving (GEX walls stable for 30–60 min). Chain prices are tick-by-tick. Use the chain-only refresh frequently; bias refresh only when warranted.

The status row also shows the **last-fetched timestamp** for each cache so you can see at a glance how stale the data is.

**Live spot price (v4.7.13).** The Spot value at the top of the page refreshes immediately on every chain fetch, decoupled from the bias-analysis pipeline. Even if the narrative or strategies fail to update, the visible price reflects the fresh quote that was just pulled. The center cell of every Lookup grid (see below) is also anchored to this live mid.

**Heavy single-name chains.** For a handful of very active single-name tickers (popular semiconductors and leveraged momentum names), the first bias detection of a session can take 20–30 seconds while the full options chain is pulled in pieces. Any subsequent bias detection on the same symbol within a few minutes is effectively instant — the result is held in memory for re-use. Index ETFs and most mega-caps remain on the fast path and respond in a second or two. See **Provider notes & troubleshooting** at the bottom for details.

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

## Limit-Order Target Premium — width-scaled tiers (v4.7.16 / v4.7.17)

When the market isn't currently pricing a spread the way you'd want, the optimizer shows you **what premium it would take** to make it tradeable — at three different edge tiers — and tells you which ones are already satisfied at market vs which would need a patient GTC limit.

The earlier single-target version (constant +$5 EV) didn't scale: $5 on a $50-wide spread is 10% mispricing (fantasy), while $5 on a $500-wide spread is 1% (reasonable). Each tier is now expressed as a **fraction of spread width**, which is how institutional desks frame edge.

### The three tiers

| Tier | Edge target | Hint | Typical scenario |
|---|---|---|---|
| **modest** | 0.75% of width | "often fills" | Patient limit, frequently catchable on normal intraday noise. |
| **balanced** | 1.50% of width | "patient" | The everyday achievable target on minor dislocations. |
| **strong** | 3.00% of width | "on dislocations" | Only fills on real liquidity events, IV crush, or earnings unwinds. |

Each tier resolves to a **limit price** (sell-at-or-above for credit; buy-at-or-below for debit), and is tagged with one of three statuses:

- **✓ met** *(emerald)* — current live mid already gives you at least this edge. No waiting required; market beats the tier.
- **○ reachable** *(stone)* — feasible but not currently satisfied. Set a GTC limit at the target price to lock this edge in if the market comes to you.
- **infeasible** — the tier's premium is mathematically impossible on this spread (e.g., target credit > width, or target debit < $0.05). Hidden from the card.

### Scenario-aware headline (v4.7.17)

The single bold line at the top of the tier card adapts to where the live mid sits relative to the tiers, so you can read the situation in one glance without parsing every row:

| Live edge vs tiers | Headline color | Headline text |
|---|---|---|
| Beats **strong** (all three met) | emerald | `◎ Already at +$X edge — market beats every tier. Take at market.` |
| Beats **modest** or **balanced**, but not strong | dim emerald | `◎ Already at +$X edge — beats MODEST/BALANCED. Patient limit can unlock the next tier.` |
| Slight positive edge, below modest threshold | amber | `◎ Slight +$X edge — under the modest threshold. Limit at one of these locks meaningful edge.` |
| Live mid is sub-fair (negative edge) | stone | `◎ Sub-fair by $X — limit-only setup. Need the market to come to you.` |

The card also shows the **breakeven** (EV = 0) and the **live** premium below the headline, plus — if at least one tier is already met — an arrow pointing to the next reachable tier's target price.

### Math (under the hood)

**Credit spreads** (sell premium — bull_put, bear_call, condors, iron_butterfly):

```
breakeven_credit       = (1 − POP) × width                  # premium where EV = 0
target_credit(tier)    = breakeven + (tier_pct × width) / wall_factor
```

Place a **limit SELL** at `target_credit` or higher.

**Debit spreads** (pay premium — bull_call, bear_put, call/put butterflies):

```
breakeven_debit        = POP × width
target_debit(tier)     = breakeven − (tier_pct × width) / wall_factor
```

Place a **limit BUY** at `target_debit` or lower.

Wall factor is included so the target accounts for any wall penalty/boost already applied.

The **modest** tier is also used as the back-compat reference for composite scoring and the trade-ticket fallback price (so a "patience-limit ticket" snapped from the card reflects the realistic, often-fills tier — not the aggressive strong-tier number).

### Card actions (v4.7.7+)

Every candidate card has two small icon buttons at the bottom right:

| Icon | What | Behavior |
|---|---|---|
| **⎘** | Copy trade ticket | Copies a single-line trade ticket to clipboard. Format depends on the card's verdict: if `ev_adjusted ≥ 0` → `MARKET` ticket; if EV negative but limit-feasible → `LIMIT @ target GTC`; otherwise → `LIMIT @ breakeven` with a `[WARN]` flag appended. Shows `✓` for 1.5s on success. |
| **↗** | Push to broker | Disabled placeholder. Future hook into a configurable broker API (under user settings). Currently no-op. |

### Composite breakdown tooltip

Hover the composite hero number on any card to see the per-component breakdown — Center, EV, Badge, Liquidity, Limit feasibility, POP — and the totals. Useful when a card scores 55 vs 62 and you want to know which signal moved it.

### Workflow

1. Run Detect Bias → Refresh Chain. All candidates score, most will be sub-fair.
2. Pick the structure whose target tier is *closest* to current premium AND has good liquidity (high or mid).
3. Note the strikes, side, and the target limit price from the card.
4. In your broker, set up the same spread. Enter price = the card's target. Make it GTC.
5. If the market ever moves your way, you fill at your terms. If not, you didn't trade — which is also the right answer.

This is the rare disciplined options play: instead of taking what the market gives, you tell the market what you'd accept. Most days you don't fill. The days you do, you've extracted real edge from a market that briefly mispriced your structure.

---

## Tickets tab vs Lookup tab (v4.7.20)

Above the three candidate cards there's a tab switcher with two views:

| Tab | What it shows |
|---|---|
| **Tickets (3)** | The standard candidate cards — composite score, badges, wall verdict, limit-order tiers, copy/push actions. This is the default and matches everything described above. Unchanged by the Lookup work. |
| **Lookup** | A what-if projection grid for each candidate showing projected premium and close-now P&L across nearby spot prices and the next three hours of clock time. Useful for sanity-checking how the spread behaves if price drifts a dollar either way or if you hold another hour. |

Switching tabs is free — no provider calls — and doesn't change the candidates themselves. You can flip back and forth at will.

### Reading the Lookup grid

Each candidate gets its own 9-row heatmap with up to 12 columns:

- **Rows (vertical axis)** — hypothetical spot prices in steps around the current spot, spanning roughly ±2 expected moves (auto-clamped between 6% and 30%). The middle row is "spot is exactly where it is now"; rows above are higher prices, rows below are lower. The center row is bolded and tagged with a small emerald dot.
- **Columns (horizontal axis)** — time slices of **15 minutes each**, walking forward from now. The axis is capped at **3 hours** OR at expiration, whichever is sooner. Column labels are `now`, `+15m`, `+30m`, ..., `+1h`, `+1h15`, ..., up to `+2h45` or `exp`. (Pre-v4.7.21 this was a multi-day axis; the intraday rework matches how the grid is actually used — "what's my premium worth in 30 minutes if spot ticks to $X" — not multi-day theta planning.)
- **Cells** — the projected spread premium at that (spot, time) pair, shown as a signed dollar amount with `+` for credit-side and `−` for debit-side display, and color-coded by **close-now P&L** (see heatmap below). The center cell at (current spot, now) is anchored to the live mid exactly, with Black-Scholes sensitivity radiating outward from there.

### Lookup heatmap colors (v4.7.21)

Each cell's color encodes the **close-now P&L** of the spread at that spot/time pair, normalized to the spread's own max profit (for gains) or max loss (for losses). Seven discrete levels:

| Level | Normalized P&L range | Color |
|---|---|---|
| **+3** *(deep emerald, bold)* | ≥ +55% of max profit | `bg-emerald-700` |
| **+2** *(medium emerald)* | +25% to +55% of max profit | `bg-emerald-800` |
| **+1** *(faint emerald)* | +8% to +25% of max profit | `bg-emerald-900` |
| **0** *(stone)* | within ±8% of breakeven | `bg-stone-800` |
| **−1** *(faint rose)* | −8% to −25% of max loss | `bg-rose-900` |
| **−2** *(medium rose)* | −25% to −55% of max loss | `bg-rose-800` |
| **−3** *(deep rose, bold)* | ≥ −55% of max loss | `bg-rose-700` |

**Hover any cell** to see the exact P&L number per contract, e.g.
`Spot $182.50 · +30m → premium +$1.42 · close-now P&L +$58/contract`.

The "current spot" row is tagged with a green dot in the left margin, and the "now × current spot" cell is ringed in emerald — that's the anchor point where projection = live mid by construction.

### Assumptions baked into the projection

- European exercise (good for index, minor error for early-exercisable American single-name puts near dividends).
- Sticky-strike IV — each leg's IV stays attached to its strike as spot moves. Realistic for short-DTE intraday projections; less so for multi-day projections through a vol-changing event.
- Constant risk-free rate r = 5%.
- No dividends.
- Center cell is forced to match the live mid (`candidate.net_premium`); all other cells are BS-derived offsets from that anchor.

If a leg is missing IV data, the cell falls back to intrinsic value and the card shows `(some legs missing IV — intrinsic fallback)` in amber italics next to the live-premium line.

### EDGELANE wordmark (v4.7.19)

The header now leads with an embossed `EDGELANE` wordmark in emerald above the "Spread Optimizer" title. Cosmetic — it doesn't change any behavior. The version pill (`· OPTIONS OPTIMIZATION LAB · v4.7.x`) sits next to it.

---

## Provider notes & troubleshooting

Most users shouldn't have to think about provider mechanics. This section exists so that if you ever notice **MU taking ~25 seconds to load**, or a re-detect on a heavy symbol returning instantly, you know it's expected behavior, not a bug.

### Adaptive Greek-exposure fetch (v4.7.22 – v4.7.24)

The provider's aggregated `analyze_greek_exposures` endpoint is fast (5–15s) for liquid index and mega-cap symbols, but slow-to-failing on heavy single-name option chains. EdgeLane now adapts automatically:

- **Known-heavy symbols** (`MU`, `AMD`, `SMCI`, `COIN`, `PLTR`, `ARM`, `AVGO`, `MARA`, `MSTR`) skip the aggregated call entirely and fan out **parallel per-expiration requests**, then aggregate the result client-side. First fetch is 20–30s; cached afterwards.
- **All other symbols** try the aggregated call first. On timeout, HTTP 5xx, "Internal server error", "gateway", or "service unavailable", they fall back to the same chunked path transparently.
- Results are held in a **5-minute in-memory cache** keyed by `(symbol, num_expirations)`. Re-detecting bias on the same ticker within five minutes is effectively instant.
- The provider request timeout on the JSX side is **75 seconds** (with `AbortController` cancellation), up from the older default — heavy aggregated calls regularly take 40–60s server-side and need the headroom.

You'll see this in the browser console as `[Greek exposures] MU on denylist → chunked path immediately` or `[Greek exposures] cache hit for SPY (num_exp=3)`. Operationally invisible from the UI — the symbol just works, or it doesn't.

### Symptom → cause cheat sheet

| What you see | What's happening |
|---|---|
| MU/AMD/etc. takes 20–30s on first bias detect | Chunked per-expiration fetch is in flight. Normal. |
| Second bias detect on same symbol returns instantly | 5-minute cache hit. |
| "↻ Re-detect bias from Greeks" button label | Bias is cached. Clicking force-refetches past the cache (v4.7.15). |
| Spot price updates but bias narrative is stale | Spot decoupled from bias pipeline (v4.7.13). Click re-detect to refresh narrative. |
| Lookup cell says "intrinsic fallback" | At least one leg has no IV from provider; BS surface degrades to intrinsic for that cell. |
| Symbol fails after ~75s with timeout error | Even chunked fallback timed out. Try again in a minute; if persistent, the symbol may need to be added to the heavy-chain list. |
