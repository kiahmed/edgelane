# EdgeLane Operating Manual

What every badge, label, and number on the page means, and how to use the tool effectively.

---

## Sign in & user session

EdgeLane gates on a sign-in before showing the optimizer. The dialog appears the first time you load the app in a tab; once signed in, the session lives in your browser's tab memory and is restored automatically on reload (until you close the tab).

### Sign-in options

The dialog offers two paths:

- **Social sign-in** — single-click buttons for Google, Microsoft, X, Facebook, Instagram, LinkedIn. **In this build the OAuth flows are simulated** — buttons synthesize a believable user object client-side; no real identity provider is contacted. A future release will swap them for real OAuth once the backend exists.
- **Email + password** — full sign-up flow with confirm-password, plus a "Forgot password?" link that prompts for your email and then lets you set a new password locally.

Accounts created via email/password are stored in your browser's session memory, hashed with SHA-256. They clear when you close the tab. Not a substitute for a real backend — adequate for prototyping.

### Profile menu (top-right)

Once signed in, an emerald avatar circle appears in the header's top-right corner showing your initial. Clicking it opens a dropdown with:

- **Settings** — opens the broker-connections page (see below).
- **Sign out** — clears your session and returns to the sign-in dialog.

---

## Broker connections (Settings page)

EdgeLane separates two flows: the **market-data** plumbing the app itself uses to fetch quotes/chains/greeks (configured once at deploy time by the operator) and **your own broker connection** used to push trades to your account. The settings page is where you configure the second.

### Providers

| Provider | Status | Required fields | Notes |
|---|---|---|---|
| **Tradier** | Live | Access token, Environment (sandbox / production) | Full multi-leg option order support. Sandbox accepts paper-trading fills against simulated quotes. Get a token at https://dash.tradier.com/settings/api (production) or https://developer.tradier.com (sandbox). |
| **WeBull** | Stub | App key, App secret | UI shell only. WeBull's official OpenAPI requires HMAC request signing which leaks the secret if done in-browser. Lights up once a signing proxy server is in place. |

### Connection lifecycle

Each connection has a label (free-form, e.g. *"Tradier sandbox · personal"*), a provider, and the provider-specific credentials. Multiple connections can coexist; **only one is active at a time**. The active one is the one your push-to-broker dialog uses for order routing.

From any connection card you can:

- **Test connection** — runs `GET /user/profile` against the provider and reports latency, account number, and option-trading level (Tradier needs level ≥ 3 for credit spreads, level 4+ for iron condors).
- **Set active** — promote this connection to the active one. Marked with an emerald "Active" pill.
- **Edit** — change label or credentials.
- **Remove** — drop the connection. If it was active, the next remaining connection becomes active automatically.

