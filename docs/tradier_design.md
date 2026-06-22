# Tradier Integration — Design

**Status:** Historical design doc — implemented. EdgeLane originally used Atlas's API (mind-vest.io) as its data backbone; Atlas was fully retired ~May 2026 and Tradier is now the sole data provider. The Atlas-side details below describe the former system and the migration that replaced it.
**Scope:** Phase 1 + Phase 2 from the proposal. Phase 4 (streaming, order placement) deferred.

This document is the architectural contract for the migration. It does not repeat
the rationale, vendor comparison, or risk register from the proposal — read that
first. This is the design the implementation document follows file-by-file.

---

## 1. Goals and non-goals

**Goal.** Replace Atlas (mind-vest.io) as the data backbone with Tradier, with
**zero observable change** to the UI, the bias engine output, the optimizer
output, or the Lookup tab. From the user's perspective, the only difference is
the quota pill semantics (now "rate-limit remaining this minute" instead of
"calls remaining this month") and the absence of the symbols Atlas choked on
(MU, AMD, SMCI — all instant under Tradier).

**Goal.** Move dealer-GEX aggregation **out of the network path** and into a
pure function the JSX already has access to. The chain we fetch becomes the
single source of truth; GEX/DEX/walls are a `useMemo` over it. This is the
architectural cleanup the proposal flagged as worth doing on its own merits.

**Non-goals (Phase 1 + 2).** No multi-leg order placement via Tradier. No
WebSocket streaming quotes. No account balances, positions, or transaction
history. No first-party JS SDK adoption — we keep calling REST directly. (At
cutover the Atlas client was retained as a selectable fallback for the first
month per the gated-migration policy; it has since been fully removed.)

---

## 2. Architecture overview

```
                  edge_lane_config.config
                          │  DATA_PROVIDER ∈ {atlas, tradier}
                          ▼
                  edge_lane_build.sh
                  ─ substitutes __DATA_PROVIDER__, __TRADIER_TOKEN__, etc.
                          │
                          ▼
        ┌─────────────────────────────────────────────┐
        │           Provider factory (JSX)            │
        │   const dataProvider = window.DATA_PROVIDER │
        │     === 'tradier' ? tradier : atlas;        │
        └────────────┬──────────────────┬─────────────┘
                     │                  │
              ┌──────▼─────┐      ┌─────▼─────┐
              │   atlas    │      │  tradier  │
              │ namespace  │      │ namespace │
              └──────┬─────┘      └─────┬─────┘
                     │ same interface   │
                     │ same shapes      │
                     └────────┬─────────┘
                              │
                              ▼
                  Normalized chain
                  { spot, dte, contracts: [ _normalizeContract ... ] }
                              │
                              ▼
              ┌───────────────────────────────────┐
              │  gexState  (useMemo, derived)     │
              │   _computeDealerExposures(        │
              │     chainData.contracts, spot)    │
              │   → { exposures_by_date,          │
              │       portfolio_totals,           │
              │       key_levels }                │
              └─────────────────┬─────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
  _computeBiasSignals      Optimizer engine        Lookup tab grid
  ( _findGexWall,          (ranks candidates       (BS projection
   _aggregateExposures )     over chain)            anchored to mid)
```

The abstraction lives entirely inside the JSX. There is no module split — single
file deploy is preserved. Provider namespaces are objects with identical method
signatures and return shapes. `_computeBiasSignals`, `_findGexWall`,
`_aggregateExposures`, the optimizer, and the Lookup grid are provider-agnostic
by construction — they consume only the normalized contract shape and the
`{exposures_by_date, portfolio_totals, key_levels}` triplet.

The former Atlas path pulled `exposures_by_date` from the
`analyze_greek_exposures` endpoint. The Tradier path computes it locally from
the chain via `_computeDealerExposures` (Section 5). Both terminate at the same
shape — see Section 4.

---

## 3. Provider interface contract

