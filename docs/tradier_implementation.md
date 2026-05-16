# Tradier Integration — Implementation Plan

**Status:** Follows `tradier_design.md`. Phase 1 + Phase 2 scope.
**Audience:** The next agent (or human) actually making the edits.

---

## 1. Overview

This document is the **how/where/what** for the Tradier swap. The architectural
"why" (provider abstraction, derived-state GEX, the validation gate) lives in
`tradier_design.md`; the vendor rationale lives in
`tradier_integration_proposal.md`. Don't re-litigate those decisions here.

This file lists every file that needs touching, what changes in each one, and
the order in which to touch them so the working build never regresses. It also
specifies the exact bash and HTML edits needed to wire three new config keys
through to `window.*` globals, and the acceptance shape for the two new smoke
tests that gate the migration.

---

## 2. File-by-file delta

### 2.1 `spread_optimizer_v4_7_html.jsx` (MODIFIED, ~250 lines added)

The single-file JSX bundle. Five distinct edits, all inside the existing
namespacing — no module split.

**A. Add `tradier` namespace** alongside `atlas`, plus a `_tradierCall` helper
mirroring `_atlasCall`'s retry / abort / error-shape contract.

Diff anchor (insert immediately **after** the closing `};` of `const atlas`):

```javascript
};

// ==============================================
// DETERMINISTIC BIAS ENGINE (v4.7 hybrid)
// ==============================================
```

Insert before the `DETERMINISTIC BIAS ENGINE` banner. New code:

- `_tradierCall(path, params, opts)` — `GET ${TRADIER_BASE_URL}${path}` with
  `Authorization: Bearer ${TRADIER_TOKEN}`, `Accept: application/json`.
  Same 75s timeout, same `_shouldRetry` / `_backoff` policy as `_atlasCall`.
  Throws errors prefixed `Provider <method>:` per design Section 3.
- `_ratelimitFromHeaders` — module-level mutable object updated from each
  response's `X-Ratelimit-Available` / `X-Ratelimit-Allowed` / `X-Ratelimit-Expiry`.
- `const tradier = { stockQuote, optionsChain, optionExpirations, greekExposures,
  subscriptionStatus }` — five methods returning the **exact** shapes from
  design Section 3. `greekExposures` is the load-bearing one: it fan-outs N
  parallel `optionsChain` calls and feeds each to `_computeDealerExposures`.

**B. Add `_computeDealerExposures(contracts, spot, opts?)`** — pure function,
mirror of `data_providers/gex_local.py`. Math + wall selection per design
Section 5. Insert immediately **before** `_strikeRowsFrom` so all bias helpers
sit together.

Diff anchor:

```javascript
// Walk the per-strike rows from Atlas's exposures_by_date entry. The shape
// isn't perfectly documented; try common keys.
function _strikeRowsFrom(exposuresForChosen) {
```

Insert above this block.

**C. Add `dataProvider` selector.** One line, placed immediately after the
`const tradier = { ... };` block so both namespaces exist when it resolves:

```javascript
const dataProvider = (typeof window !== 'undefined' && window.DATA_PROVIDER === 'tradier')
  ? tradier : atlas;
```

**D. Switch `detectBias` to `dataProvider`.** Current code uses
`atlas.stockQuote(sym)` and `_getGreekExposures(sym, 3)`. Replace with
`dataProvider.stockQuote(sym)` and `dataProvider.greekExposures(sym, 3)`. The
existing `_getGreekExposures` helper (adaptive denylist + chunked path,
v4.7.23) stays in place; under the hood, `atlas.greekExposures` delegates to
it so the Atlas path is byte-identical to today.

Diff anchor (lines ~2068–2073):

```javascript
      // STEP 1: REST fetch — quote in parallel with adaptive greek-exposures
      // (fast path for liquid symbols, chunked+cached for heavy chains).
      const [quote, greeksRaw] = await Promise.all([
        atlas.stockQuote(sym),
        _getGreekExposures(sym, 3),
      ]);
```

Replace `atlas.stockQuote(sym)` → `dataProvider.stockQuote(sym)` and
`_getGreekExposures(sym, 3)` → `dataProvider.greekExposures(sym, 3)`.

Also update the `refreshQuota` callback (line ~2009) — swap
`atlas.getSubscriptionStatus()` for `dataProvider.subscriptionStatus()`. Atlas's
returns the existing quota envelope; Tradier's returns
`{ provider: 'tradier', rate_remaining, rate_limit, rate_reset }`. The quota
pill renderer needs a small branch on `parsed.provider` — document but
don't gold-plate; minimal change is "show rate_remaining/rate_limit when
present, else fall back to current Atlas formatter."