A green ✓ next to a connection means the most recent test succeeded; a red ✗ means it failed (with the provider's error message). Test results timestamp themselves so you can see how stale the health read is.

### Storage & warning

All credentials are stored in `sessionStorage`. **They clear when you close the tab.** That's deliberate — there's no encrypted backend yet, so persisting tokens permanently would be reckless. Expect to re-enter your token at the start of each session for now. A small amber line at the bottom of the Settings page reminds you of this.

---

## Pushing trades to your broker

The **↗** button on each candidate card opens the push-to-broker dialog. It's no longer a placeholder — when an active broker connection exists, the button is enabled.

### What the dialog shows

- **Title row** — `{symbol} · {Conservative|Balanced|Aggressive} · {structure}` with the leg summary below (e.g. `BUY put 535 · SELL put 540 · SELL call 555 · BUY call 560`).
- **Connection-status banner** — checks your active broker connection on open:
  - **✓ Healthy** *(emerald)* — green light. The Execute button is enabled.
  - **✗ Unhealthy** *(rose)* — the connection's `/user/profile` call failed, or there is no active connection. Execute stays disabled. Open Settings to fix it.
- **Order type toggle** — Market or Limit:
  - **Market** is pre-selected when the live mid already meets fair value (live edge ≥ 0). The price field is hidden because there's nothing to set.
  - **Limit** is pre-selected when the live mid is sub-fair. The price field is pre-filled with the candidate's "modest tier" target — the same one the Tickets card shows as the easiest reachable edge.
  - You can always switch and override.
- **Limit price** — fully editable when in Limit mode. Suggested price, breakeven, and live mid are shown directly under the field so you can compare without leaving the dialog.
- **Time-in-force** — Day or GTC.

### What "Execute" does

For Tradier, the dialog does a **preview then live** flow:

1. Resolves your account number (cached after the first successful test).
2. POSTs the order with `preview=true` — Tradier validates it (margin, option level, strike spacing) without filling. If preview fails, you see the rejection reason inline and nothing is submitted.
3. If preview passes, POSTs the same payload live. Returns the order ID and initial status.

You'll see a confirmation card with the order ID and status. Track the fill in your broker terminal — the dialog doesn't poll. For WeBull, Execute throws `WeBull integration not yet implemented` until the signing-proxy backend ships.

### When Execute is grayed out

Three reasons:

- **No active broker connection** — Settings is empty or you haven't promoted one to active.
- **Connection healthcheck failed** — token expired, sandbox/production mismatch, network issue. The banner shows the provider's exact error.
- **Submitting** — pressed once, request in flight. Resolves to either a success card or an error inline.

---

## Page layout at a glance

The screen flows roughly from setup at the top to analysis below:

1. **Header row** — title, status pills (bias cached / chain cached / last fetched), a **Reload all data** button, and your **profile avatar** in the top-right.
2. **Inputs panel** — symbol, expiration, strategy buttons, target delta, wing width preference, and the green **Detect bias from Greeks** button.
3. **Bias card** (appears after detect) — the dealer-positioning read: bias label, directional score, recommended strategies, and an expandable narrative.
4. **Stats strip** — Spot price, expected move, ATM IV, DTE, current width preference, and the GEX wall location.
5. **Composite tradeability banner** — a single sentence telling you whether the top pick is tradeable right now.
6. **Tabs** — switch between **Tickets** (the three candidate cards) and **Lookup** (what-if projection grids).
7. **Three candidate cards** or **three Lookup grids**, depending on which tab.

---

## Inputs — what you tell the tool

### Symbol & expiration

Type the underlying ticker, pick the expiration date. Changing either clears the bias and chain caches for that combination.

### Strategy

Eight choices. Each is a multi-leg structure with a specific bias fit:

| Strategy | Type | Fits bias |
|---|---|---|
| Bull Put Spread | credit | bullish, mild bullish, neutral |
| Bear Call Spread | credit | bearish, mild bearish, neutral |
| Iron Condor | credit | neutral, mild bullish, mild bearish |
| Iron Butterfly | credit | neutral |
| Bull Call Spread | debit | bullish, mild bullish |
| Bear Put Spread | debit | bearish, mild bearish |
| Call Butterfly | debit | neutral, mild bullish |
| Put Butterfly | debit | neutral, mild bearish |

After bias is detected, recommended strategies are highlighted with a star and the engine pre-selects the best fit.

### Target delta

The short-strike delta the optimizer hunts for. 0.25 – 0.30 is the typical sweet spot for credit spreads; higher deltas trade more directionally.

### Wing width preference

| Pref | Factor | Behavior |
|---|---|---|
| **Tight** | 1.0× expected move | Min credit, min capital. Theta-only — needs time. |
| **Balanced** *(default)* | 1.5× | Both delta and theta contribute. Working zone. |
| **Generous** | 2.5× | More capital, smoother P&L, lower max profit, lower max loss. |

The base width scales by DTE: 0.4× at DTE 0, 1.0× at DTE 1 – 7, 1.5× at DTE > 7.

---

## Bias card — what the dealer book is saying

### Bias labels & directional score

The score is in **[−100, +100]**:

| Label | Score range | Strategy intent |
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

### Wall strength

Tells you how much to trust the wall-aware logic:

| Strength | Trigger (#1 |GEX| vs #2) | Meaning |
|---|---|---|
| **high** | > 2× | Dominant; pin behavior reliable. |
| **medium** | 1.2× to 2× | Soft level; other strikes are close in magnitude. |
| **low** | < 1.2× | No real wall; don't lean on wall-aware logic. |

### Confidence

| Confidence | Trigger |
|---|---|
| **high** | Wall strength is high AND |score| > 30 AND DEX direction agrees with wall-position direction |
| **medium** | Wall present, score has clear sign, signals not fully aligned |
| **low** | Wall is weak OR |score| < 15 |

Low confidence means the structured signals aren't telling a coherent story. Fade or reduce size.

### Narrative & signal panels

Below the score gauge, an expandable section shows a one-paragraph plain-English summary plus four short signal sentences covering dealer DEX skew, the GEX wall's role, gamma regime, and where spot sits relative to the wall. Click the chevron to toggle; the default can be set in your config.

---

## Stats strip — the at-a-glance status

Between the bias card and the candidate area:

| Field | Meaning |
|---|---|
| **Spot** | Current underlying price. Updates immediately every time the chain is refreshed, even if the bias narrative hasn't finished updating. |
| **Exp Move** | Expected move to expiration, in dollars and percent. Derived from ATM straddle premium. |
| **ATM IV** | At-the-money implied volatility for this expiration. |
| **DTE** | Days to expiration. |
| **Width Pref** | The currently selected width preference (Tight / Balanced / Generous). |
| **GEX Wall** | Dominant dealer-gamma strike and its strength tag. Shows "—" if no wall data. |

A short "Width Logic" line below explains how the width factor was computed for the current DTE.

---

## Composite tradeability banner

A single bold sentence above the candidate cards, color-coded:

| Composite of top card | Banner color | Verdict |
|---|---|---|
| **≥ 60** | emerald | tradeable — engine pick is workable at market |
| **40 – 59** | amber | marginal — wait for a fill or move on |
| **< 40** | rose | do not trade |

The banner is your "should I even bother engaging?" filter. If it's rose, skip the rest.

---

## Tabs — Tickets vs Lookup

Right above the cards there's a two-tab switcher:

| Tab | What it shows |
|---|---|
| **Tickets (3)** | The standard candidate cards — composite score, badges, wall verdict, limit-order tiers, copy/push actions. Default view. |
| **Lookup** | A what-if projection grid for each candidate showing projected premium and close-now P&L across nearby spot prices and the next three hours. |

Switching tabs is free — no provider calls — and doesn't change the candidates. Flip between them any time.

---

## Tickets tab — the three candidate cards

Three cards side-by-side: **Conservative**, **Balanced**, **Aggressive**. Each is a variant of the same strategy at different delta/width tradeoffs.

### Card glow color

The card border + glow signals two page-level states:

| Card state | Glow | Meaning |
|---|---|---|
| ★ Engine Pick | **emerald** | The optimizer's top recommendation. |
| Health is `broken` | **strong rose** | Hard skip — structural flaw. |
| Anything else | none | Inspect the badge + wall stripe for nuance. |

A non-glowing card isn't bad — it just isn't the engine pick and isn't broken.

### Liquidity badge

Top right of every card, based on the minimum open interest across all legs:

| Tier | Trigger | What it means |
|---|---|---|
| **high** *(emerald)* | min OI > 200 | Bid/ask should be tight; fill at mid or one penny inside. |
| **mid** *(amber)* | 50 < min OI ≤ 200 | Workable but expect a few cents of slippage. |
| **low** *(rose)* | min OI ≤ 50 | Wide spread, partial fills, painful exits. Skip on multi-leg. |

### Health badge

A colored chip with a one-line explanation. The classifier branches on whether the spread is selling premium (credit) or paying premium (debit), since the failure modes differ.

**Credit spreads** (bull_put, bear_call, iron_condor, iron_butterfly) — the play is theta harvest:

| Badge | Trigger | What it means |
|---|---|---|
| **✓ Healthy** *(emerald)* | Net Δ ∈ [0.10, 0.30], DTE > 5, max_loss ≤ 5× max_profit | Working zone — both theta and modest delta drift contribute; capital risk is bounded. |
| **⚠ Thin** *(amber)* | Net Δ < 0.10 | Theta is doing all the work. Needs days, not hours. |
| **⚠ Directional** *(amber)* | Net Δ > 0.30 | Behaves more like a directional bet than premium harvest. |
| **⚠ Broken** *(rose)* | Net Δ < 0.05 AND DTE ≤ 5 | Gamma will dominate before theta delivers. Skip. |
| **⚠ Capital Trap** *(rose)* | max_loss > 5× max_profit | One loss undoes 5+ winners. Math doesn't survive normal hit rate. |

**Debit spreads** (bull_call, bear_put, call_butterfly, put_butterfly) — the play is directional:

| Badge | Trigger | What it means |
|---|---|---|
| **✓ Healthy** *(emerald)* | Payoff/debit ratio ≥ 0.6, DTE ≥ 3 | Reasonable debit for the width and time. |
| **⚠ Thin** *(amber)* | Payoff/debit ratio < 0.6 | Mediocre return per dollar; needs a strong directional move. |
| **⚠ Capital Trap** *(rose)* | Payoff/debit ratio < 0.30 | Paid too much premium; breakeven hard to reach even on a correct call. |
| **⚠ Broken** *(rose)* | DTE ≤ 2 | Not enough time for the move to play out before gamma dominates. |

The **Directional** badge does not apply to debit spreads (debits are directional by design).

### Composite score

One number per card, 0 – 100, integrating EV, structural health, liquidity, limit-order feasibility, and POP:

| Score | Verdict | Meaning |
|---|---|---|
| **≥ 60** *(emerald)* | tradeable | EV positive, structure healthy, liquidity workable. |
| **40 – 59** *(amber)* | marginal | No setup is clearly tradeable. Consider waiting on a limit-order fill or moving to a different expiration. |
| **< 40** *(rose)* | do not trade | No EV edge and acceptable structure. Move on. |

The math:
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

Clamped to [0, 100]. EV is already wall-adjusted, so the wall verdict is implicitly counted through EV without double-billing.

**Hover the composite number** on any card to see the per-component breakdown — useful when one card scores 55 and another 62 and you want to know which signal moved it.

### Credit / Debit number + POP

Two big stats below the health badge. The premium (credit for credit spreads, debit for debit spreads) and the probability-of-profit estimate.

### GEX wall stripe

A colored stripe near the bottom of each card explains how the dominant wall affects this specific spread. Walls are *attractors* — price drifts toward them. Whether that's good or bad depends on where you need price to go.

| Strategy | Wall in profit zone | Wall above profit zone | Wall below profit zone |
|---|---|---|---|
| **bull_put** *(credit)* | bad — pin risk on short put | **good** — wall above as upward support | bad — no support; price can drift through |
| **bear_call** *(credit)* | bad — pin risk on short call | bad — drags price up into loss | **good** — wall below as resistance cap |
| **iron_condor** *(credit)* | **good** if centered between shorts — ideal pin | bad — drags price out | bad — drags price out |
| **iron_butterfly** *(credit)* | **good** if AT center strike | bad if off-center | bad if off-center |
| **bull_call** *(debit)* | **good** — pulls price into profit | **good** — pulls past max-profit cap | **bad** — pulls AWAY from breakeven |
| **bear_put** *(debit)* | **good** — pulls price into profit | **bad** — pulls AWAY from breakeven (upward) | **good** — pulls past max-profit cap |
| **call/put butterfly** *(debit)* | **good** if AT center | bad if off-center | bad if off-center |

Verdict color tells you how much it helps or hurts:

| Verdict | EV multiplier |
|---|---|
| **good** *(emerald)* | 1.0 — wall helps the structure |
| **neutral** | 1.0 — wall doesn't materially help or hurt |
| **warn** *(amber)* | 0.7 – 0.95 — wall is close to working against you |
| **bad** *(rose)* | 0.5 – 0.6 — wall actively hurts the structure |

### Limit-order target premium

When the market isn't pricing a spread the way you'd want, this section shows **what premium it would take** to make it tradeable at three different edge tiers, and tells you which ones are already satisfied at market vs. which would need a patient GTC limit.

#### The three tiers

| Tier | Edge target | Hint | Typical scenario |
|---|---|---|---|
| **modest** | 0.75% of width | "often fills" | Patient limit, frequently catchable on normal intraday noise. |
| **balanced** | 1.50% of width | "patient" | Achievable on minor dislocations. |
| **strong** | 3.00% of width | "on dislocations" | Only fills on real liquidity events, IV crush, or earnings unwinds. |

Each tier resolves to a **limit price** (sell-at-or-above for credit; buy-at-or-below for debit), and is tagged:

- **✓ met** *(emerald)* — current live mid already gives you at least this edge. No waiting required.
- **○ reachable** *(stone)* — feasible but not currently satisfied. Set a GTC limit at the target price to lock this edge in if the market comes to you.
- **infeasible** — the tier's premium is mathematically impossible on this spread. Hidden from the card.

#### Scenario-aware headline

The single bold line at the top of the tier card adapts:

| Live edge vs tiers | Headline color | Headline text |
|---|---|---|
| Beats **strong** (all three met) | emerald | `◎ Already at +$X edge — market beats every tier. Take at market.` |
| Beats **modest** or **balanced**, but not strong | dim emerald | `◎ Already at +$X edge — beats MODEST/BALANCED. Patient limit can unlock the next tier.` |
| Slight positive edge, below modest threshold | amber | `◎ Slight +$X edge — under the modest threshold. Limit at one of these locks meaningful edge.` |
| Live mid is sub-fair (negative edge) | stone | `◎ Sub-fair by $X — limit-only setup. Need the market to come to you.` |

The card also shows the **breakeven** (EV = 0) and the **live** premium below the headline, plus — if at least one tier is already met — an arrow pointing to the next reachable tier's target price.

#### Reading the headline in plain English

The fundamental rule for credit spreads (everything that sells premium): **the more credit you collect, the better the trade**. The breakeven price is the credit at which expected value is exactly zero. Below it, you're being underpaid for the risk. Above it, you have edge.

For debit spreads (everything that buys premium), it's the mirror image: **the less debit you pay, the better the trade**. Pay less than breakeven and you have edge.

| Headline | What it means in practice |
|---|---|
| `Already at +$X edge — market beats every tier. Take at market.` | Sell-to-open (or buy-to-open) at market right now — you're already collecting more credit (or paying less debit) than even the strong-tier target. |
| `Already at +$X edge — beats MODEST/BALANCED.` | You're getting paid more than fair, but not as much as you could. Taking it now is fine. A limit slightly better than current to chase a stronger fill is also fine. |
| `Slight +$X edge — under the modest threshold.` | Mathematically positive but thin. Taking it at market works; a small patient limit gets a meaningfully better fill. |
| `Sub-fair by $X — limit-only setup.` | Market is paying less than the trade is worth. Taking it at market is a math-loser. The trade only works if the market widens (credit) or narrows (debit) into positive territory. |

#### Practical reality check — when to walk away

The "sub-fair, limit-only" verdict isn't always worth waiting for. Sanity-check the *size* of the gap before parking a GTC order:

```
◎ Sub-fair by $39.19 — limit-only setup. Need the market to come to you.
breakeven (EV=0): $70.79 · live: $31.60
```

Read: market pays $31.60 in credit now; for this IC to break even on EV, it needs to pay $70.79. That's a **2.2× jump in collected credit**. That requires a real volatility spike, not a normal day.

A rough triage:

| Sub-fair gap as % of breakeven | Realistic to wait? |
|---|---|
| < 10% (e.g., breakeven $70, live $63) | Yes — normal intraday noise closes this in a session |
| 10 – 30% | Maybe — needs a meaningful but not extreme move; might fill in a day or two |
| 30 – 60% | Long shot — needs a real catalyst; weeks |
| > 60% | Probably not — structurally mispriced for your side; skip |

When in doubt, move on — there's always another setup tomorrow.

#### Math behind the tiers

**Credit spreads** (sell premium):
```
breakeven_credit      = (1 − POP) × width                  # premium where EV = 0
target_credit(tier)   = breakeven + (tier_pct × width) / wall_factor
```
Place a **limit SELL** at `target_credit` or higher.

**Debit spreads** (pay premium):
```
breakeven_debit       = POP × width
target_debit(tier)    = breakeven − (tier_pct × width) / wall_factor
```
Place a **limit BUY** at `target_debit` or lower.

Wall factor is included so the target accounts for any wall penalty or boost already applied. The **modest** tier feeds the composite score's limit-feasibility component and acts as the trade-ticket fallback price.

### Card actions

Two small icon buttons at the bottom right of each card:

| Icon | What | Behavior |
|---|---|---|
| **⎘** | Copy trade ticket | Copies a single-line trade ticket to clipboard. If EV ≥ 0 → MARKET ticket; if EV negative but limit-feasible → LIMIT @ target GTC; otherwise → LIMIT @ breakeven with a `[WARN]` flag appended. Shows `✓` for 1.5s on success. |
| **↗** | Push to broker | Opens the [push-to-broker dialog](#pushing-trades-to-your-broker) pre-filled with this candidate. Enabled when you have an active broker connection under Settings; otherwise tooltip says *"No active broker connection — add one under Settings to enable"*. |

### Rationale line

A short italic sentence at the bottom of each card explaining why the optimizer picked these specific strikes — typically references target delta, width factor, and any wall positioning.

### 3-lens positioning strip (when present)

Most days you'll never see this strip. When you do, it sits just below the GEX wall verdict on a card and reads something like:

`⚠ lenses diverge · GEX $235 · VEX $232 (pull below) · TEX $228 (pull below)`

It only appears when the three dealer-hedging lenses point at meaningfully different strikes — see [VEX / TEX lenses](#vex--tex-lenses--going-beyond-gex) for what to do when it shows up.

---

## Lookup tab — what-if projections

A 9-row × up-to-12-column heatmap per candidate showing projected premium and close-now P&L across nearby spot prices and the next few hours.

### What it's for

Use it to answer questions like:

- *"If NVDA ticks up $2 in 30 minutes, what's my spread worth?"* — Row two steps above center, column `+30m`. The number is the projected premium; the color tells you whether you'd be in profit or loss.
- *"How much theta do I pick up if I hold another hour at current price?"* — Center row, walk right one column per 15 minutes. Watch how the color drifts.
- *"If price runs to my short strike, am I underwater enough to want to roll?"* — Find the row at that price level. Deep red means most of max loss is gone.
- *"Where does this flip from profitable to losing if I close right now?"* — The band where color transitions from emerald through stone to rose is your close-now breakeven zone.

It's not a forecast — it's a "if this happens, here's what your trade looks like" table.

### Reading the grid

- **Rows** — hypothetical spot prices spanning roughly ±2 expected moves around current spot (auto-clamped between 6% and 30%). The center row is bolded and tagged with a small emerald dot.
- **Columns** — 15-minute time slices walking forward from now. Capped at 3 hours or expiration, whichever is sooner. Labels: `now`, `+15m`, `+30m`, ..., up to `+2h45` or `exp`.
- **Cells** — projected premium at that (spot, time) pair, signed `+` for credit-side and `−` for debit-side. Color-coded by close-now P&L.

The center cell (current spot, now) is anchored to the live mid you see on the Tickets card; everything else fans out from there with delta and theta sensitivity.

### Heatmap colors

Each cell's color encodes **close-now P&L** of the spread at that spot/time pair, normalized to the spread's own max profit (for gains) or max loss (for losses):

| Level | Color | Normalized P&L range |
|---|---|---|
| **+3** *(deep emerald, bold)* | brightest emerald | ≥ +55% of max profit |
| **+2** *(medium emerald)* | medium emerald | +25% to +55% of max profit |
| **+1** *(faint emerald)* | faint emerald | +8% to +25% of max profit |
| **0** *(stone)* | neutral stone | within ±8% of breakeven |
| **−1** *(faint rose)* | faint rose | −8% to −25% of max loss |
| **−2** *(medium rose)* | medium rose | −25% to −55% of max loss |
| **−3** *(deep rose, bold)* | brightest rose | ≥ −55% of max loss |

### Hover tooltip

Hover any cell to see the exact P&L number per contract plus a decomposition:

```
Spot $182.50 · +30m
premium +$1.42 · close-now P&L +$58/contract
composed: +$28 theta · +$5 delta · +$3 γ/vega
```

The "composed" line tells you *why* the cell is green or red — whether you're being paid for time, direction, or curvature/vol. When delta dominates a profit cell, the trade is direction-dependent; if a reversal comes, you'll give it back fast. When theta dominates, the trade is time-dependent; sitting on it longer is the play. That distinction matters for how you exit.

The "current spot" row is tagged with a small emerald **▸** in the left margin, and the "now × current spot" cell is ringed in emerald.

### VEX / TEX cluster annotations

Extra markers in the margins flag where the chain's vol and theta hedging concentrates:

| Marker | Where | Meaning |
|---|---|---|
| **●** *(amber, left margin)* | A spot-row label | The strike nearest this row sits in the top quartile of vega × open-interest across the chain. If price drifts into this row and IV moves, premium will re-price faster than the linear model suggests. |
| **⌛** *(amber, column header)* | A time-column header | The column falls inside the theta-burn window. Only shown for spreads with **DTE ≤ 1** — for longer DTEs theta is roughly constant per day and the marker would be noise. |
| **amber ring on cell** | Any cell intersecting either cluster | The cell sits where vol or decay effects are concentrated; the tooltip's cluster warning explains how much to inflate the model estimate by. |

When a hover tooltip lands on a cluster cell, it appends one of:

- `⚠ strike near VEX cluster — vol shock could move premium ~1.3× this estimate`
- `⚠ horizon in TEX cluster — actual decay may be ~1.4× this estimate`

These multipliers are rough rules of thumb, not precise corrections — treat them as flags to widen your mental error bars on that cell, not exact adjustments.

### When the Lookup refreshes

The grids stay in sync with whatever data is loaded:

- **Refresh chain & re-rank** → all three grids redraw; center cells snap to new live mids.
- **Reload all data** → same as above, plus the bias card refreshes.
- **Change strategy, target delta, or width preference** → candidates change, grids change with them, no provider call needed.
- **Switching tabs** → nothing changes; the data is just shown differently.

If you're watching a position and the spread starts moving, hit Refresh chain & re-rank — the colors and numbers update immediately. One provider call per refresh.

### Pricing assumptions

- European exercise (slight error for early-exercisable American single-name puts near dividends).
- Sticky-strike implied volatility — each leg's IV stays attached to its strike as spot moves.
- Constant risk-free rate of 5%.
- No dividends.
- The center cell is anchored to the live mid; surrounding cells use Black-Scholes sensitivity from there.

If a leg is missing IV data, the cell falls back to intrinsic value and the card shows `(some legs missing IV — intrinsic fallback)` in amber italics.

---

## VEX / TEX lenses — going beyond GEX

EdgeLane's primary bias engine is built around **GEX** (gamma exposure × open interest) — the structural map of where dealers must hedge. Two adjacent lenses live alongside it for the cases where gamma alone doesn't tell the full story.

Two genuinely different VEX/TEX use cases, on two different tabs.

- **Tickets tab** — *"is this trade worth doing right now?"* The lens is positioning: where do the GEX, VEX, and TEX walls sit relative to my strikes? Static answer. Helps pick the spread.
- **Lookup tab** — *"how does my trade evolve in the next 3 hours and across spots?"* The lens is flow over time and price: where will vol-driven and theta-driven dealer hedging be strongest? Dynamic answer. Helps decide when to bail or hold.

### Where each lives

- **Tickets tab** gets the optional **3-lens positioning strip** (only when divergent) — for trade selection.
- **Lookup tab** gets **margin annotations + tooltip decomposition** — for trade-evolution awareness.

### Tickets tab — 3-lens positioning strip

The strip only appears when GEX, VEX, and TEX disagree by more than 5 strikes. When it shows up, the three lenses tell you *what kind of risk dominates this trade*:

- **GEX wall** — where dealers pin price (the structural magnet).
- **VEX wall** — where vol gets re-priced fastest (the IV-shock magnet).
- **TEX wall** — where theta bleed concentrates (the time-decay magnet).

What you do when you see it:

- **All three line up with your credit zone** → take the trade with normal size. (You'd never see the strip in this case — it would be hidden, since the lenses agree.)
- **Lenses diverge, with VEX and TEX pulling *below* the GEX wall** (like the strip's example) → price has a second gravity well your spread isn't structured for. Either tighten the short strike toward the VEX/TEX side, cut size, or skip and wait for the lenses to re-converge.
- **Divergence with VEX/TEX pulling *above*** → mirror logic on the upside.

The strip is hidden by default and dashed-amber-bordered when it appears, so it doesn't add noise to the 99% of cards where the three lenses agree.

### Lookup tab — margin annotations & tooltip decomposition

Two layers on top of the existing premium grid:

- Spot-rows whose closest strike sits in a **VEX cluster** (top quartile of vega × OI) get an amber **●** in the left margin. Premium at those rows will move faster than the linear model predicts when IV ticks.
- Time-columns inside a **TEX cluster** (back half of the time axis for ≤1 DTE spreads) get an amber **⌛** in the column header. Theta bleeds harder than the linear estimate at those horizons.
- The hover tooltip on every cell breaks the close-now P&L into **theta / delta / γ-vega** components, so you can see *why* a cell is profitable (or not). When a cluster intersects, the tooltip appends a multiplier hint.

What you do with it: scan the grid before you commit to sitting on a trade for hours. If your planned exit horizon lands on a ⌛ column, plan to harvest theta there rather than wait. If the **●** is on a row your spot keeps drifting toward, pre-stage a roll because vega will hit you before delta does. The tooltip is for the moment you ask *"why is this cell green?"* — it tells you whether you're being paid for time, direction, or vol, which determines what kills the position if it reverses.

### How to think about the two together

A short, useful rule of thumb:

> A is for **picking** the trade (one-shot signal at entry).
> B is for **managing** the trade (continuous signal across the day).
>
> If you mostly enter, set GTC, and walk away → A is the higher-value add.
> If you actively babysit positions and exit dynamically → B pays off more.
>
> They don't overlap; they're entry vs. lifecycle.

Both features stay quiet most of the time. The strip only appears when there's real divergence to flag, the dots only mark genuine cluster strikes, and the ⌛ only shows up on same-day spreads. That's by design — they're meant to be *signals*, not decoration.

---

## Buttons & refreshes

Three different refresh actions across two locations:

| Button | Location | Refreshes | When to click | Provider cost |
|---|---|---|---|---|
| **Detect bias from Greeks** | Inputs panel | Bias card | First analysis of a ticker, or after major market move | 2 calls |
| **↻ Re-detect bias from Greeks** | Inputs panel | Bias card — **force-refetch** past cache | When the tape clearly moved and you want a fresh GEX read on the same symbol/expiration | 2 calls |
| **↻ Refresh chain & re-rank** | Inputs panel | Chain prices, IV, candidates | Every few minutes while watching a setup; before placing a limit order | 1 call |
| **↻ Reload all data** | Status row (top) | Both bias + chain | Ticker change, or "start fresh" | 3 calls |

Bias data is slow-moving (GEX walls stable 30 – 60 min). Chain prices are tick-by-tick. Use the chain refresh frequently; bias refresh only when warranted. The status row shows the **last-fetched timestamp** for each cache so you can see how stale the data is.

The Spot price in the stats strip updates immediately on every chain refresh, even before the bias narrative finishes recomputing.

---

## Typical workflow

1. Type symbol, pick expiration, hit **Detect bias from Greeks**.
2. Read the bias card — note the label, score, confidence, and recommended strategies.
3. Adjust strategy / target delta / width if needed; the three candidate cards re-rank instantly.
4. Check the composite tradeability banner. If rose, skip this setup.
5. On the engine pick, look at the limit-order tier card. If green at market, take it; if "sub-fair, limit-only," apply the reality-check triage.
6. Optional: switch to **Lookup** and check how the spread would look 30 – 60 minutes from now or if spot moves to specific levels.
7. Click **⎘** to copy the trade ticket. Paste into your broker, verify, place.
8. While watching: hit **↻ Refresh chain & re-rank** every few minutes. Update the limit price if conditions change.
9. If the bias deteriorates materially: re-detect bias to see if the wall moved.

This is the rare disciplined options play: instead of taking what the market gives, you tell the market what you'd accept. Most days you don't fill. The days you do, you've extracted real edge from a market that briefly mispriced your structure.

---

## Torque — fast order builder

Separate page on the market backend (`:8789/torque`) for placing multi-leg
orders fast — **no** bias logic. Run: `cd market/backend && make run-dev` (paper)
or `make run-prod` (real). Build/deploy doc: `docs/torque.md`; Torque's own
badge/reading reference (this file's counterpart, for Torque): `docs/torque_operating_manual.html`.

1. Pick a **ticker** + one **strategy** (10, incl. single-leg). Legs auto-fill at
   per-ticker offsets snapped to the live grid; −/+ nudges strikes.
2. **Spot** is live (Yahoo, parity fallback); strikes + net bid/mid/ask re-anchor
   every ~3s. Type a limit or **Send Market**. **DRY RUN** validates without placing.
3. **Auto-close +N%** (default 30%): on confirmed fill it places the profit-target
   close (single-leg limit → native OTO; spreads/market → background watch-then-close).
4. **Orders panel** (bottom): working orders + armed closes — click a limit to
   modify, or cancel. Env badge **SANDBOX** (gold) / **PRODUCTION** (red).

## Simmer — premium-selling readiness engine (live: backend + UI)

Watches your ticker watchlist every 5 minutes and tells you **when a name is
conditioned to sell a credit spread** — structure, strikes, credit, POP at the
breakeven, payoff-integrated EV — gated hard on catalysts (SEC 8-K hard-blocks,
earnings blackouts, macro calendar), liquidity, VRP, and the 0.20–0.35 short-delta
band. Most names are **vetoed most of the time; the veto reasons are the
product.** Full doc: `docs/simmer.md`.

**Backend** (live — rides in the market service, no separate process):

- Endpoints under `/simmer/*`, gated on the `simmer` entitlement
  (`profiles.tools_enabled`); admin token works too:
  `/simmer/status` (watcher health + regime), `/simmer/analyze/{sym}` (full
  envelope), `/simmer/news/{sym}` (scored headline clusters + velocity),
  `/simmer/watchlist`, `/simmer/alerts`, `/simmer/settings`,
  `/simmer/outcomes/summary` (paper-outcome calibration).
- **Watchlist writes are client-side under RLS** (frontend, when built); today
  seed rows into `simmer_watchlist` via service role. Alerts are written only by
  the backend and fan out per user with that user's own thresholds.
- News: Alpaca (free paper-account keys) → dedup into clusters → Gemini scores
  only never-seen headlines (~$0.25/mo at 20 tickers). No keys = clean no-op;
  sentiment can only pick the side or veto, never promote a score.

**Config check before any deploy** (all in `edgelane_market.config`):

| Key | Check |
|---|---|
| `DEVMODE` | **`false` for prod.** The flag flips Tradier base+token+account AND the DuckDB file in one go — a prod container with `true` serves users sandbox quotes and writes history into the sandbox DB while looking perfectly alive. Local testing flips it `true`; always flip back. |
| `SIMMER_DATA_PROVIDER` | `tradier` (default) or `yahoo` — Yahoo keeps the sweep off the 120 req/min Tradier budget Matrix/Torque share, and supplies the daily OHLC bars that IV-history/outcomes need. |
| `SIMMER_NEWS_PROVIDER` | `alpaca` \| `rss` \| `off` |
| `ALPACA_KEY_ID` / `ALPACA_SECRET_KEY` | From a free paper account (app.alpaca.markets → enable MFA first, or the API-keys widget stays hidden). |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Same key the legacy frontend uses; default model `gemini-2.5-flash-lite`. |

**Deploy (backend — the only Simmer surface today):**

```bash
# one-time per schema change
make db-push                 # applies supabase/migrations (0010 = Simmer tables)

# every code change
sed -n 's/^DEVMODE=/&/p' edgelane_market.config   # eyeball it: must be false
make deploy-be-restart       # rebuilds the container from the working tree
```

The config file is bind-mounted into the container, so key changes need only a
container restart, not a rebuild. `make stop` is safe for the local dev server
(SIGTERM + grace — a hard kill corrupts the DuckDB WAL and the next boot dies
replaying it; if that happens: delete the `.wal` file next to the DB and reboot).

**Frontend** (live): a SvelteKit 5 SPA (adapter-static, runes) at
`https://edgelane-simmer.vercel.app`. Sign-in, watchlist/settings writes, and every
read go through the backend API — the **browser never contacts Supabase directly**
(auth flows through the backend `/auth/*` proxy; see `market/backend/app/routes/auth_proxy.py`).

### The moving pieces (and how they find each other)

| Piece | Lives on | How it's reached |
|---|---|---|
| Backend (market API + Torque + Simmer) | local Docker container `edgelane-backend` | Cloudflare **named tunnel** → permanent hostname **`edge.facades.trade`** |
| Cloudflared connector | container `edgelane-cloudflared` | `cloudflare/cloudflared` running `tunnel run` with `CF_TUNNEL_TOKEN` |
| Matrix UI | Vercel project `edgelane-matrix` at `matrix.facades.trade` | calls the baked `window.__EDGELANE_API_BASE__` |
| Simmer UI | Vercel project `edgelane-simmer` at `simmer.facades.trade` | calls the baked `window.__EDGELANE_API_BASE__` |
| Torque | served by the backend itself | `https://edge.facades.trade/torque` |

One host serves all three products. The hostname belongs to the **tunnel**, not to
the container, so restarts, rebuilds and host moves all keep the same URL — which is
why both frontends simply **bake** it at deploy time from `EDGELANE_API_BASE`. There
is no runtime pointer, no publisher, and no self-heal step any more; if you change
the hostname, redeploy the frontends.

> The Supabase `app_config.api_base` row and the Vercel Edge Config store that the
> old rotating-URL scheme used are no longer read or written by anything. They're
> inert — drop them whenever convenient.

### One-time setup — `make frontend-setup`

Run once (and again whenever a Vercel project or the Edge Config is added/renamed).
Idempotent; safe to re-run.

```bash
vercel login            # setup reads this CLI session for provisioning — no token to paste
make frontend-setup     # 1) links/creates BOTH Vercel projects (Matrix + Simmer)
                        # 2) syncs Supabase Auth uri_allow_list with both origins
```

`deploy-fe` / `deploy-simmer` **refuse** until the project is linked and point you
here. Provisioning uses your Vercel CLI login (`~/.local/share/com.vercel.cli/auth.json`).

**The tunnel is created once, in the Cloudflare dashboard** (Zero Trust → Networks →
Tunnels → Create tunnel), with public hostname `edge.facades.trade` →
`http://edgelane-backend:8789`. Paste the connector token into `deploy/.env`:

```bash
CF_TUNNEL_TOKEN=eyJ...                        # deploy/.env — compose fails fast if blank
EDGELANE_API_BASE=https://edge.facades.trade  # baked into both SPAs; deploy.sh dies if blank
```

### Deploy / update — what to run, in what order

Do only the steps for what changed. The **sequence matters** (schema → backend →
frontends) because a frontend may depend on a new backend field, not because any URL
moves — the tunnel hostname is fixed.

1. **DB schema changed** → `make db-push` (applies `supabase/migrations`).
2. **Backend / engine changed** → verify `DEVMODE=false`, then `make deploy-be`
   (rebuild) or `make deploy-be-restart`. The tunnel hostname is unchanged, so no
   frontend redeploy is needed unless the UI itself changed.
   - Config-only change (keys in `edgelane_market.config`): the file is bind-mounted —
     a container restart suffices, no rebuild.
3. **Matrix UI changed** → `make deploy-fe` (also re-syncs Supabase `site_url` +
   `uri_allow_list`, which is where the Simmer origin gets added if setup didn't).
4. **Simmer UI changed** → `make deploy-simmer`. (npm ci + `vite build` can take
   >2 min; that's normal.)
5. **Always finish with** → `make check-tunnel` — verifies container health, the
   Supabase pointer, **the Edge Config pointer matches it**, tunnel `/status`, CORS for
   the Vercel origin, and the Turnstile path.

**Make sure, before/after:**

- `DEVMODE=false` in `edgelane_market.config` before any prod backend deploy (it flips
  Tradier token/base/account **and** the DuckDB file in one switch).
- A permanent `VERCEL_API_TOKEN` is in `deploy/.env` if you want tunnel self-heal.
- After a backend/tunnel restart, confirm the pointer landed —
  `make check-tunnel` shows `✓ Vercel Edge Config api_base matches Supabase`, or watch
  `docker compose -f deploy/docker-compose.yml logs -f edgelane-cloudflared` for
  `published api_base -> Vercel Edge Config (HTTP 200)`.
- The Simmer origin is in Supabase `uri_allow_list` (so confirmation/reset emails can
  redirect back) — added by `make frontend-setup` or the next `make deploy-fe`.

### Simmer UI internals worth knowing

- **Deployed-vs-dev is a build-time constant** (`import.meta.env.PROD`), not baked
  config. A production `vite build` always resolves the backend via `/api/config` and
  locks the `?api=` override; it carries **no** Supabase creds or secrets in the
  browser. `edgelane.config.js` is inert for detection.
- **SPA routing on Vercel:** `simmer/ui/vercel.json` uses `rewrites: /(.*) → /index.html`
  and **no `cleanUrls`**. Vercel's `source` is path-to-regexp — **no lookaheads**
  (`(?!api)` silently matches nothing); `/api/*` stays safe because functions/static
  files are matched before rewrites.

## Strike Profiles — debit strike picker config (admin)

Admin-only page on the market backend for tuning **how the engine builds debit
verticals** (`bull_call` / `bear_put`) per ticker — the OTM-OTM "buy the move,
sell the wall" strike picker. Config only; **no** orders are placed here. Standalone
page (not linked from the dashboard). Full doc: `docs/strike_profiles.md`.

**How to access:** open `…/strike-profiles/admin?token=<ADMIN_API_TOKEN>` on the
backend host — local `http://127.0.0.1:8789/strike-profiles/admin?token=…`, or prod
the Cloudflare tunnel URL + the same path. Auth (same admin secret as Torque):
- Browser nav → append `?token=<ADMIN_API_TOKEN>` (the page then sends it as the
  `X-Admin-Token` header on every API call).
- API/curl → send `X-Admin-Token: <ADMIN_API_TOKEN>`.
- When `AUTH_ENABLED=true`: anonymous → **401**, signed-in non-admin user → **403**,
  admin token → **200**. When `AUTH_ENABLED=false` (dev) the gate is a no-op.

1. **List** — every saved profile (one row/symbol; `DEFAULT` is the fallback for any
   unconfigured ticker). Shows enabled, long Δ-band, target source, width window,
   snap, min OI.
2. **Edit** — click a row → form prefilled from the current values. Save is a
   **full-object replace** (the page loads then re-sends the whole object). Blank
   nullable fields (`long_offset_pts`, `min/max_width_pts`) mean **auto / derive
   from expected move**, not `0`.
3. **Add symbol** — type a ticker → a `DEFAULT`-seeded form → save creates the
   override. Also add it to `SYMBOLS=` in `edgelane_market.config` so the poller
   fetches it.
4. **Applies on the next poll cycle (~15s)** — no restart needed. SPX + NDX ship
   seeded; unconfigured symbols inherit `DEFAULT`. (No delete endpoint yet.)

## tp — positions & P&L CLI

`tp` (`tools/tradier_positions.py`) shows positions and realized P&L. Full doc:
`tools/tp_operating_manual.html`.

- `tp` — open positions. `tp -G` — realized gain/loss (merges today's `/orders`
  with prior-day opens via `/history`, back to 7 days; partials use the
  whole-trade average as cost basis).
- `tp -B SYM STRIKE [DATE]` — open a single-leg position. The date is optional
  (defaults to today) and can be an explicit date **or** `+N` for "N trading
  days out": `tp -B spy 738c+1 -x` (or `tp -B spy 738c +2`). `+N` resolves to
  the Nth real expiration in the live chain, so weekends/holidays are skipped
  automatically and the target date is guaranteed tradeable — `+1` on a Thursday
  whose Friday is a market holiday lands on Monday.

---

## Troubleshooting

| What you see | What's happening |
|---|---|
| Heavy single-name ticker (MU, AMD, SMCI, COIN, PLTR, ARM, AVGO, MARA, MSTR) takes 20 – 30 s on first bias detect | The full options chain is being pulled in pieces. Normal for these tickers. |
| Second bias detect on same symbol returns instantly | Result is cached for 5 minutes. |
| **↻ Re-detect bias from Greeks** label appeared | Bias is cached. Clicking force-refetches past the cache. |
| Spot price updated but bias narrative looks stale | Spot is decoupled from the bias pipeline. Click re-detect to refresh the narrative. |
| Lookup cell says "intrinsic fallback" | At least one leg is missing IV data from the provider; that cell uses intrinsic value instead. |
| Symbol fails after ~75 s with a timeout error | Provider didn't respond. Try again in a minute; if persistent, the symbol may need a wider timeout configuration. |

### Simmer / deployment / tunnel

| What you see | What's happening / fix |
|---|---|
| Simmer prod **login fails** or the app calls `edgelane-simmer.vercel.app/auth/...` (404) | The app isn't resolving the backend via `/api/config`. Confirm it's a **production build** (`import.meta.env.PROD`; a dev build hits localhost), that `GET /api/config` returns the tunnel URL, and that Edge Config has `api_base`. Historically caused by detecting "dev build" from baked config instead of `import.meta.env`. |
| Simmer **subroutes 404** on direct load/refresh (`/settings`, `/news`) but `/` works | SPA fallback not firing. `simmer/ui/vercel.json` must have `rewrites: /(.*) → /index.html` and **no `cleanUrls`** (it conflicts with the rewrite target). Never use a lookahead in `source` — path-to-regexp silently no-ops it. Redeploy with `make deploy-simmer`. |
| After a backend/tunnel restart, Simmer can't reach the backend (Matrix is fine) | Edge Config `api_base` went stale — the container couldn't PATCH it. `deploy/.env` is missing a **permanent** `VERCEL_API_TOKEN`, or it expired. Check `docker compose … logs edgelane-cloudflared` for `skipping Edge Config` / non-200; set the token and restart the container. |
| `make check-tunnel` says **"Edge Config not configured"** | `VERCEL_API_TOKEN` / `EDGE_CONFIG_ID` unset in `deploy/.env`. Run `make frontend-setup` (writes `EDGE_CONFIG_ID`) and add a permanent `VERCEL_API_TOKEN`. |
| Frontends can't reach the backend after a restart | The hostname is permanent, so this is the tunnel connector, not a rotation. Check `docker compose … logs edgelane-cloudflared` for a registered connector, and that the tunnel's public hostname still routes to `http://edgelane-backend:8789`. |
| Simmer email confirmation / password-reset link is **rejected on redirect** | The Simmer origin isn't in Supabase Auth `uri_allow_list`. Run `make frontend-setup` or `make deploy-fe` (both PATCH it), or add `https://edgelane-simmer.vercel.app/**` manually. |
| `make frontend-setup` prints **"no Vercel token — skipping Edge Config"** | You're not logged in for provisioning. Run `vercel login`, then re-run. (This is separate from the permanent `VERCEL_API_TOKEN` the container needs.) |
| Simmer prod `edgelane.config.js` shows `API_BASE = null` / "LOCAL DEV config" | Expected. Vercel rebuilds remotely and serves the `static/` file; it's **inert** — detection is `import.meta.env.PROD` and the API base comes from `/api/config`. Not a bug. |