Both `atlas` and `tradier` must implement the following five methods. Method
names, parameter names, and return shapes are load-bearing — downstream code
references the field names verbatim.

```javascript
interface DataProvider {
  stockQuote(symbol: string): Promise<{
    symbol: string,
    price: number,            // last/mid spot — required
    volume: number | null,
    timestamp: string | null,
    raw: object,              // provider-native response, debugging only
  }>;

  optionsChain(
    symbol: string,
    expiration: string,       // 'YYYY-MM-DD'
    opts?: { strikeBandPct?: number, maxExpirations?: number }
  ): Promise<{
    spot: number,
    dte: number,
    atm_iv_pct: number,       // duplicated as atmIV for legacy consumers
    atmIV: number,
    expected_move: number,    // duplicated as expectedMove
    expectedMove: number,
    expected_move_pct: number,
    expectedMovePct: number,
    contracts: NormalizedContract[],
  }>;

  optionExpirations(symbol: string): Promise<{
    symbol: string,
    expirations: string[],    // 'YYYY-MM-DD', ascending
  }>;

  greekExposures(
    symbol: string,
    num_expirations?: number  // default 3
  ): Promise<{
    symbol: string,
    spot: number,
    exposures_by_date: {
      [expiration: string]: {
        by_strike: Array<{ strike: number, gex: number, dex: number, vex?: number, tex?: number }>,
        totals: { net_gex: number, net_dex: number, net_vex?: number, net_tex?: number },
      }
    },
    portfolio_totals: { net_gex: number, net_dex: number, net_vex?: number, net_tex?: number },
    key_levels: {
      call_wall: { strike: number, gex: number } | null,
      put_wall:  { strike: number, gex: number } | null,
    },
  }>;

  subscriptionStatus(): Promise<{
    provider: 'atlas' | 'tradier',
    quota_used?: number,      // atlas only
    quota_total?: number,     // atlas only
    rate_remaining?: number,  // tradier — last X-Ratelimit-Available
    rate_limit?: number,      // tradier — last X-Ratelimit-Allowed
    rate_reset?: number,      // tradier — epoch seconds
  }>;
}
```

### Error contract

Both providers throw `Error` for any failure. Messages must be prefixed
`Provider <method>:` so the existing status-row error handler renders them
cleanly. Specific failure-mode strings (recognized by the JSX retry layer):

- `Provider <method>: HTTP <status>: <body-snippet>` — non-2xx response
- `Provider <method>: request timed out after <n>s.` — AbortController fired
- `Provider <method>: blocked by CORS or network.` — fetch threw network error
- `Provider <method>: <code>: <message>` — provider returned 200 with error body
  (Atlas-style quota wrap, Tradier `{fault: {...}}`)

Transient errors (HTTP 5xx, timeouts, network errors) are retried with backoff
inside `_atlasCall` / `_tradierCall`; the surfaced error is the final failure
after retries are exhausted. Non-retryable errors (auth, quota, invalid symbol)
throw on the first response.

---

## 4. Schema mapping

### Per-contract shape

| Normalized field   | Atlas source                                            | Tradier source                          | Notes |
|---|---|---|---|
| `strike`           | `strike` \| `strike_price` \| `strikePrice`             | `strike`                                | number |
| `side`             | `side` \| `type` \| `option_type` \| `contract_type`    | `option_type` (`'call'` \| `'put'`)     | lowercased, mapped to `'call'`/`'put'` |
| `bid`              | `bid` \| `bidPrice`                                     | `bid`                                   | 0 if null |
| `ask`              | `ask` \| `askPrice`                                     | `ask`                                   | 0 if null |
| `mid`              | `mid` if present, else `(bid+ask)/2`, else `last`       | `(bid+ask)/2` else `last`               | 0 if all null |
| `delta`            | `delta` \| `greeks.delta`                               | `greeks.delta`                          | requires `greeks=true` query param |
| `gamma`            | `gamma` \| `greeks.gamma`                               | `greeks.gamma`                          | requires `greeks=true` |
| `theta`            | `theta` \| `greeks.theta`                               | `greeks.theta`                          | requires `greeks=true` |
| `vega`             | `vega` \| `greeks.vega`                                 | `greeks.vega`                           | new field; downstream code already tolerates null |
| `iv`               | `iv` \| `implied_volatility` \| `greeks.iv`             | `greeks.mid_iv`                         | both are percent (e.g. 17.8 for 17.8%) |
| `open_interest`    | `open_interest` \| `openInterest` \| `oi`               | `open_interest`                         | 0 if null |
| `volume`           | `volume` \| `vol`                                       | `volume`                                | 0 if null |

