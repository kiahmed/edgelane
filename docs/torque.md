# Torque — fast multi-leg order builder

Torque is a standalone web page served from the EdgeLane **market** backend. It's
the place-order dialog pulled out of the main JSX app into its own fast,
auto-filling advanced order menu. You choose a ticker + one strategy, it auto-fills
the legs, prices them live, and places the order (optionally with an auto-close
profit target).

It does **no forecasting** — no predictive bias engine like the main app. What it
*does* show is a **Positioning** panel: an honest, deterministic snapshot of where
options money currently sits (a heuristic "lean") plus a **live flow** read of
where premium is being traded right now. That read also **auto-lands a starting
strategy** in the grid on load — a convenience, fully overridable, never a
recommendation.

> **This file is the technical/implementation spec** — how Torque is built, run,
> configured, and deployed. For what every control does and what every reading on
> screen means (Lean, Skew, Magnet, Flow, Session Flow, Counter Read, every label
> and threshold, every caption you can see), see the separate
> **[Operating Manual](torque_operating_manual.html)**.

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

Torque reads **`edgelane_market.config`** (the market-backend config), which is now
the only config in the repo. The single flag that decides sandbox vs production for
Torque is **`DEVMODE`** in `edgelane_market.config`:

- `DEVMODE=true` → sandbox token + `sandbox.tradier.com` + sandbox DB (paper).
- `DEVMODE=false` → production token + `api.tradier.com` (real money, market-hours gated).

You normally don't edit it by hand: **`make run-dev` rewrites it to `true`** and
**`make run-prod` rewrites it to `false`** before booting (so they force the
environment regardless of the current value). Only bare **`make run`** respects the
existing value. Note: Tradier's **sandbox serves fake/stale prices** (no real index
quote), so use `run-prod` when you need realistic spot/strikes — and DRY RUN first.

### Access (auth)

Torque trades the **server's single broker account**, so it's an admin/owner
tool — not a per-user feature. When the backend runs with `AUTH_ENABLED=true`
(production), every data/action endpoint requires the **admin token** (or a
Supabase JWT); the page is locked until you open it with the token in the URL:

```
https://<backend-host>/torque?token=<ADMIN_API_TOKEN>
```

The page stashes the token (per-tab `sessionStorage`) and sends it as
`X-Admin-Token` on every fetch. No token → the page shows a "locked" notice and
the endpoints 401. Being logged into the main EdgeLane app in another tab does
**not** carry over — that session lives on the Vercel origin; Torque is served
from the backend origin and browsers isolate storage per-origin.

`ADMIN_API_TOKEN` lives in `edgelane_market.config`. With `AUTH_ENABLED=false`
(dev / `make run-dev`) the gate is a no-op — open `:8789/torque` directly.

## Using it

- **Ticker** dropdown (left). First entry (NDX) is selected on load.
- **Strategy** — one of 10, single-select: Long Call, Long Put, Bull Call,
  Bear Put, Bull Put, Bear Call, Iron Condor, Iron Fly, Call Fly, Put Fly. On load
  (and when you switch ticker) the grid **auto-lands once** on the structure the
  current positioning makes playable — `call-leaning → Bull Call`,
  `put-leaning → Bear Put`, `balanced → Iron Condor` (or **Iron Fly** when OI is
  pinned tight to a near-spot magnet). It **does not keep flipping** as the lean
  drifts, and any manual pick sticks — so it never yanks the strategy out from
  under you mid-build. The small caption beside "Strategy" (`positioning now: …`)
  restates that this reflects **where the market sits, not a recommended play**.
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
- **Lock Strikes** *(toggle, right of the spot line)* — freezes the strikes so they
  **stop re-anchoring** as spot moves. The page keeps rebuilding every 3s, but the
  strikes are derived from the spot **at the moment you locked** (a frozen anchor)
  instead of live spot — so **premiums keep updating** for exactly those strikes
  while the strikes themselves stay put. The **−/+ steppers still work** (relative to
  the frozen anchor), so you can dial in a specific set of strikes and target them
  without the constant drift distracting you. The displayed spot stays **live** (only
  the strikes are pinned). Toggle **off** to drop the anchor and resume normal
  auto-anchoring; it also resets to unlocked on any ticker/strategy change. *(Backend:
  optional `anchor_spot` on `POST /torque/build` — strikes derive from it, prices
  still come from the live chain.)*

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
- **Right panel — Positioning** — a real-time **snapshot** score plus a **live
  flow** read. It is **NOT a price forecast** — static OI/premium can't tell
  direction (OI has a long *and* a short side; index put OI is mostly hedging).
  Read it as positioning context only. Full breakdown of every reading, label, and
  threshold: **[Operating Manual](torque_operating_manual.html)**.

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
at **`market/backend/torque/torque_tickers.json`** (a dedicated folder for Torque
config, kept alongside — not inside — the `app/` code; a template with every
current key is at `torque_tickers.json.example` in that same folder) to override
without touching code. `$TORQUE_TICKERS_CONFIG` still works too if you'd rather
point at a file elsewhere; a bare `torque_tickers.json` at the repo root is also
still picked up as a back-compat fallback. Shape:

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