**E. `_normalizeContract` — add `expiration` field.** Required by the
aggregator to bucket per-strike rows by date. Additive change; no existing
consumer reads it.

Diff anchor (lines ~237–257):

```javascript
function _normalizeContract(c, sideHint) {
  const sideSrc = String(c.side || c.type || c.option_type || c.contract_type || sideHint || '').toLowerCase();
  ...
  return {
    strike: _num(c.strike ?? c.strike_price ?? c.strikePrice),
    side,
```

Change the signature to `_normalizeContract(c, sideHint, sideContext)` and add
in the return object:

```javascript
    expiration: String(c.expiration ?? c.expiration_date ?? sideContext?.expiration ?? ''),
```

`_flattenChain`'s recursive `walk` already knows the wrapping group's
expiration — pass it through as `sideContext = { expiration: exp }` in the
`node.calls.forEach` / `node.puts.forEach` calls.

**F. Add `gexState` useMemo** inside `SpreadOptimizer`. Placed near the
existing `candidates` useMemo (line ~2039) so React deps live together.

```javascript
const gexState = useMemo(() => {
  if (!chainData?.contracts || !spotPrice) return null;
  return _computeDealerExposures(chainData.contracts, spotPrice);
}, [chainData, spotPrice]);
```

Wire-in is intentionally minimal in this PR — `detectBias` still uses
`dataProvider.greekExposures(...)` (which under Tradier already calls the
aggregator). `gexState` exists as a hook for the future Lookup-tab tile-level
GEX overlay and for a downstream PR that removes the redundant fan-out fetch
when the chain is already cached.

---

### 2.2 `edge_lane.template.html` (MODIFIED, +3 lines)

Three new placeholders next to the existing `window.ATLAS_KEY` block.

Diff anchor (lines ~69–76):

```html
    window.ATLAS_KEY     = '__ATLAS_KEY__';
    window.ANTHROPIC_KEY = '__ANTHROPIC_KEY__';
    window.GEMINI_API_KEY = '__GEMINI_API_KEY__';
    window.GEMINI_MODEL   = '__GEMINI_MODEL__';
    // Optional: route through a CORS-enabled proxy. Empty string = direct.
    window.ATLAS_BASE_URL  = '__ATLAS_BASE_URL__' || undefined;
    window.GEMINI_BASE_URL = '__GEMINI_BASE_URL__' || undefined;
```

Add immediately after the last `window.GEMINI_BASE_URL` line:

```html
    // Tradier — provider selection + auth. DATA_PROVIDER picks the namespace
    // at runtime; the value is set in edge_lane_config.config and baked in here.
    window.DATA_PROVIDER     = '__DATA_PROVIDER__' || 'atlas';
    window.TRADIER_TOKEN     = '__TRADIER_TOKEN__';
    window.TRADIER_BASE_URL  = '__TRADIER_BASE_URL__' || 'https://api.tradier.com';
```

---

### 2.3 `edge_lane_build.sh` (MODIFIED, +6 lines)

Three new required-vars and three new substitution lines.

Diff anchor (lines ~89–95):

```bash
: "${ATLAS_KEY:?ATLAS_KEY not set in $CONFIG}"
: "${ANTHROPIC_KEY:?ANTHROPIC_KEY not set in $CONFIG}"
: "${GEMINI_API_KEY:?GEMINI_API_KEY not set in $CONFIG}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"  # default if not in config
ATLAS_BASE_URL="${ATLAS_BASE_URL:-}"     # empty = direct (will CORS-fail from browser)
GEMINI_BASE_URL="${GEMINI_BASE_URL:-}"   # empty = direct
```

Append:

```bash
DATA_PROVIDER="${DATA_PROVIDER:-atlas}"             # 'atlas' or 'tradier'
TRADIER_ACCESS_TOKEN="${TRADIER_ACCESS_TOKEN:-}"    # empty OK when DATA_PROVIDER=atlas
TRADIER_BASE_URL="${TRADIER_BASE_URL:-https://api.tradier.com}"
# Fail-fast: if Tradier is the chosen provider, the token must be set.
if [[ "$DATA_PROVIDER" == "tradier" && -z "$TRADIER_ACCESS_TOKEN" ]]; then
  echo "✗ DATA_PROVIDER=tradier but TRADIER_ACCESS_TOKEN is empty in $CONFIG" >&2
  exit 1
fi
```