`_normalizeContract` already handles every alias on the Atlas side. The Tradier
side adds no new aliases — its field names are a subset of those already listed.

### Chain wrapper shape

| Normalized wrapper      | Atlas source                                             | Tradier source                                                         |
|---|---|---|
| `spot`                  | `get_stock_quote` parallel call → `price`                | `GET /v1/markets/quotes` → `quotes.quote.last`                          |
| `contracts`             | `_flattenChain(resp.chain ?? resp.contracts ?? resp)`    | `resp.options.option[]` (already flat, single-expiration)               |
| `expirations` (list)    | `Option-Expiration-Dates` → `expirations.date[]`         | `GET /v1/markets/options/expirations` → `expirations.date[]`            |
| `atm_iv_pct`            | computed: avg of ATM call/put `iv`                       | same — computed from normalized contracts                               |
| `expected_move`         | computed: ATM call mid + ATM put mid                     | same                                                                    |
| `dte`                   | computed from expiration vs now                          | same                                                                    |

Tradier's `options/chains` returns one expiration per call — the existing
`_flattenChain` is overkill for it but harmless. Per-expiration parallel fan-out
(needed by `greekExposures` to populate `exposures_by_date`) uses
`Promise.all([tradier.optionsChain(sym, exp1), ...])`.

### Greek-exposures shape (the Tradier gap)

Atlas returns the full `exposures_by_date` / `portfolio_totals` / `key_levels`
shape directly from `analyze_greek_exposures`. Tradier returns nothing of the
kind — `tradier.greekExposures` fetches N expirations of chain data in parallel
and feeds each to `_computeDealerExposures` (Section 5) to produce that shape.

---

## 5. Local dealer-GEX aggregator

`_computeDealerExposures(contracts, spot, opts?)` is a pure function. Same
inputs always produce the same output. It runs on the client in O(N) over the
contract array; ~5 ms for 1,000 contracts.

### Signature

```javascript
function _computeDealerExposures(
  contracts: NormalizedContract[],   // may span 1 or many expirations
  spot: number,
  opts?: {
    contractMultiplier?: number,     // default 100
    strikeTieBreak?: 'lower-call' | 'higher-put',  // default both true (see below)
  }
): {
  exposures_by_date: {
    [expiration: string]: {
      by_strike: Array<{
        strike: number,
        call_oi: number, put_oi: number,
        call_gamma: number, put_gamma: number,
        call_delta: number, put_delta: number,
        gex: number,                 // dollar terms, dealer convention
        dex: number,                 // dollar terms, dealer convention
      }>,
      totals: { net_gex: number, net_dex: number },
    }
  },
  portfolio_totals: { net_gex: number, net_dex: number },
  key_levels: {
    call_wall: { strike: number, gex: number } | null,
    put_wall:  { strike: number, gex: number } | null,
  },
}
```

The input `contracts` array is the same `NormalizedContract` shape from Section
4. The normalized contract must carry its `expiration` (added field — see Section
6 cache invalidation notes) so the aggregator can bucket by date. If
`contracts` is a single-expiration slice (most calls from `useMemo`), the result
has one key in `exposures_by_date`; that's fine.

### Math

Sign convention matches the standard public-GEX literature and the convention
Atlas's `key_levels` agreed with on the validation symbols. Calibrated during
Phase 1a if the probe (Section 7) disagreed.