**Counter Read's noise floor** lives in the same override file, under a
`richness_floor` key (per-ticker, in $) — see
[Counter Read](torque_operating_manual.html#counter) for what it does and why one
flat number doesn't work across tickers with different premium scales. Defaults:
NDX/SPX/RUT `0.05`, SPY/QQQ/DJX `0.02`, everything else `0.03`. Served to the
frontend as `richness_floors` in `GET /torque/config`.

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
| `GET /torque/config` | tickers + strategy registry + env/mode + per-ticker `close_targets`/`richness_floors` |
| `GET /torque/clock` | `{market_state, open}` from Yahoo `marketState` (holiday/early-close aware; cached ~30s success / ~8s failure so an outage doesn't re-hit each poll) — gates the pause |
| `GET /torque/analyze/{sym}` | spot + positioning snapshot (poll ~5s) |
| `POST /torque/build` | auto-filled legs for (ticker, strategy, step adjustments); optional `anchor_spot` freezes the strikes (Lock Strikes); also returns `counter` — the opposite structure's legs+price from the same chain/spot, `null` for Iron Condor/Fly (see [Counter Read](torque_operating_manual.html#counter)) |
| `POST /torque/price` | live net bid/mid/ask for a set of legs (poll ~2.5s) |
| `POST /torque/place` | submit entry; arm background fill-watch + auto-close (`confirm:true`) |
| `GET /torque/order/{id}` | single order status |
| `GET /torque/orders` | `orders` (live/working) + `watchers` (active auto-close) + `history` (all Torque-tagged orders today, any status — the account-wide **Past Orders**) — feeds the bottom panel (polled ~4s) |
| `POST /torque/cancel/{id}` | cancel a working order from the panel |
| `POST /torque/modify/{id}` | change a working order's limit price (Tradier PUT) |

## Orders panel

A bottom panel with two tabs:

**Working** (default) — shows **only live orders** (entries still resting + GTC
profit-target closes), newest-first. Each row's **limit price is click-to-edit**
(Enter or ✓ submits a modify; Tradier `PUT`), and has a **cancel** button. An
**auto-close watcher** strip shows the armed close while its entry is still
filling ("auto-close armed — waiting for entry N to fill → ~$X GTC"); once the
close order is actually placed it becomes a working order in the table. Polls
`/torque/orders` every ~4s; a **busy cursor** shows during any place/modify/
cancel.

**Past Orders** — **account-wide**, newest first: every **Torque-tagged** order on
the broker account today at **any status** (pending → filled / canceled / rejected
/ expired). Because it's derived from the account's own orders (Torque tags all of
them `torque…` / `torqueClose…`), it is **identical from every tab**, shows the
**broker's own live status** (so it always reflects fills/cancels — no per-tab
tracking to drift), and needs no extra API call (the `/torque/orders` poll returns
it as `history`, filtered from the same fetch). Not persisted server-side — it's
just today's account orders, so it naturally clears with the broker's daily order
history. *(Caveat: an order **rejected at submission** never becomes a broker order,
so it won't appear here — the red `✗ ENTRY REJECTED` result panel is your record of
that.)*

**Order-status panel** — the "✓ ENTRY SENT · entry id N" confirmation that
appears under the order builder after you place **auto-dismisses after 10s** (the
order lives on in Working / Past Orders below, so the confirmation doesn't need to
linger).

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

- `docs/torque.md` — this file: the technical/implementation spec.
- `docs/torque_operating_manual.html` — the user-facing manual: every control,
  every reading, every label and threshold on screen.
- `market/backend/ui/torque.html` — the page (React + Babel-standalone, single file).
- `market/backend/ui/assets/` — logo + favicon (SVG).
- `market/backend/app/routes/torque.py` — endpoints + place/confirm/close flow.
- `market/backend/app/torque_engine.py` — strike auto-fill, pricing, lean, close math (pure).
- `market/backend/app/torque_config.py` — per-ticker offsets + strategy registry.
- Reuses `order_builder.py` (multileg payloads) and the async `TradierClient`
  (`quotes`, `place_order`, `get_order` added for Torque).

## DJX (Dow) — DJXW root only

DJX is Cboe's 1/100th-DJIA index option (spot ~525 when the Dow is ~52,500).
Torque trades **only the `DJXW` root** — the daily/weekly, PM-settled listing
(dailies launched 2026-05-18). The AM-settled monthly `DJX` root is excluded via
`torque_config.TICKER_ROOTS`, and any expiration that lists *only* that root
(the 3rd-Friday monthlies) is **skipped** — `_get_chain` walks forward through
`teng.expiration_candidates()` until an expiration yields DJXW contracts.

**Why DJX needs special handling.** It is ~1/12th the premium of SPX while market
makers still quote a ~$0.30-wide package. Measured live (2026-07-09):

| | ATM package spread | zero-bid strikes (±2.9%) |
|---|---|---|
| SPX 0DTE | ~3.4% of mid | 0–6% |
| DJX (DJXW) | 16–43% of mid, →800% as 0DTE premium decays | 22–40% |

Consequences, all encoded in config rather than in the page:

* **`tick = 0.01`** — DJX quotes in pennies (2.68 / 1.41 are not 0.05 multiples).
* **`width = 10`** on verticals/wings. At width 2–5 the package spread is 47–60%
  of its own mid and cannot be closed profitably; width 10 brings it to ~16%.
* **Auto-close floor.** The global +30% target is *below* DJX's round-trip cost,
  so a 30% close order prices inside the package bid/ask and can never fill.
  `CLOSE_TARGETS["DJX"]` sets `default 60% / min 45%` and marks the ticker
  `spread_scaled`, which additionally raises the floor to the **live** package
  spread at place time. `/torque/place` clamps **up** to that floor and reports
  `close_target_pct` + `close_target_clamped` + `package_spread_pct`.
* **Untradeable-package guard.** If the package's own bid/ask exceeds
  `MAX_AUTO_CLOSE_SPREAD_PCT` (100% of mid) the order is refused with a 400
  rather than armed with an exit that can never fill.

Spread-scaling is **opt-in per ticker** (`spread_scaled: true`) so SPX/NDX/RUT
behaviour is byte-for-byte unchanged.

> Reality check: DJX is genuinely illiquid — Dow options flow lives in **DIA**
> (penny-wide) and YM/MYM futures options. Short-horizon DJX spread trading is
> dominated by transaction cost. The floors above stop guaranteed-loss configs;
> they do not make DJX a good scalping vehicle.
>
> Note DIA has **no dailies** (Fridays only), so it cannot serve 0DTE. For daily
> Dow 0DTE, DJXW is the only instrument on Tradier — and it is expensive.

### The spread is not a fee

The most common misreading. **Filling at the mid costs nothing** — the mid *is*
fair value, and no fee is "baked in" (DJX's actual Cboe exchange fees are cents
per contract). The spread only costs you when you **cross**:

| Fill | Cost |
|---|---|
| at the mid | zero |
| at the ask (market order / crossing) | the full half-spread |

So the cost is a tax on *needing to trade now*, not on the instrument. On SPX
there is size resting at the mid and a mid limit fills instantly; on DJX 22–40%
of near-money strikes have no bid at all, so a mid limit may never fill and you
end up improving toward the ask.

This shapes every guardrail:

* **Auto-close is a passive resting GTC limit** — it never crosses, so it never
  pays the exit half-spread. That is why it remains viable on a wide book.
* **Auto-close is MANDATORY on DJX** (`CLOSE_TARGETS["DJX"]["required"]`). The
  target % is editable (floored); the toggle is locked on. `/torque/place`
  rejects `auto_close=false` rather than silently coercing it — a coerced `true`
  would place a second real order the caller never asked for.
  **Consequence:** a mandatory exit that cannot be armed means the *entry* is
  refused too. When the package exceeds `MAX_AUTO_CLOSE_SPREAD_PCT`, DJX cannot
  be traded at all until the book tightens — the Send buttons grey out. This is
  intended: no fillable exit, no trade.
* **A stop must cross** (you need out now), so it *does* pay. `_monitor_stop`
  therefore triggers off the **mid**, never the bid (a wide package is underwater
  by half its spread the instant it fills — a bid-based stop fires immediately),
  and refuses to cross a book wider than `STOP_MAX_EXIT_SPREAD_PCT` (60%).
  Config: `STOP_LOSS["DJX"] = {"default": 50.0}`.
* **Market orders are disabled for DJX** (`NO_MARKET_ORDER_TICKERS`), enforced in
  `/torque/place` and greyed out in the page. A market order is "cross at any
  price", and DJX's ask has been measured at 4× its own mid.
* **`/torque/price` surfaces the real cost** — `marketable_price` (what crossing
  now costs), `cross_cost` (how much you overpay vs mid), `package_spread_pct`,
  `untradeable`, and the live `min_close_target_pct` the server will clamp to.
  The Send row labels this, so the true price is visible *before* you send.

### Execution fees are folded into the close target

`close_target_price()` nets the profit target **after commissions**, so a debit
target is priced *above* naive `entry×(1+pct)` and a credit buy-back *below*
`entry×(1−pct)`. `breakeven_close_price()` is the same function at `pct=0`.

Commission is charged **per contract, per leg, on the open and again on the
close**. Because fee and premium both scale with quantity, the per-unit cost is
quantity-independent: `round_trip_fee = 2 × legs × fee ÷ multiplier`.

Rates in `COMMISSION_PER_CONTRACT` were read off Tradier's own order-preview
`commission` field against a live production account (sandbox agrees):

| | 1 contract | 10 contracts | 2-leg vertical |
|---|---|---|---|
| DJX | 0.53 | 5.30 | 1.06 |
| SPX | 0.95 | — | — |

Preview reports `fees = 0` — regulatory pass-throughs (ORF, TAF) are assessed at
settlement, not at order time. We do **not** invent them; `extra_fee_per_contract`
defaults to `0.0` and is configurable in `torque_tickers.json`.

**Credit targets are bounded below 100%.** A credit buy-to-close is priced at
`entry×(1−pct)`, which goes **non-positive at pct ≥ ~100%** (sooner with fees) —
Tradier rejects a ≤0 limit, which would *silently unarm* the auto-close. Two
guards: `close_target_price()` floors every credit result at one tick (the
cheapest real buy-back = "keep essentially all the credit"), and `/torque/place`
caps the *reported* `close_target_pct` at `max_credit_pct()` for credit orders so
the response is honest. The field still accepts up to 500 because the same field
carries debit targets (sell at 6×), where high values are legitimate.

Two consequences worth knowing:

* **Tick rounding is directional** — debit targets round UP, credit targets round
  DOWN. Nearest-tick rounding could price a "breakeven" order *below* the entry
  (e.g. entry 2.123, tick 0.05 → 2.10). A property test asserts the target never
  prices below breakeven, for every leg count.
* **This shifts SPX/NDX targets too**, by the round-trip commission (e.g. a 2-leg
  NDX debit at 20.00 targets 26.05 rather than 26.00). That is a correction, not a
  regression: the old target did not cover its own commissions.

`/torque/price` returns `breakeven_at_mid` / `close_target_at_mid` and
`breakeven_at_market` / `close_target_at_market`, so both fill scenarios are
priced before you send. On a 5-cent DJX contract the commission alone is ~21% of
premium — the readout makes that visible.
* **`/torque/modify` re-applies the floor.** Editing a `torqueClose…` order in the
  orders panel used to bypass every guardrail (the modify payload carries only a
  price). `_CLOSE_GUARD` records the entry fill + floor when the close is placed,
  and `_enforce_close_floor_on_modify` rejects an edit that prices the close
  inside the package bid/ask.

**Not protected:** the spread cost itself. The guardrails stop you *placing a
trade whose exit cannot fill* and stop you crossing a garbage book. They do not
cap losses from the spread, and max loss remains the debit paid (or wing width).

## Brokers (Tradier / Webull)

Torque places through the **signed-in user's own active `broker_configs` row**
(`broker_resolver.resolve_broker`); the admin/dev path uses the house Tradier
account. Both Tradier and Webull connections are accepted, but Webull is
**capability-limited** because `WebullClient` exposes only `list_accounts` /
`first_account_id` / `preview_option` / `place_option`:

| Torque feature | Tradier | Webull |
|---|---|---|
| Entry (single + multileg) | ✅ | ✅ (always `preview_option` first) |
| Auto-close | ✅ | ❌ 501 — needs order-status polling |
| Native OTO bracket | ✅ | ❌ (no OCO in this SDK surface) |
| Orders panel / cancel / modify | ✅ | ❌ 501 |

Auto-close is **refused** (not silently dropped) on Webull: it is app-managed
(place → poll until FILLED → place close), and without `get_order` the entry fill
can't be confirmed — placing anyway would either never close or leave a naked
close against an unfilled entry.

> ⚠️ **Webull cannot run in this venv today.** `pyproject.toml` pins
> `webull-openapi-python-sdk>=2.0`, but every release requires `python >=3.8,<3.14`
> and the backend venv is **3.14**. The SDK is not installed, so *any* Webull call
> (here or in `/orders`) raises `WebullError: Webull SDK not installed`. Pin the
> backend to Python 3.13 to light it up.

## Status / not yet exercised

Backend is validated end-to-end against live NDX data (analyze/build/price) and
against the mock client for the full place→confirm→close and OTO branches. The
**live broker round-trip was not exercised** because the configured sandbox token
returns HTTP 401 — refresh `TRADIER_TOKEN_SANDBOX` and run `make run-dev` to test
placement on paper before going to production.

Possible next steps: WebSocket quote streaming (vs. the current 2.5s poll);
premium-optimized wing selection for condors/flys; a stop-loss leg (→ native
OTOCO for single-leg).