Diff anchor (lines ~144–152, the substitution chain):

```bash
OUTPUT="${TEMPLATE_CONTENT//__JSX_BODY__/$JSX_BODY}"
OUTPUT="${OUTPUT//__ATLAS_KEY__/$ATLAS_KEY}"
...
OUTPUT="${OUTPUT//__BIAS_NARRATIVE_OPEN_DEFAULT__/$BIAS_NARRATIVE_OPEN_DEFAULT}"
```

Append after the last `OUTPUT=` line:

```bash
OUTPUT="${OUTPUT//__DATA_PROVIDER__/$DATA_PROVIDER}"
OUTPUT="${OUTPUT//__TRADIER_TOKEN__/$TRADIER_ACCESS_TOKEN}"
OUTPUT="${OUTPUT//__TRADIER_BASE_URL__/$TRADIER_BASE_URL}"
```

Also extend the dry-run summary block (lines ~163–173) to print
`DATA_PROVIDER`, `TRADIER_BASE_URL`, and `mask "$TRADIER_ACCESS_TOKEN"`. Extend
the `REMAINING` placeholder regex to include
`DATA_PROVIDER|TRADIER_TOKEN|TRADIER_BASE_URL`.

---

### 2.4 `edge_lane_config.config.example` (MODIFIED, +12 lines)

Diff anchor (lines ~17–23):

```bash
# Atlas (mind-vest.io) — get one at https://mind-vest.io/dashboard
ATLAS_KEY="sk_atlas_REPLACE_ME"

# Anthropic — get one at https://console.anthropic.com/settings/keys
# Used by detectBias to call the Messages API + atlas-mcp via mcp_servers.
ANTHROPIC_KEY="sk-ant-REPLACE_ME"
```

Insert before the `Atlas` block:

```bash
# Data provider — 'atlas' or 'tradier'. Default 'atlas' for backward compat;
# flip to 'tradier' after tests/tradier_vs_atlas_probe.py passes on SPY/NVDA/MU.
DATA_PROVIDER=atlas

# Tradier — sandbox is free + 15-min delayed; production needs a funded
# brokerage account. Flip BASE_URL to swap environments; token must match.
TRADIER_ACCESS_TOKEN="tradier_REPLACE_ME"
TRADIER_BASE_URL="https://api.tradier.com"          # production
# TRADIER_BASE_URL="https://sandbox.tradier.com"    # sandbox
```

---

### 2.5 `data_providers/__init__.py` (NEW, ~30 lines)

Factory module. Single public function:

```python
from .atlas import AtlasProvider
from .tradier import TradierProvider

def get_provider(name: str, cfg: dict) -> "Provider":
    name = (name or "atlas").lower()
    if name == "atlas":   return AtlasProvider(cfg["ATLAS_KEY"], cfg.get("ATLAS_BASE_URL"))
    if name == "tradier": return TradierProvider(cfg["TRADIER_ACCESS_TOKEN"], cfg.get("TRADIER_BASE_URL"))
    raise ValueError(f"unknown provider: {name}")
```

Both provider classes expose the same five methods as the JSX contract,
returning `dict`s shaped per design Section 3.

---

### 2.6 `data_providers/atlas.py` (NEW, ~120 lines)

Move the existing `atlas_call()` body and its retry helpers out of
`tests/utils.py` into this module, wrapped as a class:

```python
class AtlasProvider:
    def __init__(self, key: str, base_url: str | None = None): ...
    def stock_quote(self, symbol: str) -> dict: ...
    def options_chain(self, symbol: str, expiration: str, **opts) -> dict: ...
    def option_expirations(self, symbol: str) -> dict: ...
    def greek_exposures(self, symbol: str, num: int = 3) -> dict: ...
    def subscription_status(self) -> dict: ...
```

`tests/utils.py` re-exports the legacy `atlas_call` function from this module
so existing test scripts (`atlas_rest_smoke.py`, `e2e_pipeline.py`,
`atlas_subscription.py`, `atlas_chunked_probe.py`) keep working unchanged.

---

### 2.7 `data_providers/tradier.py` (NEW, ~180 lines)

Tradier REST client. Five public methods matching the interface from §2.5.

Endpoint mappings (per design §4):