```
dealer_gamma_at_strike(K) = put_gamma(K) × put_OI(K)
                          − call_gamma(K) × call_OI(K)

gex(K)                    = dealer_gamma_at_strike(K)
                          × contractMultiplier        // 100
                          × spot²

dealer_delta_at_strike(K) = call_delta(K) × call_OI(K)
                          − put_delta(K) × put_OI(K)

dex(K)                    = dealer_delta_at_strike(K)
                          × contractMultiplier
                          × spot

net_gex                   = Σ over all (expiration, K)
net_dex                   = Σ over all (expiration, K)
```

### Wall selection

For each expiration's `by_strike` array (sorted ascending by strike):

```
candidates_above_spot = rows where strike >  spot
candidates_below_spot = rows where strike <  spot
candidates_at_spot    = rows where strike == spot   // tie-broken below

call_wall = argmin over candidates_above_spot of gex(K)
            // most-negative dealer GEX above spot

put_wall  = argmax over candidates_below_spot of gex(K)
            // most-positive dealer GEX below spot
```

**Tie-breaks.** If multiple strikes share the extremum:
- `call_wall`: pick the **lower** strike (closer to spot, more relevant resistance)
- `put_wall`:  pick the **higher** strike (closer to spot, more relevant support)

**Portfolio-level `key_levels`** is computed by re-aggregating each strike across
all expirations (sum gex per strike, then apply the wall-selection rules above
to the portfolio-level series). This matches the multi-expiration semantics
Atlas used for its top-level `key_levels` block.

### Edge cases

| Case                                       | Behavior |
|---|---|
| Contract missing `gamma` (null)            | Treat as 0 — strike contributes nothing to GEX. Same for `delta` / DEX. |
| Strike with zero `open_interest` on a side | Contributes 0 from that side. Both sides zero → row dropped from `by_strike` to keep the array sparse. |
| No strikes above spot                      | `call_wall = null`. `_findGexWall` already handles null and falls through to neutral bias. |
| No strikes below spot                      | `put_wall = null`. Same. |
| All gex values zero                        | Both walls null. `_computeDirectionalScore` returns 0, bias label = `'neutral'`. |
| `spot` is null / zero                      | Aggregator returns `null` (matches the `useMemo` guard in Section 6). |
| `contracts` empty / null                   | Returns `null`. |
| `contractMultiplier` differs (mini options)| Caller passes via opts. Default 100. |

`vex` and `tex` (vega / theta exposures) are deliberately omitted from Phase 1.
Atlas exposes them in `portfolio_totals`; the JSX bias engine (`_aggregateExposures`)
never reads them today. If a future signal needs them, the aggregator extends
trivially — same shape, swap `gamma` → `vega` / `theta`.

---

## 6. Client-side derived state (useMemo pattern)

GEX state is **never fetched separately under Tradier**. It is a pure derivation
of the chain in the React render path.

### Inside `SpreadOptimizer`

```javascript
const gexState = useMemo(() => {
  if (!chainCache?.data?.contracts || !spotPrice) return null;
  return _computeDealerExposures(chainCache.data.contracts, spotPrice);
}, [chainCache, spotPrice]);
```

### Lifecycle

| Trigger                                          | What happens                                  |
|---|---|
| `fetchChain` succeeds (Reload, chain refresh)    | `chainCache` updates → memo recomputes        |
| User picks a new expiration                      | `cacheKey` changes → `fetchChain` re-runs → memo recomputes |
| `setSpotPrice(...)` on a fresh quote             | `spotPrice` dep changes → memo recomputes     |
| User flips DATA_PROVIDER and rebuilds            | New page load, fresh memo                     |
| `detectBias` is clicked                          | Reads current `gexState`, no fetch            |

### Downstream consumption

The current `detectBias` callback issues `tradier.greekExposures(sym, 3)` for
the multi-expiration case (used to find `chosen` expiration from
`exposures_by_date`). Under the new design, that call returns
`gexState`-shaped data computed from N parallel chain fetches — the consumer
code is unchanged.

