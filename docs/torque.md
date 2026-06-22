# Torque — fast multi-leg order builder

Torque is a standalone web page served from the EdgeLane **market** backend. It's
the place-order dialog pulled out of the main JSX app into its own fast,
auto-filling advanced order menu. It does **no** bias detection or strategy
picking — you choose a ticker + one strategy, it auto-fills the legs, prices them
live, and places the order (optionally with an auto-close profit target).

## Run

It rides on the existing market backend — no separate process.

```bash
cd market/backend
make run-dev     # sandbox Tradier (paper, safe) — recommended first
make run-prod    # production Tradier (real money)
make run         # boot with whatever DEVMODE is already in the config
```

Then open **http://127.0.0.1:8789/torque** (port = `HTTP_PORT`, default 8788; the
`make` targets use 8789). The env badge top-left shows **SANDBOX** (gold) or
**PRODUCTION** (red) so you always know whether orders are real.

### Config file + DEVMODE

Torque reads **`edgelane_market.config`** (the market-backend config) — *not*
`edge_lane_config.config`, which belongs to the old standalone frontend and has its
own unrelated `DEVMODE`. The single flag that decides sandbox vs production for
Torque is **`DEVMODE`** in `edgelane_market.config`:

- `DEVMODE=true` → sandbox token + `sandbox.tradier.com` + sandbox DB (paper).
- `DEVMODE=false` → production token + `api.tradier.com` (real money, market-hours gated).

You normally don't edit it by hand: **`make run-dev` rewrites it to `true`** and
**`make run-prod` rewrites it to `false`** before booting (so they force the
environment regardless of the current value). Only bare **`make run`** respects the
existing value. Note: Tradier's **sandbox serves fake/stale prices** (no real index
quote), so use `run-prod` when you need realistic spot/strikes — and DRY RUN first.

## Using it

- **Ticker** dropdown (left). First entry (NDX) is selected on load.
- **Strategy** — one of 10, single-select. **Bull Call** selected by default:
  Long Call, Long Put, Bull Call, Bear Put, Bull Put, Bear Call, Iron Condor,
  Iron Fly, Call Fly, Put Fly.
- **Legs** auto-fill from per-ticker offset rules (snapped to the live chain
  grid). Use the −/+ steppers to move any leg up/down the real strike grid.
- **Net price** card shows the spread's live **bid / mid / ask** (debit or
  credit). The page **rebuilds every 3s**: strikes re-anchor to the current
  spot (keeping any manual ±steps) and prices refresh together, so spot,
  strikes, and net price stay in sync. Rebuild pauses while a confirm dialog or
  placement is in flight. The limit box pre-fills to mid; type any price or hit
  **Send Market**.
  - **Pricing source:** the net is summed **directly from the deduped chain**
    bid/ask (`/torque/build` and `/torque/price` use the *same* chain, never a
    separate `/markets/quotes` call that could return one leg). If any leg's
    quote is momentarily missing, the net is returned as `None` and the UI keeps
    its last complete price — it will **never** flash a partial single-leg
    number.

### Spot price (Yahoo live, parity fallback)

Spot source order (`_get_chain` in `routes/torque.py`):
1. **Yahoo** live `regularMarketPrice` — primary (`YAHOO_SYMBOLS` map: NDX→^NDX,
   SPX→^GSPC, RUT→^RUT, SPY, QQQ). 2s timeout; any failure is soft.
2. **Tradier put-call parity** at the ATM strike (`spot ≈ call_mid − put_mid +
   strike`, `torque_engine.implied_spot`) — fallback if Yahoo is down. Computed
   from real-time option bid/ask, so it's accurate even though Tradier's own
   *derived index quote* lags during fast moves.
3. Raw broker quote, then median strike — last resorts.

Used everywhere: display *and* strike anchoring. Yahoo was chosen as primary
because it's the exact live index print; parity is the resilient fallback that
needs no external feed.

**Index root (NDX→NDXP):** index underlyings expose two roots — the stale
AM-settled monthly (`NDX`, quotes hours old, **lists extra dead 5-pt strikes**)
and the live PM-settled 0DTE (`NDXP`). `normalize_chain` fixes this in two steps:
1. **Per-strike dedup** by **volume first** (live root trades, stale root vol=0),
   then spread tightness, then timestamp. Timestamp is NOT primary — Tradier's
   `bid_date` flip-flops, which would make the live↔stale pick flicker every few
   seconds. **Open interest is excluded** (the stale monthly root often has
   *higher* OI → would select the wrong quotes).