| Method                     | HTTP                                                      |
|---|---|
| `stock_quote(sym)`         | `GET /v1/markets/quotes?symbols={sym}`                    |
| `options_chain(sym, exp)`  | `GET /v1/markets/options/chains?symbol={sym}&expiration={exp}&greeks=true` |
| `option_expirations(sym)`  | `GET /v1/markets/options/expirations?symbol={sym}&includeAllRoots=true` |
| `user_profile()`           | `GET /v1/user/profile` (smoke-test convenience method)    |
| `greek_exposures(sym, n)`  | Fans out N parallel `options_chain` calls + aggregates via `gex_local.compute_dealer_exposures` |

Internal helper `_call(path, params, timeout=75, max_retries=3)`:

- `Authorization: Bearer <token>`, `Accept: application/json`
- Reads `X-Ratelimit-Available` / `X-Ratelimit-Allowed` / `X-Ratelimit-Expiry`
  off every response into `self._last_ratelimit`
- Same retry policy as `atlas_call` (429 / 5xx / network / timeout)
- Tradier error envelope: `{ "fault": { "faultstring": ..., "detail": {...} } }`
  → raise `TradierError(f"Provider {path}: {fault.faultstring}")`

Normalization helper `_normalize_contract(raw)` converts Tradier's per-option
dict into the shape from design §4 (notably: `iv = greeks.mid_iv * 100` to
align units with Atlas; carry `expiration_date` → `expiration`).

---

### 2.8 `data_providers/gex_local.py` (NEW, ~140 lines)

Pure function `compute_dealer_exposures(contracts, spot, opts=None) -> dict`.
Python mirror of the JSX `_computeDealerExposures`. Same math, same wall
selection, same edge-case handling per design §5. Return shape exactly matches
the `greek_exposures` interface. Used by:

- `TradierProvider.greek_exposures` to convert fan-out chain results into the
  Atlas-compatible envelope
- `tests/tradier_vs_atlas_probe.py` to compute the side-by-side reference
- (Future) any Python script that wants dealer exposures from a raw chain

No I/O, no globals, no side effects. Pure Python + stdlib.

---

### 2.9 `tests/utils.py` (MODIFIED, ~30 lines)

Keep the public surface (`atlas_call`, `gemini_call`, `load_config`,
`require_keys`, `stopwatch`) stable. Under the hood:

- Import `atlas_call` from `data_providers.atlas` and re-export.
- New helper `def get_provider_from_config(cfg)` that returns the configured
  provider instance — usable from any new test.

No behavioral change for existing scripts.

---

### 2.10 `tests/tradier_smoke.py` (NEW, ~120 lines)

Mirror of `tests/atlas_rest_smoke.py`. Hits four endpoints in sequence, prints
a pass/fail line per step + total elapsed. Exits non-zero on any failure.

Steps:

1. `user_profile` — verifies auth + parses an account ID
2. `stock_quote("SPY")` — verifies REST shape parsing
3. `option_expirations("SPY")` — verifies expirations array
4. `options_chain("SPY", first_expiration, greeks=True)` — verifies greeks present

---

### 2.11 `tests/tradier_vs_atlas_probe.py` (NEW, ~200 lines)

The **migration gate** from design §7. Loops `['SPY', 'NVDA', 'MU']`, pulls
the next standard monthly expiration via both providers within a 60-second
window, runs `compute_dealer_exposures` on the Tradier chain, and diffs
against Atlas's `analyze_greek_exposures` output:

| Metric                       | Tolerance                |
|---|---|
| `call_wall.strike`           | within 1 strike step     |
| `put_wall.strike`            | within 1 strike step     |
| `portfolio_totals.net_gex`   | within ±15% magnitude    |

Writes `tests/probe_results.csv` (one row per `(symbol, run, metric)`) and
prints a per-symbol PASS/FAIL banner. Exits non-zero on any failure.

---

## 3. Order of operations

Touch files in this order so the existing build keeps producing v4.7.x bundles
the whole time. Every step before step 6 is invisible to end users.

1. **`data_providers/` folder + `gex_local.py` + `tradier.py` + `atlas.py` +
   `__init__.py`.** Pure additions; nothing existing imports from here yet.
2. **`tests/tradier_smoke.py` + `tests/tradier_vs_atlas_probe.py`.** Exercises
   the new code path. Existing test scripts continue using `tests/utils.py`'s
   legacy surface.
3. **(GATE) Run the probe locally.** Requires pass on SPY + NVDA + MU per
   design §7 tolerances. If any symbol class fails by more than the tolerance,
   stop and tune `gex_local.py` (sign convention is the usual culprit) before
   continuing.