For the single-expiration case (the common path), `detectBias` reads `gexState`
directly:

```javascript
// Provider-agnostic. Atlas path: gexState comes from analyze_greek_exposures.
// Tradier path: gexState comes from useMemo over chainCache.
const exposuresForChosen = gexState?.exposures_by_date?.[chosen] || null;
const computed = _computeBiasSignals(sym, expiration, spot, gexState, exposuresForChosen);
```

`_computeBiasSignals` already destructures `greeksRaw?.key_levels` and
`greeksRaw?.portfolio_totals` — those keys are populated identically by both
provider paths.

### Cache invalidation

`chainCache` is keyed by `(symbol, expiration)` already. The useMemo inherits
that key implicitly via its `[chainCache, spotPrice]` dependency. No explicit
invalidation needed. The existing 5-minute soft cache on `greekExposures`
(adaptive denylist path, v4.7.23) becomes redundant for Tradier and is bypassed
when `DATA_PROVIDER === 'tradier'` — the chain cache already gates re-fetches.

### Required schema addition

The current `_normalizeContract` does **not** carry the contract's expiration.
The aggregator needs it to bucket by date. Add `expiration` as a normalized
field, sourced from either the wrapping group's `expiration` key (Atlas) or
the contract's `expiration_date` field (Tradier).

```javascript
expiration: _str(c.expiration ?? c.expiration_date ?? sideContext?.expiration)
```

This is an additive change — no existing consumer reads or mutates the field.

---

## 7. Validation probe — acceptance criteria

The hard migration gate from `tradier_integration_proposal.md §4` and §8 Phase 1a.

**Probe location:** `tests/tradier_vs_atlas_probe.py`.

**Probe logic:**

1. For each symbol in `['SPY', 'NVDA', 'MU']`:
2. Resolve the next standard monthly expiration (same expiration both sides).
3. Within a 60-second window:
   - Fetch via Atlas: `get_stock_quote` + `analyze_greek_exposures(symbol, num_expirations=1)`
   - Fetch via Tradier: `GET /v1/markets/quotes` + `GET /v1/markets/options/chains?symbol=...&expiration=...&greeks=true`
4. Run `gex_local.compute_dealer_exposures(tradier_chain, tradier_spot)` (Python mirror of Section 5).
5. Compare:

| Metric                       | Tolerance                          |
|---|---|
| `call_wall.strike`           | within 1 strike step of Atlas's    |
| `put_wall.strike`            | within 1 strike step of Atlas's    |
| `portfolio_totals.net_gex`   | within ±15% magnitude of Atlas's   |

6. **Pass** = all three metrics within tolerance on all three symbols.

**Failure handling.** If a single symbol class fails (e.g., MU's wall is off by
3 strikes but SPY and NVDA pass), document the gap, keep Atlas-as-fallback
specifically for that symbol class, and proceed for the others. If sign
conventions are inverted across the board, flip the sign in
`_computeDealerExposures` and re-run — that's the single tuning knob the
proposal anticipates needing.

Probe is rerun any time the aggregator's math changes. Output is a single CSV
(`tests/probe_results.csv`) with one row per (symbol, run, metric).

---

## 8. Failure modes and fallbacks

