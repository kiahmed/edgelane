# Tradier Data Source — Integration Proposal

**Status:** Historical proposal — accepted and implemented. EdgeLane originally used Atlas's API (mind-vest.io) as its data backbone; Atlas was fully retired ~May 2026 and Tradier is now the sole data provider. This document is retained for the migration rationale; the Atlas-side details below describe the former system.
**Author:** Research fleet (4 parallel agents) synthesized into this document.

---

## 1. Executive summary

Tradier is a US brokerage that publishes its market-data and trading stack as a REST + WebSocket API. It was a strong technical fit for replacing Atlas (mind-vest.io) as EdgeLane's data backbone, with two structural differences:

1. **Tradier is brokerage-first.** Authentication is tied to a Tradier brokerage account, not a data subscription. Real-time data requires a funded brokerage account; the free sandbox is 15-minute delayed.
2. **Tradier has no dealer-positioning rollups.** Where Atlas exposed `analyze_greek_exposures` with pre-computed `key_levels.call_wall` / `put_wall` and `portfolio_totals.net_gex`, Tradier returns raw chain data only. We would compute GEX/DEX/walls **locally** from the chain we already fetch — the same architectural path I called out as "Path B" earlier in the session.

**Recommendation:** proceed. The migration is mostly a refactor toward a cleaner architecture (raw data in, analytics computed locally) that we'd have wanted anyway for Atlas resilience.

---

## 2. What Tradier brings

| Property | Tradier | Atlas (former) |
|---|---|---|
| Business model | Brokerage; API is a feature of the account | Pure data SaaS |
| Auth | Bearer token (sandbox + production keys) | Bearer token |
| Free tier | Sandbox (15-min delayed) with full feature parity | None — paid subscription required |
| Production data | Real-time consolidated feed (funded account) | Real-time |
| Rate limit | 120/min markets, 60/min sandbox, response headers expose remaining | 200 calls/month on your $30 plan |
| Streaming | HTTP-chunked + WebSocket (`wss://ws.tradier.com/v1/`) | None |
| Greeks (delta, gamma, theta, vega, IV) | In chain endpoint with `greeks=true`; sourced from ORATS, hourly refresh | In chain + analyze_greek_exposures, real-time |
| Open interest | Included on every contract | Included |
| Dealer GEX / walls | **Not provided** — compute locally | `analyze_greek_exposures` + `key_levels` |
| Order placement | Yes (multi-leg supported) | No |
| First-party SDK | OAuth helpers only; community libs exist but unsupported | None |
| Recipes / tutorials | Sidebar exists but is empty; no spread examples | None |
| Token lifetime | 24h via OAuth (partner program); static personal tokens never expire | Static API key |

**Notable wins vs Atlas:**
- No per-call quota anxiety (120/min is generous; cost is account-tier, not per-call)
- WebSocket streaming opens future tile-level live updates
- Multi-leg order placement, positions, balances — replaces a future broker integration we'd have needed anyway
- 15-min delayed sandbox is genuinely free for testing

**Notable concerns:**
- Real-time data gated by funded brokerage account ($-required to switch over)
- No first-party JS SDK — we call REST directly, no change from today
- One-stream-per-user limit will bite if we ever go multi-tab
- Recipes section is empty — no copy-paste examples for credit spreads

---

## 3. Endpoint mapping — Atlas → Tradier

(Historical mapping from the former Atlas calls to their Tradier equivalents.)