4. **JSX additions only (no swaps yet).** Add `tradier` namespace,
   `_computeDealerExposures`, `_tradierCall`, `dataProvider` selector,
   `gexState` useMemo. `detectBias` and `refreshQuota` still call `atlas.*`
   directly — the new code paths are present but inert. Rebuild and verify
   nothing visibly changed.
5. **JSX swap.** Replace `atlas.stockQuote` / `_getGreekExposures` /
   `atlas.getSubscriptionStatus` in `detectBias` and `refreshQuota` with
   `dataProvider.*`. With `DATA_PROVIDER=atlas` (default), behavior is
   identical to step 4.
6. **Template + build script + config.example.** Add the three placeholders +
   substitution lines + example config block. Rebuild — still
   `DATA_PROVIDER=atlas`, still identical output.
7. **Flip the flag.** Set `DATA_PROVIDER=tradier` in
   `edge_lane_config.config`, rebuild, hard-reload the page. Verify Tickets
   tab + Lookup tab on SPY, NVDA, MU. This is the user-visible cutover.
8. **(Later, after a stable period) Retire Atlas.** Remove the `atlas`
   namespace from JSX, drop `data_providers/atlas.py` (move `atlas_call` back
   inline if any test still needs it), simplify the build-script fail-fast.
   This is a separate PR; do not bundle it with the cutover.

---

## 4. Build-script change spec

The bash diff for `edge_lane_build.sh` in full (after line ~95, replacing the
trailing comment-only lines):

```bash
DATA_PROVIDER="${DATA_PROVIDER:-atlas}"
TRADIER_ACCESS_TOKEN="${TRADIER_ACCESS_TOKEN:-}"
TRADIER_BASE_URL="${TRADIER_BASE_URL:-https://api.tradier.com}"
if [[ "$DATA_PROVIDER" == "tradier" && -z "$TRADIER_ACCESS_TOKEN" ]]; then
  echo "✗ DATA_PROVIDER=tradier but TRADIER_ACCESS_TOKEN is empty in $CONFIG" >&2
  exit 1
fi
```

After line ~152 (the last `OUTPUT=` substitution):

```bash
OUTPUT="${OUTPUT//__DATA_PROVIDER__/$DATA_PROVIDER}"
OUTPUT="${OUTPUT//__TRADIER_TOKEN__/$TRADIER_ACCESS_TOKEN}"
OUTPUT="${OUTPUT//__TRADIER_BASE_URL__/$TRADIER_BASE_URL}"
```

Dry-run summary additions (after line ~167):

```bash
  printf "  %-20s = %s\n" "DATA_PROVIDER"        "$DATA_PROVIDER"
  printf "  %-20s = %s\n" "TRADIER_TOKEN"        "$(mask "$TRADIER_ACCESS_TOKEN")"
  printf "  %-20s = %s\n" "TRADIER_BASE_URL"     "$TRADIER_BASE_URL"
```

Extend the placeholder grep on line ~173 to include the new placeholders:

```bash
REMAINING=$(printf '%s' "$OUTPUT" | grep -cE '__(JSX_BODY|ATLAS_KEY|ANTHROPIC_KEY|GEMINI_API_KEY|GEMINI_MODEL|DATA_PROVIDER|TRADIER_TOKEN|TRADIER_BASE_URL)__' || true)
```

The HTML template diff is the single insertion block shown in §2.2 above.

---

## 5. Smoke-test recipes

### `tests/tradier_smoke.py`

Expected output on success (sandbox):

```
Tradier smoke - sandbox
[1/4] user.profile      → ✓ 0.4s — id: id-abc123...
[2/4] quote(SPY)        → ✓ 0.6s — $580.21 (last)
[3/4] expirations(SPY)  → ✓ 0.5s — 14 dates, front: 2026-05-23
[4/4] chain(SPY, 2026-05-23, greeks=true)  → ✓ 1.1s — 184 contracts, IV present
SUMMARY: 4/4 ok
```

Failure pattern (auth bad):

```
[1/4] user.profile      → ✗ 0.2s — TradierError: Provider /v1/user/profile: HTTP 401: ...
SUMMARY: 0/4 ok — aborting subsequent steps
```

### `tests/tradier_vs_atlas_probe.py`

Expected output on success (3-symbol pass):