| Condition                                        | Surfaced as                                    | Behavior |
|---|---|---|
| Tradier auth fails (401)                         | `Provider <method>: HTTP 401: ...`              | Non-retryable. Error banner. User checks `TRADIER_ACCESS_TOKEN`. |
| Tradier rate limit hit (429 or `X-Ratelimit-Available: 0`) | `Provider <method>: HTTP 429: ...`    | Single retry after `X-Ratelimit-Expiry`. Subsequent failure surfaces. Quota pill turns rose. |
| Greeks=null in chain response (ORATS lag)        | Aggregator treats per-contract gamma/delta as 0 | Walls may degrade to `null` if lag is broad. Bias falls to `neutral`. Logged: `[gex] N contracts missing greeks for SYM @ EXP`. |
| Tradier 5xx / network / timeout                  | `Provider <method>: ...` after backoff retries  | Same retry logic as Atlas (`_atlasCall` mirrored in `_tradierCall`). |
| Spot quote OK, chain fails                       | Error banner; `chainCache` untouched            | Previous chain stays visible (same behavior as today). |
| `gexState` null (no chain yet)                   | `detectBias` sets bias = `neutral` / no wall    | UI shows the same "no bias available" state Atlas's null-exposures path renders. |
| Validation probe **failure for a symbol class**  | Per-class Atlas fallback                        | Phase 1 keeps `atlas.greekExposures` reachable; a thin wrapper checks `SYMBOL_CLASS_USES_ATLAS = ['MU', ...]` and routes accordingly. Default fully Tradier. |
| Atlas path itself fails (fallback path also down)| Surfaced; no silent third fallback              | Operating manual already covers the symptom-to-cause cheat sheet for these. |

---

## 9. Config schema

Additions to `edge_lane_config.config`:

```bash
# Provider selection
DATA_PROVIDER=tradier

# Tradier — sandbox is 15-min delayed and free; production needs a funded
# brokerage account. Switch base URL to flip environments; token must match.
TRADIER_ACCESS_TOKEN="..."
TRADIER_BASE_URL="https://api.tradier.com"        # production
# TRADIER_BASE_URL="https://sandbox.tradier.com"  # sandbox
```

Build placeholders added to `edge_lane.template.html` and substituted by
`edge_lane_build.sh`:

```
__DATA_PROVIDER__         → window.DATA_PROVIDER
__TRADIER_TOKEN__         → window.TRADIER_TOKEN
__TRADIER_BASE_URL__      → window.TRADIER_BASE_URL
```

(During the gated-migration window an `ATLAS_FALLBACK_SYMBOLS` key and matching
`__ATLAS_FALLBACK_SYMBOLS__` placeholder existed for per-symbol Atlas fallback;
both were removed when Atlas was retired ~May 2026.)

---

## 10. Out of scope (Phase 2+)

Explicitly deferred:

- **WebSocket streaming quotes** (`wss://ws.tradier.com/v1/`). Live spot ticker
  in the header, per-tile auto-refresh. Requires session HTTP handshake +
  EventSource/WebSocket plumbing. Single-user-stream limit makes multi-tab
  awkward — needs a design pass before adoption.
- **Multi-leg order placement** via `POST /v1/accounts/{id}/orders`. Replaces
  the disabled "↗ Push to broker" stub. Requires account selection UI, dry-run
  preview parity with the trade ticket, OAuth token rotation if we move off
  personal tokens.
- **Account balances / positions / transaction history.** Adds a fourth panel
  to the page; out of scope for a data-provider swap.
- **OAuth flow.** Personal static tokens cover single-user local deployment.
  Multi-user hosted deployment is a separate project.
- **Historical OHLC, time-and-sales.** Bonus capability Tradier exposes; no
  current EdgeLane consumer.

---

## Open questions flagged for follow-up

- **Vega / theta exposures.** Aggregator omits them in Phase 1; confirm no
  future bias signal needs them before retiring Atlas entirely.
- **Sign-convention alignment.** Section 5's math assumes the standard public-
  GEX convention. If the Phase 1a probe shows Atlas uses an inverted sign on
  any metric, document the flip here and in `gex_local.py`.
- **Mini / weekly contract multipliers.** Default is 100. Tradier exposes
  `contract_size` per contract — wire into the aggregator if probe symbols
  include non-standard multipliers.
- **`atm_iv_pct` units.** Atlas returns IV as percent already (17.8 = 17.8%);
  Tradier `greeks.mid_iv` is decimal (0.178). Conversion happens in
  `_normalizeContract` for the Tradier path — verify against the probe before
  shipping.