| EdgeLane need | Atlas call (former) | Tradier equivalent | Notes |
|---|---|---|---|
| Spot price | `get_stock_quote` | `GET /v1/markets/quotes?symbols=NVDA` | Direct map. Response: `quotes.quote.last`. |
| Options chain for one expiration | `get_options_chain` | `GET /v1/markets/options/chains?symbol=NVDA&expiration=2026-05-23&greeks=true` | Direct map. Per-contract fields: `strike, option_type, bid, ask, last, volume, open_interest, greeks{delta, gamma, theta, vega, bid_iv, mid_iv, ask_iv, smv_vol}`. |
| Expiration list | `Option-Expiration-Dates` | `GET /v1/markets/options/expirations?symbol=NVDA&includeAllRoots=true` | Direct map. Response: `expirations.date[]`. |
| Strikes for an expiration | (we derive from chain) | `GET /v1/markets/options/strikes` | Available if useful, not strictly needed. |
| **Dealer GEX rollup, walls** | `analyze_greek_exposures` | **None — compute locally** | Critical gap. Aggregator built client-side from chain data. |
| Per-expiration GEX | `Greek-Exposure-Single-Expiration` | Compute from per-expiration chain call | Same path as above. |
| Subscription / quota | `get_subscription_status` | Rate-limit headers (`X-Ratelimit-*`) on every response | We'd repurpose the quota pill to show remaining-this-minute. |
| Historical OHLC | (we don't currently use) | `GET /v1/markets/history` | Bonus for future. |
| Time-and-sales | (we don't currently use) | `GET /v1/markets/timesales` | Bonus for future. |
| Streaming quotes | (we don't currently use) | WebSocket `wss://ws.tradier.com/v1/` | Bonus — could enable live spot ticker. |
| User profile (auth verify) | implicit via any call | `GET /v1/user/profile` | Cheap test that the token works. |

**Coverage assessment:** every blocking field EdgeLane consumes today maps to Tradier, **except** the pre-computed dealer GEX rollup. That gap is the only real engineering work.

---

## 4. The dealer-GEX gap — solution sketch

EdgeLane previously consumed from Atlas's `analyze_greek_exposures`:

- `exposures_by_date[date].by_strike[].net_gex` / `net_dex`
- `portfolio_totals.net_gex` / `net_dex`
- `key_levels.call_wall.{strike, gex}`
- `key_levels.put_wall.{strike, gex}`

Tradier's options chain gives us per-contract `gamma`, `delta`, `theta`, `vega`, `open_interest` directly. The aggregation math is standard:

```
dealer_gamma_per_strike(K) = (call_gamma(K) × call_OI(K) − put_gamma(K) × put_OI(K)) × 100 × spot²
dealer_delta_per_strike(K) = (call_delta(K) × call_OI(K) − put_delta(K) × put_OI(K)) × 100 × spot

call_wall = arg max over strikes K>spot of dealer_gamma_per_strike(K)
put_wall  = arg min over strikes K<spot of dealer_gamma_per_strike(K)
net_gex   = Σ dealer_gamma_per_strike(K) for K in chain
```

(The convention is that retail buys options → market makers are short options → MM gamma exposure flips sign by side.)

This computation is fast (~milliseconds for 1,000 contracts), runs entirely client-side, and reuses the chain we already fetch for the optimizer. **Net effect: bias detection became 1 Tradier call (chain) instead of the former 2 Atlas calls (quote + greek_exposures). Heavy symbols like MU stop being a problem entirely** because we never call a slow aggregation endpoint — we just iterate the chain we already have.

**Trade-off:** Atlas's `key_levels` likely used a more sophisticated wall-detection algorithm than naive max-magnitude. Our local version is defensible but may have differed from Atlas's output on edge cases (very flat exposure profiles, dispersed OI). For the symbols we typically trade (liquid index / mega-cap names), the difference should be small.

---

## 5. Architecture — provider abstraction

Single-file JSX deployment means we can't truly split into modules, but we can introduce a clean namespace pattern:

```javascript
// === Data provider abstraction ===
//
// Two providers (atlas, tradier) implement the same interface. Build script
// reads DATA_PROVIDER from config and exposes window.DATA_PROVIDER. Downstream
// code uses `dataProvider.*` instead of provider-specific names.

const atlas = {
  stockQuote(symbol) { ... },
  optionsChain(symbol, expiration) { ... },
  optionExpirations(symbol) { ... },
  greekExposures(symbol, num) { ... },     // → calls analyze_greek_exposures
  subscriptionStatus() { ... },
};

const tradier = {
  stockQuote(symbol) { ... },               // → GET /v1/markets/quotes
  optionsChain(symbol, expiration) { ... }, // → GET /v1/markets/options/chains
  optionExpirations(symbol) { ... },        // → GET /v1/markets/options/expirations
  greekExposures(symbol, num) {             // → fetch N chains in parallel, aggregate locally
    return _computeGreekExposuresFromChains(symbol, num);
  },
  subscriptionStatus() {                    // → uses rate-limit headers from last call
    return _ratelimitFromHeaders;
  },
};

const dataProvider = window.DATA_PROVIDER === 'tradier' ? tradier : atlas;

// Downstream code is provider-agnostic:
const [quote, greeksRaw] = await Promise.all([
  dataProvider.stockQuote(sym),
  dataProvider.greekExposures(sym, 3),
]);
```

Each provider returns the **same shape** to its callers, so `_findGexWall`, `_computeBiasSignals`, `_aggregateExposures` etc. don't change at all. We're swapping the *source* of `exposures_by_date` / `portfolio_totals` / `key_levels`, not the consumers.

### Python tests

Same pattern — single `data_provider` module with `atlas` and `tradier` namespaces. `tests/utils.py` picks one based on config.

### Folder layout

```
EdgeLane/
├── spread_optimizer_v4_7_html.jsx       (current; adds tradier namespace)
├── edge_lane_config.config              (adds DATA_PROVIDER + TRADIER_TOKEN)
├── edge_lane.template.html              (adds __TRADIER_TOKEN__, __TRADIER_BASE_URL__, __DATA_PROVIDER__ placeholders)
├── edge_lane_build.sh                   (no structural change, just new placeholders)
├── data_providers/                      NEW — Python provider implementations
│   ├── __init__.py                      Factory: get_provider(name) → Atlas | Tradier
│   ├── atlas.py                         Current atlas_call() refactored here
│   ├── tradier.py                       NEW — Tradier REST client
│   └── gex_local.py                     NEW — local dealer-GEX aggregation
├── tests/
│   ├── utils.py                         (delegates to data_providers/)
│   ├── tradier_smoke.py                 NEW — parallel to atlas_rest_smoke.py
│   └── tradier_chunked_probe.py         NEW — same shape as atlas_chunked_probe.py
└── docs/
    ├── tradier_integration_proposal.md  (this file)
    ├── tradier_design.md                Created after approval
    └── tradier_implementation.md        Created after approval
```

---

## 6. Config wiring

Additions to `edge_lane_config.config.example`:

```bash
# Provider selection
DATA_PROVIDER=tradier

# Tradier — sandbox or production
TRADIER_ACCESS_TOKEN="your-token-here"
TRADIER_BASE_URL="https://api.tradier.com"   # production
# TRADIER_BASE_URL="https://sandbox.tradier.com"   # sandbox
```

Build script (`edge_lane_build.sh`) adds three new placeholder substitutions:
- `__TRADIER_TOKEN__`
- `__TRADIER_BASE_URL__`
- `__DATA_PROVIDER__`

Switching providers is a one-line config edit + rebuild. No code changes per switch.

---

## 7. Risks, unknowns, decisions to make

### Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Local GEX/wall numbers differ from Atlas's output | Medium | Probe both side-by-side on the same chain; document the deltas; tune local algo to match within tolerance |
| ORATS-sourced greeks lag bid/ask by ~1 hour | Low-Medium | Document this; consider falling back to BS-computed greeks from live IV for intraday-critical use |
| Real-time data requires funded Tradier account | High (cost) | You decide: use sandbox (15-min delayed) for development, decide on funding later |
| Token expiry (24h via OAuth) | Low | Personal tokens (static, never expire) are fine for our use; OAuth flow only needed for multi-user |
| One-stream-per-user limit | Low | Not relevant until we add streaming (Phase 2+) |
| Tradier docs lack recipes for spreads | Low | We have all endpoint specs; minor friction for future extensions |

### Open questions for you to decide

1. **Sandbox vs production initially?** Sandbox is free + 15-min delayed; production needs a funded brokerage account. I'd start with sandbox to wire the integration, then switch via config when you fund.

2. **Run Atlas + Tradier in parallel (A/B mode) or hard-switch?** Two options:
   - **Hard-switch via config**: simplest. Set `DATA_PROVIDER=tradier`, rebuild, verify, done.
   - **A/B mode**: load both providers, run side-by-side, compare wall/bias outputs cell-by-cell. Costs 2× quota during the comparison window. Useful if you don't fully trust the local GEX math yet.

3. **Should we keep `Atlas` as the fallback provider after Tradier ships?** I'd lean yes for the first month — gives us a known-good reference if Tradier returns surprising bias numbers. After confidence is high, retire the Atlas client entirely.

4. **Streaming quotes — yes/no in Phase 1?** Tradier has WebSocket. We could replace the manual "Reload" button with a live-updating spot ticker. Probably better as Phase 2 — get the REST path stable first.

5. **Token storage in browser.** Same as today's Atlas key — baked into the HTML at build time via the template. We don't store anything client-side beyond what's already there. Confirm this is acceptable for your deployment model (single-user local HTML, not a hosted multi-tenant app).

---

## 8. Phased implementation plan (post-approval)

### Phase 1 — Foundation (no UI changes visible to user)
- Create `data_providers/` folder; refactor existing `tests/utils.py` Atlas code into `data_providers/atlas.py`
- Build `data_providers/tradier.py` REST client (quotes, options-chain, expirations)
- Build `data_providers/gex_local.py` — local dealer-GEX aggregator with `_computeGreekExposuresFromChains()`
- Add `DATA_PROVIDER` switch to config + template
- Tradier sandbox smoke test (`tests/tradier_smoke.py`) — verify auth, parse quote, parse chain
- Side-by-side probe: `tests/tradier_vs_atlas.py` runs both, diffs wall/bias outputs on a couple symbols
- **Acceptance criteria for Phase 1:** Tradier sandbox quote + chain works; local GEX wall on SPY agrees with Atlas's wall within 1 strike

### Phase 2 — JSX integration
- Add `tradier` namespace in JSX mirroring `atlas`'s shape
- Add `dataProvider` selector based on `window.DATA_PROVIDER`
- Test rebuild with `DATA_PROVIDER=tradier`, verify bias detection works end-to-end
- Hide Atlas-specific UI elements (quota pill semantics change to "rate limit remaining this minute")
- **Acceptance criteria for Phase 2:** Tickets tab + Lookup tab render correctly with Tradier data on SPY, NVDA, and MU

### Phase 3 — Cleanup + docs
- Update `operating_manual.md` with Tradier-specific operational notes (already partially done for v4.7.20+ changes)
- Write `tradier_design.md` (architecture decisions) and `tradier_implementation.md` (file-by-file map)
- Decide whether to retain Atlas as fallback or retire it
- Production rollout: fund brokerage account, swap `TRADIER_BASE_URL` to production, retest

### Phase 4 (optional, future) — Streaming + multi-leg orders
- WebSocket spot ticker on the header
- Replace SnapTrade-based copy/push with Tradier's native multi-leg order placement
- Account balance / positions on the trade ticket card

---

## 9. What I need from you to proceed

1. **Sign off on the approach** (or push back on specific pieces).
2. **Decide sandbox vs production for development** (recommend sandbox first).
3. **A/B mode vs hard-switch?** (recommend hard-switch with `DATA_PROVIDER` flag — easy to flip back).
4. **OK to keep Atlas client present as fallback** in the JSX for now?
5. **Any concern about the local GEX math** vs Atlas's pre-computed walls?

Once approved, I'll:
- Write `docs/tradier_design.md` with detailed schema mappings and edge-case handling
- Write `docs/tradier_implementation.md` with file-by-file delta from current code
- Begin Phase 1 build

---

## 10. Quick references

- Tradier docs home: https://docs.tradier.com/
- API reference root: https://docs.tradier.com/reference/
- Sandbox token signup: https://web.tradier.com/user/api
- Rate limits: https://docs.tradier.com/docs/rate-limiting
- Streaming guide: https://docs.tradier.com/docs/streaming-data
- Internal: `operating_manual.md` (updated for v4.7.20–24)