```
Tradier vs Atlas — validation probe (2026-05-16 09:42:03 ET)
target expiration: 2026-06-19 (next standard monthly)

[1/3] SPY
  atlas:    call_wall=600  put_wall=575  net_gex=+8.4e9
  tradier:  call_wall=600  put_wall=575  net_gex=+7.9e9
  Δ:        call_wall=0    put_wall=0    net_gex=-5.9%   → PASS

[2/3] NVDA
  atlas:    call_wall=145  put_wall=130  net_gex=-1.2e9
  tradier:  call_wall=145  put_wall=130  net_gex=-1.1e9
  Δ:        call_wall=0    put_wall=0    net_gex=-8.3%   → PASS

[3/3] MU
  atlas:    call_wall=110  put_wall=95   net_gex=+340e6
  tradier:  call_wall=110  put_wall=95   net_gex=+310e6
  Δ:        call_wall=0    put_wall=0    net_gex=-8.8%   → PASS

SUMMARY: 3/3 pass — migration gate satisfied.
results CSV: tests/probe_results.csv
```

Failure pattern (sign-convention inverted):

```
[1/3] SPY
  atlas:    net_gex=+8.4e9
  tradier:  net_gex=-8.2e9
  Δ:        net_gex=-195%                                → FAIL (sign mismatch)
... tune sign in gex_local.compute_dealer_exposures and rerun.
```

---

## 6. Rollout + rollback

**Switch FROM Atlas TO Tradier (post step 6):**

```bash
$ sed -i 's/^DATA_PROVIDER=.*/DATA_PROVIDER=tradier/' edge_lane_config.config
$ ./edge_lane_build.sh
$ # hard-reload edge_lane.html in browser
```

**Switch BACK:** same operation, flip the value to `atlas`. Both namespaces
remain present in the JSX bundle, so no rebuild-from-scratch needed beyond
running the build script with the flag flipped.

**Per-symbol fallback (if the probe fails for a symbol class).** The hook is
inside `tradier.greekExposures` in the JSX. Pseudocode:

```javascript
async greekExposures(symbol, num) {
  const fallback = (window.ATLAS_FALLBACK_SYMBOLS || '').split(',').map(s => s.trim().toUpperCase());
  if (fallback.includes(symbol.toUpperCase())) {
    return atlas.analyzeGreekExposures(symbol, num);
  }
  // ...fan out N options_chain calls + _computeDealerExposures...
}
```

The `ATLAS_FALLBACK_SYMBOLS` config key (CSV, e.g. `"MU,SMCI"`) and matching
template/build placeholder follow the same wiring pattern as the three core
keys. Add only when needed — Phase 1's default is empty.

---

## 7. Estimated effort

| Workstream                          | Files                                       | Estimated hours |
|---|---|---|
| GEX aggregator (Python + JSX)       | `gex_local.py`, JSX `_computeDealerExposures` | 4 |
| Tradier REST client (Python)        | `tradier.py`                                  | 3 |
| Provider factory + Atlas refactor   | `data_providers/__init__.py`, `atlas.py`, `tests/utils.py` | 2 |
| Smoke + validation probe            | `tradier_smoke.py`, `tradier_vs_atlas_probe.py` | 3 |
| JSX `tradier` namespace + selector  | JSX                                           | 3 |
| `_normalizeContract` expiration field + `gexState` useMemo | JSX | 1 |
| Template + build script wiring      | template, `edge_lane_build.sh`, config.example | 1 |
| Manual verification (SPY/NVDA/MU)   | live testing                                  | 2 |
| **Total**                           |                                               | **19 h** |

Estimate assumes the validation probe passes on first or second tune (sign
convention is the only likely calibration knob). Add ~4 h if a symbol class
fails and needs the per-symbol fallback hook wired in.

---

## 8. Conflicts and notes for the next agent

- `atlas.analyzeGreekExposures` (line ~291) is the **only** consumer-visible
  Atlas method that is not called via the new `dataProvider`. It's used
  internally by `_getGreekExposures` (the v4.7.23 adaptive helper). Leave it.
  The new `atlas.greekExposures(sym, n)` in step 4 above is a **thin wrapper**
  that calls `_getGreekExposures(sym, n)` — same code path, different name to
  satisfy the provider interface. No renaming of the existing
  `analyzeGreekExposures` function.
- `atlas.getSubscriptionStatus` becomes `atlas.subscriptionStatus` to match
  the new interface. Update the one call site in `refreshQuota`. The
  underlying tool name (`'get_subscription_status'`) is unchanged.
- `_normalizeContract`'s signature gains a third parameter. All current call
  sites pass two args; the new one is optional and defaults to `undefined`,
  so callers that don't pass `sideContext` continue to work (the field just
  comes back as the empty string for Atlas calls that don't thread expiration
  through). The Tradier path always knows the expiration since
  `options/chains` is a single-expiration endpoint.