2. **Keep only the live root** (`_keep_primary_root`) — drops the stale root's
   extra strikes entirely, so the tradeable grid is exactly the live listing and
   the auto-anchor can't snap onto a dead strike. Root preference is
   **deterministic**: a KNOWN weekly/PM root (`KNOWN_LIVE_ROOTS` = NDXP, NDXW,
   SPXW, SPXPM, RUTW, XSP…) wins first (so it's right even pre-market at 0
   volume), then most volume, then most strikes.

**Why translate after the fetch, not before:** Tradier's chain endpoint only
accepts the **base** symbol — `symbol=NDXP/SPXW/RUTW` returns empty (verified),
so you can't request the weekly root directly. The base query (`NDX`) always
returns both roots mixed; filtering to the live root is the only place the
NDX→NDXP translation can happen.

The UI shows the **base ticker** (NDX/SPX/RUT) for display only; the legs and
the placed order carry the resolved weekly OCC (NDXP/SPXW/RUTW). Generic across
all tickers; ETFs (SPY/QQQ) are single-root no-ops. Verified live: NDX→NDXP,
SPX→SPXW, RUT→RUTW, plus a 40s sticky monitor (net mid held a tight band, root
stayed NDXP, build and `/torque/price` agreed every sample, zero single-digit
dips).
- **Auto-close +N%** toggle (default 30%) places a profit-target close — see
  below.
- **Right panel** — a real-time directional **lean** score (−100…+100 from
  volume skew + premium skew + OI magnet). ≥+25 → bullish/CALL, ≤−25 →
  bearish/PUT, in between → **neutral / NO EDGE** (no side suggested — it does
  *not* default to CALL). It is an **honest heuristic, not a prediction**.

## Auto-close (hybrid, +30% target)

The profit target is a **% return on the entry's debit/credit**:
- **Debit** spreads/singles (Bull Call, Bear Put, flys, Long Call/Put): close =
  sell-to-close at **1.30 × entry debit**.
- **Credit** spreads (Bull Put, Bear Call, IC, Iron Fly): close = buy-to-close at
  **0.70 × entry credit** (keep 30% of the credit).

How the close is wired (Tradier has **no** native conditional bracket for
*spreads* — OTO/OTOCO legs are single-symbol only):
- **Single-leg + limit entry** → native Tradier **OTO** bracket (broker holds the
  profit-target close; survives disconnects).
- **Everything else** (all spreads, any market entry) → **app-managed background
  watcher**: the place request submits the entry and **returns immediately**
  (it does *not* block), then a background task polls
  `GET /accounts/{id}/orders/{id}` until `status=filled` — for up to a full
  trading day — and places the close then, **retrying** if the freshly-filled
  position isn't settled yet. If the entry is rejected, canceled, or never fills,
  **no close is placed**.

  > This replaced an earlier *synchronous* 30s poll: a limit entry that filled
  > after 30s got no close (and the request blocked 12-15s meanwhile). The
  > background watcher fixes both — fast response **and** a close on slow fills.
  > Watcher progress is visible in the **Orders panel** (`state`:
  > `watching_fill` → `close_placed` / `entry_not_filled` / `close_rejected`).

## Dry run (validate without placing)

The **DRY RUN** button (next to Send) submits the entry *and* the close to Tradier
with `preview=true` — Tradier validates symbols, multileg format, and buying power
and returns a verdict **without placing anything**. Use it before market open to
confirm there are no format/symbol issues.

- **Entry** → `✓ accepted`, or the real reason (e.g. insufficient buying power — a
  real account constraint, not a format error).
- **Close** → a closing order can't be fully previewed until the position is open,
  so Tradier replies *"cannot be placed unless closing a long/short position"*.
  Torque recognizes this and reports **format valid, validates live after fill** —
  confirming the close is a well-formed closing order. A genuine format/symbol
  error surfaces differently.

API: `POST /torque/place` with `dry_run:true` (no `confirm` needed) →
`{mode:"dry_run", entry_ok, entry_reason, close_format_valid, close_needs_position,
close_target_price, ...}`. Validated against **production** for all 10 strategies
(2026-06-17): every entry+close payload accepted; only a single long NDX put hit a
real buying-power limit.

## Per-ticker strike config

Offsets are **backend-owned and API-served** — never hardcoded in the page. Edit
defaults in `market/backend/app/torque_config.py`, or drop a `torque_tickers.json`
at the repo root (or point `$TORQUE_TICKERS_CONFIG` at one) to override without
touching code. Shape:

```json
{
  "tickers": ["NDX","SPX","RUT","SPY","QQQ"],
  "default_strategy": "bull_call",
  "overrides": {
    "NDX": {
      "tick": 0.05,
      "vertical":  {"anchor": 20, "width": 100},
      "condor":    {"short_offset": 100, "width": 100},
      "iron_fly":  {"width": 100},
      "butterfly": {"body_offset": 0, "wing": 100}
    }
  }
}
```

