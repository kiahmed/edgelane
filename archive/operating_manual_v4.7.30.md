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

- *"If NVDA ticks up $2 in 30 minutes, what's my spread worth?"* — Row two steps above center, column `+30m`. The number is the projecte