Per-ticker defaults (0DTE, scaled to each instrument's price + strike grid;
debit-vertical `anchor`/`width` shown — condor/fly widths match):

| Ticker | grid | anchor | width |
|---|---|---|---|
| NDX | 5–10 | 20 | 100 |
| SPX | 5 | 5 | 25 |
| RUT | 5 | 5 | 20 |
| SPY | 1 | 1 | 5 |
| QQQ | 1 | 1 | 5 |

These are starting points — tune per taste in `torque_config.py` or `torque_tickers.json`.

Anchoring model (points from spot, snapped to the chain grid):
- `single` — long option `offset` points OTM (0 = ATM).
- `vertical` — near leg `anchor` from spot; far leg `width` beyond it. Near leg is
  the long for debit verticals, the short for credit verticals. **NDX:** anchor 20,
  width 100 (Bull Call long 20pts off spot, short 100 beyond).
- `condor` — short put/call `short_offset` from spot, `width` wings.
- `iron_fly` — short put+call ATM, `width` wings.
- `butterfly` — body at spot+`body_offset`, `wing` each side (buy 1 / sell 2 / buy 1).

## API (all under the market backend)

| Endpoint | Purpose |
|---|---|
| `GET /torque` | the page |
| `GET /torque/config` | tickers + strategy registry + env/mode |
| `GET /torque/analyze/{sym}` | spot + directional lean (poll ~5s) |
| `POST /torque/build` | auto-filled legs for (ticker, strategy, step adjustments) |
| `POST /torque/price` | live net bid/mid/ask for a set of legs (poll ~2.5s) |
| `POST /torque/place` | submit entry; arm background fill-watch + auto-close (`confirm:true`) |
| `GET /torque/order/{id}` | single order status |
| `GET /torque/orders` | **only the live/working** orders (no filled/closed history) + active auto-close watchers — feeds the bottom **Orders panel** (polled ~4s) |
| `POST /torque/cancel/{id}` | cancel a working order from the panel |
| `POST /torque/modify/{id}` | change a working order's limit price (Tradier PUT) |

## Orders panel

A bottom panel shows **only working orders** (entries still resting + GTC
profit-target closes), newest-first. Each row's **limit price is click-to-edit**
(Enter or ✓ submits a modify; Tradier `PUT`), and has a **cancel** button. An
**auto-close watcher** strip shows the armed close while its entry is still
filling ("auto-close armed — waiting for entry N to fill → ~$X GTC"); once the
close order is actually placed it becomes a working order in the table. Polls
`/torque/orders` every ~4s; a **busy cursor** shows during any place/modify/
cancel.

**Layout:** the page is a fixed-viewport SPA — centered, never scrolls. The
orders panel fills the remaining height and scrolls **internally** when it has
more rows than fit.

## Branding & assets

Logo and favicon live in `market/backend/ui/assets/` (`torque-logo.svg`,
`favicon.svg` — a torque-gauge mark, emerald→blue gradient), served at
`GET /torque/assets/{name}` (path-traversal guarded). The header shows the logo +
**TORQUE** with a small italic **"by EDGELANE"** byline in the EdgeLane emerald
(`#36d399`). Muted label colors were brightened (`--dim #aab7c5`, `--dim2
#8d9bab`) and small label fonts bumped ~1px for readability on the dark theme.

## Files

- `market/backend/ui/torque.html` — the page (React + Babel-standalone, single file).
- `market/backend/ui/assets/` — logo + favicon (SVG).
- `market/backend/app/routes/torque.py` — endpoints + place/confirm/close flow.
- `market/backend/app/torque_engine.py` — strike auto-fill, pricing, lean, close math (pure).
- `market/backend/app/torque_config.py` — per-ticker offsets + strategy registry.
- Reuses `order_builder.py` (multileg payloads) and the async `TradierClient`
  (`quotes`, `place_order`, `get_order` added for Torque).

## Status / not yet exercised

Backend is validated end-to-end against live NDX data (analyze/build/price) and
against the mock client for the full place→confirm→close and OTO branches. The
**live broker round-trip was not exercised** because the configured sandbox token
returns HTTP 401 — refresh `TRADIER_TOKEN_SANDBOX` and run `make run-dev` to test
placement on paper before going to production.

Possible next steps: WebSocket quote streaming (vs. the current 2.5s poll);
premium-optimized wing selection for condors/flys; a stop-loss leg (→ native
OTOCO for single-leg).
