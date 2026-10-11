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
1. **Yahoo** live `regularMarketPrice` — primary (each ticker's `yahoo_symbol` in
   `torque_tickers_setup`: NDX→^NDX, SPX→^GSPC, RUT→^RUT, SPY, QQQ, …). 2s
   timeout; any failure is soft.
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

## Auto-close + stop-loss (hybrid, ±30% default)

The profit target and stop-loss are both a **% return on the entry's debit/credit**:
- **Debit** spreads/singles (Bull Call, Bear Put, flys, Long Call/Put): profit =
  sell-to-close at **1.30 × entry debit**; stop = sell-to-close at
  **0.70 × entry debit**.
- **Credit** spreads (Bull Put, Bear Call, IC, Iron Fly): profit = buy-to-close at
  **0.70 × entry credit** (keep 30% of the credit); stop = buy-to-close at
  **1.30 × entry credit**.

Both default to 30% (`DEFAULT_CLOSE_TARGET_PCT` / `DEFAULT_STOP_LOSS_PCT`),
overridable per ticker (`torque_tickers_setup`) and per order (UI toggle,
`close_target_pct` / `stop_loss_pct`). DJX keeps a wider 50% stop
(`stop_loss_default` on DJX's row) — its own package spread is wide enough that a 30% stop
would fire on noise; `stop_loss_defaults_map()` feeds this to the UI so
switching to DJX doesn't silently flatten it to 30%. `stop_loss_pct=0`
explicitly disables the stop (the field is now always sent, so `0` — not
omission — is what "off" means).

How the close/stop are wired (Tradier has **no** native conditional bracket for
*spreads* — OTO/OTOCO legs are single-symbol only; confirmed live against
sandbox that `class=multileg` rejects `type=stop`/`stop_limit` outright,
"Invalid parameter, type: is not valid."):
- **Single-leg + limit entry, no stop requested** → native Tradier **OTO**
  bracket (entry + profit leg; broker holds the close, survives disconnects).
- **Single-leg + limit entry, stop requested, exit prices ≥ `OCO_MIN_PRICE_GAP`
  ($0.10) apart** → native Tradier **OTOCO** bracket: entry + profit `limit`
  leg + stop `stop_limit` leg, all three submitted together. Both exits rest on
  Tradier's own book — whichever fires first cancels the other, no app-side
  coordination, survives a backend restart. The stop leg's limit sits
  `STOP_LIMIT_BUFFER_TICKS` (3 ticks) past its trigger so it's marketable the
  instant it trips.
- **Everything else** (all spreads, any market entry, or a single-leg stop too
  narrow to clear the $0.10 OCO gap) → **app-managed background watcher**: the
  place request submits the entry and **returns immediately** (it does *not*
  block), then a background task polls `GET /accounts/{id}/orders/{id}` until
  `status=filled` — for up to a full trading day — and places the profit-target
  close then, **retrying** if the freshly-filled position isn't settled yet.
  If a stop is also armed, a second loop (`_monitor_stop`, every
  `STOP_INTERVAL`=10s) watches the **live package mid** and, on breach,
  **cancels the resting profit close and submits a fresh stop-exit close** in
  its place. This is reactive, not resting — the stop-exit order doesn't exist
  on Tradier's book until the breach is detected and the cancel+resubmit
  round-trip completes, so it carries more latency/slippage risk than the
  native OTOCO's stop leg. If the entry is rejected, canceled, or never fills,
  **no close/stop is placed**.

  **A stop-limit only ever guarantees price, never a fill** — a violent enough
  move can leave it resting while the market keeps running away, with the loss
  compounding the whole time. So the watcher doesn't submit-and-forget: once
  the stop-exit limit is placed it keeps polling *that order's own status*. If
  it's still unfilled after `STOP_ESCALATE_AFTER_TICKS` (2) more polls, it does
  **not** jump straight to an unbounded market order — a multileg market order
  has *no* price protection and on a thin/wide book can clear worse than the
  quoted book itself. Instead it **re-quotes the SAME resting order in place**
  via `modify_order` (not cancel-and-replace — cheaper, and the order id/tag
  the Orders panel and history track never changes) at a fresh bounded price:
  still a real limit, just refreshed — what usually fills close to mid with
  price improvement on a multileg spread. Only after `STOP_MAX_REQUOTES` (5)
  of those real re-quotes still haven't cleared does it **freeze** and finally
  submit a genuine **market** order to guarantee the exit — because no limit,
  bounded or not, can ever guarantee a fill, and a stop that chases forever
  without ever exiting is worse than one that eventually pays the spread to
  actually get out. That fallback order carries a distinct tag
  (`torqueStopMkt…`) so it's visibly flagged in the Orders/Past-Orders table
  as what it is, not a routine bounded fill. The `STOP_MAX_EXIT_SPREAD_PCT`
  wide-book refusal (below) hands off to this same ladder rather than blocking
  forever: it still won't cross a garbage book on the *first* breach tick, but
  staying blocked for `STOP_ESCALATE_AFTER_TICKS` ticks running starts the
  ladder anyway — an open loss parked indefinitely is worse than one bounded
  crossing.

  **Every stall re-confirms the order's true state before touching it again**
  (`_resolve_stalled_stop`) — a timed-out poll does not mean the order is
  still sitting untouched. A fill (or partial fill) can land in the window
  between the last status check and the next requote, especially likely right
  when a stop fires, since a fast move is exactly what triggers both the stop
  AND a fill. Trusting a cancel or modify call's own success/failure isn't
  enough either — both commonly *fail* because the order just filled. So
  whenever a modify itself fails, the ladder re-reads the order fresh: fully
  filled → stop, nothing left to protect; partially filled (still resting,
  chased by modify like any other stall — the broker tracks its own remaining
  open quantity) → once the ladder does finally have to walk away from that
  order, the next one is sized for only what's actually still open
  (`quantity − exec_quantity`), never the original full size; state
  unconfirmable even after a cancel attempt → the ladder refuses to resubmit
  at all and parks in `stop_needs_attention` (surfaced in the Orders panel's
  watcher list, styled as a distinct alert) rather than risk closing more than
  actually exists, which would open a fresh naked position on the excess.

  **The TP → stop handoff gets the exact same ground-truth check.** Right
  before the stop takes over, it cancels the resting profit-target close and
  hands off to the ladder — but the profit-target order can just as easily be
  sitting *partially filled* at that instant (naturally, or because the
  caller cancelled it by hand at exactly the wrong moment), and blindly
  sizing the stop-exit for the position's original full quantity closes units
  the profit-target close already closed. So the handoff resolves the
  profit-target order's true state first, the same way: fully filled → stand
  down, nothing left to protect (`closed_at_target`); partially filled →
  protect only what TP actually left open; unconfirmable → `stop_needs_attention`
  rather than guess. A **manually cancelled TP with nothing filled** still
  protects the full original position, on every leg — cancelling it does not
  disable the stop.

  > This replaced an earlier *synchronous* 30s poll: a limit entry that filled
  > after 30s got no close (and the request blocked 12-15s meanwhile). The
  > background watcher fixes both — fast response **and** a close on slow fills.
  > Watcher progress is visible in the **Orders panel** (`state`:
  > `watching_fill` → `close_placed` / `entry_not_filled` / `close_rejected`).

**Scope**: both mechanisms only arm for orders placed through Torque's own
`/torque/place` call — there is no background scanner that discovers or adopts
existing positions/orders. An order placed by hand on Tradier, or through the
separate `tp` CLI (`tools/tradier_positions.py`, which talks straight to
Tradier's REST API and never calls into Torque), gets **no** auto-close and
**no** stop-loss. It can still *show up* in Torque's Orders panel (`/torque/orders`
lists every live order on the account, not just Torque's own), which is
display-only and easy to mistake for tracking.

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

Every per-ticker setting is **backend-owned, database-held and API-served** —
never hardcoded in the page or in code. It lives in the Supabase table
**`public.torque_tickers_setup`** (migration `0021`), one row per ticker, so every
backend container reads the same data:

| Column | Meaning |
|---|---|
| `ticker`, `enabled`, `sort_order` | Which tickers trade and in what list order (first enabled = page default). A disabled ticker leaves the list and is ignored by news signals, but its settings still apply to anything already open on it. |
| `tick`, `tick_above_3` | Price step. `tick_above_3` is the step once a price is ≥ $3 — penny-pilot names $0.01 below / $0.05 at or above; SPX/NDX/RUT $0.05 below / $0.10 at or above. null = `tick` at every price. It is a **single-option** rule: a multi-leg spread is a complex order priced in the base `tick` at any net price. For single legs every limit, profit target and stop rounds to the step valid **at its own price**, so a bracket that straddles $3 stays legal on both sides. |
| `offsets` (jsonb) | Strike offsets per strategy shape (`single` / `vertical` / `condor` / `iron_fly` / `butterfly`), merged over the built-in base offsets. |
| `yahoo_symbol` | Primary live spot quote (null = Tradier put-call parity). |
| `allowed_roots` | Option roots allowed (null = any; DJX trades `DJXW` only). |
| `commission_per_contract`, `extra_fee_per_contract` | Per contract per leg, one way (null = global default). |
| `stop_loss_default`, `close_target_default`, `close_target_min`, `close_target_spread_scaled`, `close_target_required` | Exit defaults / floors (null = global default). |
| `market_orders_allowed` | False forces limits (DJX). |
| `max_spread_pct` | Widest package bid/ask, as % of mid, the ticker trades, arms a profit target on, or calls tradeable (null = global 100; XLE/XLF 150 — their 0DTE books routinely run 50–120%). Entries are priced at mid either way; this only decides whether to trade at all. Migration `0023`. |
| `richness_floor` | Counter Read noise floor in $ (null = global default 0.03) — see [Counter Read](torque_operating_manual.html#counter). |
| `daily_min_range`, `daily_max_range` | Reserved for later use; not read by Torque yet. |

**How it's read.** Each container loads the table into memory before it serves
(`torque_config.refresh_ticker_setup`, from `main.py`), then re-reads it every
`TORQUE_TICKERS_REFRESH_SEC` (default 60) — **an edit in the table reaches every
container within that window, no restart or redeploy**. It **fails closed**:
until the table has been read once, Torque has no tickers (the page lists none,
news signals drop as "not a configured Torque ticker"; `GET /torque/config`
reports `tickers_loaded: false`). A failed refresh keeps the last good copy.
**New entries are refused** — `/torque/build`, `/torque/price` and
`/torque/place` return 503 while the setup hasn't loaded and 400 for a ticker
with no enabled row — because every per-ticker safety rule (DJX's forced limit
orders, mandatory auto-close and 45% floor, 50% stop, penny step) comes from
the table and must never fall back to permissive defaults. Exits, cancels and
modifies on existing positions keep working; a Torque profit-target close keeps
the step and floor rule it was placed with.
There is no `torque_tickers.json` and no hardcoded per-ticker data any more;
code keeps only strategy shapes and global defaults.

**Setup.** `make db-setup` (Backend — setup) applies every migration including
the seed, idempotently; the seed uses `on conflict do nothing`, so re-running it
never overwrites a row someone has since edited.

Seeded tickers (0DTE-scaled starting points; debit-vertical `anchor`/`width`
shown — condor/fly widths match):

| Ticker | step | anchor | width | notes |
|---|---|---|---|---|
| NDX | 0.05 / 0.10 ≥ $3 (single legs) | 20 | 100 | |
| SPX | 0.05 / 0.10 ≥ $3 (single legs) | 5 | 25 | |
| RUT | 0.05 / 0.10 ≥ $3 (single legs) | 5 | 20 | |
| SPY | 0.01 | 1 | 5 | penny at every price |
| QQQ | 0.01 | 1 | 5 | penny at every price |
| DJX | 0.01 | 1 | 10 | DJXW only; 50% stop, mandatory auto-close (60%/min 45%, spread-scaled), no market orders |
| IWM | 0.01 | 1 | 5 | penny at every price |
| XLF | 0.01 / 0.05 ≥ $3 | 0.5 | 1 | penny pilot |
| XLE | 0.01 / 0.05 ≥ $3 | 1 | 2 | penny pilot |
| SMH | 0.01 / 0.05 ≥ $3 | 2 | 5 | penny pilot |

IWM, XLF, XLE and SMH were added for Reflex `engine_v2`; their spread offsets
are starting points (news signals only trade the ATM single leg). Torque always
trades the **next listed expiry** from the chain, whether that's same-day (0DTE)
or a week out for weekly-only names, and the same-contract retry moves to the
following listed expiry.

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
| `GET /torque/clock` | `{market_state, open}` from Tradier's own `markets/calendar` (holiday/early-close aware, no external dependency; the instant check is cached ~30s success / ~8s failure, the underlying month's calendar ~1 call/day) — gates the pause. Replaced a Yahoo `marketState`-based version 2026-09-26 after that field silently disappeared from Yahoo's response |
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

## News-signal ingestion (facades-news-reactor)

`POST /webhook/news_signal` receives a directional signal (bullish/bearish, no
strike/spread guidance) from the sibling repo's news-sentiment pipeline and, if
every gate passes, places it through Torque's **own** `/torque/place` path —
same guardrails (auto-close, the stop-exit ladder) as a human clicking Send,
zero special-casing for "this came from a webhook." Past the authentication
gate (below), it always returns `200 ok:true` regardless of outcome (the
sender doesn't need to know Torque's mode); every non-execution path is a
log-and-drop `accepted:false` with a `reason`, never an error:

- **Authentication (`require_news_signal_auth`) — checked before anything
  else, and the only gate that verifies WHO is calling** (every other gate
  here constrains WHAT gets traded). HMAC-SHA256 over `{X-Signal-Timestamp}.
  {raw body}` with the shared `NEWS_REACTOR_WEBHOOK_SECRET`, sent as
  `X-Signal-Signature`; the timestamp must be within 5 minutes of now. Fails
  **closed** (401) when the secret is unset — unconfigured must mean closed,
  never open-to-anyone reaching `edge.facades.trade`. A real anonymous POST
  used to reach the handler and place a live order before this existed.
- **Idempotency (`claim_news_signal`, Supabase — `news_reactor_signals`)** —
  `source_event_id` is claimed atomically before any other work; a replay of
  an already-signed request (within the timestamp window) or the sender's own
  retry never places a second order. Claimed IDs are purged after 24h,
  piggybacked on the poller's own open→closed transition (`app/poller.py` —
  the same browser-independent loop that already recomputes market state
  every cycle), not a separate scheduler.
  The same insert stores the **full signal** — raw `payload` (jsonb) plus
  `symbol`/`state`/`direction`/`cancels_event_id`/`generated_at` — and the
  handler stamps `outcome` (`placed`/`dropped`/`cancel_processed`) + `reason`
  when it decides, so every signal is traceable (migration 0015).
- **Staleness (`NEWS_SIGNAL_MAX_AGE_SEC`, default 180)** — an **entry**
  signal whose `generated_at` is older than this on arrival, missing,
  unparseable, or that far in the future is dropped, never traded. This is
  the signal's own age, not the request signature's timestamp (which only
  says when it was *sent* — a queued/retried signal can be freshly signed yet
  stale). Cancels are exempt: unwinding late is still risk-reducing.
- **Out of hours in DEVMODE (sandbox accounts only)** — with `DEVMODE=true`
  (or `TRADIER_ENV=sandbox`) — the same switch that lifts the Torque UI's own
  market gate — entry and cancel signals are still processed when the market
  is closed/unknown, but **only** for accounts whose active connection is a
  Tradier **sandbox** (`_is_sandbox_account` for entries; the resolved
  client's sandbox base URL for cancels). DEVMODE only switches the house
  client, never a user's own connection, so every production account is
  still skipped exactly as the closed-market gate would. Responses carry
  `test_mode`. In production mode, closed = dropped, as before.
- **A per-window rate cap** (`NEWS_SIGNAL_MAX_PER_WINDOW`, default 10/hour) —
  independent backstop; a compromised secret or a malfunctioning sender still
  can't place unbounded orders.
- **`ACCEPT_NEWS_REACTOR_SIGNALS`** (default `false`) — the hard kill switch.
- **Ticker configured**, **market open** — fails **closed**: anything other
  than a confirmed `open is True` (including a calendar-fetch failure) drops
  the signal. An unattended trader should never act on an uncertain read, even
  though the sender already enforces trading hours on its own side.
- **Which accounts act on this** — facades-news-reactor posts **once and
  forgets**; fanning out to every qualified account is Torque's own job, not
  the sender's. "Qualified" is a **live Supabase query**
  (`supabase_admin.get_users_with_tool("news-reactor")`) — every profile
  whose `tools_enabled` currently contains `news-reactor`, checked again for
  **both** `torque` and `news-reactor` per account, plus an **active broker
  connection** (`resolve_broker`'s own per-user check, no house-account
  fallback). Granting or revoking a user's `news-reactor` entitlement takes
  effect on the very next signal — no restart, no separate allowlist to keep
  in sync with the database. (There is no live-session registry either way —
  an entitled+configured account acts on every signal whether or not anyone
  is looking at the page right now; a per-user "only auto-execute while I'm
  active" preference is a reasonable later addition but isn't built.) Each
  account is checked and placed independently — one account with no broker
  connected doesn't block another that has one, and each gets the order
  through its **own** broker. The chain/quote is fetched once and shared
  across every qualified account so they all act on the identical price
  rather than staggered re-fetches drifting apart. The response's `results`
  array reports each account's own outcome.
- **Live spread sanity** — the payload deliberately carries no price (by
  design: Torque fetches its own fresh quote at execution time rather than
  trading off a stale snapshot), so the freshly-built package spread is
  checked against `MAX_AUTO_CLOSE_SPREAD_PCT` and rejected if it looks wrong,
  rather than trading blind.

**Single-leg only for now** (`bullish → long_call`, `bearish → long_put`) —
the only structure that gets the fully broker-native OTOCO bracket for both
the profit target and the stop, the safest execution path for something that
fires with no one watching it. Multi-leg strategy selection is a possible
later addition once the signal payload itself carries more to base that
choice on. Quantity is `NEWS_SIGNAL_QUANTITY` (default 1); the stop-loss is
armed at the ticker's own configured default, same as any other order.

Every resulting order carries the `torqueNews…` tag (vs. the normal
`torque…`/`torqueClose…`) — visible in the Orders/Past-Orders table like any
other order (no separate panel), and it's what the page's sound alert keys
off: a short tone plays the first time a `torqueNews…`-tagged order appears in
a poll that wasn't there in the previous one, so an unattended fire still gets
noticed. Detection is tag-based specifically so it doesn't also fire for
orders placed via `tp` or by hand on the broker's own site, which show up in
the same account-wide orders list but aren't news-signal-driven.

### After submission: confirmation, same-contract retry, status

- **Submitted ≠ placed.** Tradier accepts a submission and can reject it a
  moment later, so each account's entry is read back (`_confirm_entry_status`)
  before it counts as placed. A rejection is reported per account with
  Tradier's reason, and the signal's outcome becomes `rejected`.
- **Same-contract conflict → next expiry.** When an earlier signal's bracket
  already holds exit orders on the exact contract, Tradier rejects a second
  bracket (*"Sell order is for more shares than your current long
  position…"*). That rejection walks forward one tradeable expiry at a time
  (`_build_next_expiry`, up to `NEWS_SIGNAL_MAX_EXPIRY_RETRIES`, default 1)
  until one takes — each an independent trade (`attempt` = 2, 3, …); a
  later CANCEL for the signal finds and unwinds it via `torque_orders` like
  any other order. Other rejections are not retried.
- **Every signal order's status lives in `torque_orders`** (`status`,
  `status_reason`, `status_at`, `expiration`, `attempt` — migration 0016):
  set at placement (the confirmed status, rejected attempts included), by
  the cancel-signal handler, by Torque's cancel button, and by the orders
  panel's broker poll, which catches anything done directly at the broker
  (fills, manual cancels, expiry). A recorded Torque action
  (`closed_at_market`, `close_rejected`, `needs_attention`) is never
  overwritten by the entry's raw broker status.
- **Recognised by id, not tag.** Tradier drops the `tag` on OTOCO brackets,
  so `/torque/orders` flags signal orders (`news_signal: true`) from
  `torque_orders`; the page's sound alert and Past Orders use that flag.
- **Cancels are exempt from the rate cap** (as from the age limit): the cap
  bounds new positions; an unwind only reduces risk.
- **Quantity** is `NEWS_SIGNAL_QUANTITY` (default 1) — raise it once the flow
  is proven.

### Entry step-up (fill chasing)

A news-signal entry goes in at **mid**. If it hasn't filled after
`NEWS_SIGNAL_ENTRY_STEP_SEC` (default 5s), it's re-placed **one price step
higher** — up to `NEWS_SIGNAL_ENTRY_STEPS` times (default 5), never above the
ask captured at build — and if it still hasn't filled after the last step it's
**cancelled** (no fill, no trade). News signals only; manual orders keep the
price you set. Each step cancels the resting bracket, confirms the cancel
took, then re-places a fresh bracket at the new price (its own `torque_orders`
row; the superseded one is recorded `canceled` with `repriced: step N → price`).
It stops the moment the entry fills, the order closes out, or a cancel signal
for the trade arrives — on this container (in memory) or any other (the signal
log). Rare race: a fill landing between the last read and the cancel leaves the
position with its bracket exits cancelled — recorded `needs_attention`, never
re-placed on top of. Not persisted across a restart: a step-up in progress when
the backend restarts leaves its current order resting as a day order.

**Brackets are read by their entry leg.** Tradier reports a bracket's entry
fill on its first leg; the parent can still read "open" (exits resting) after
the entry filled and carries no fill quantity of its own. The step-up and the
cancel handler both read the entry leg, and a cancel market-closes only what's
still open (entry fill minus anything the profit/stop legs already executed;
`already_closed` if they closed it all).

### CANCEL: unwinding a prior signal

The same endpoint also accepts a **cancel** for a signal it already acted on —
`state: "cancel"` plus `cancels_event_id: "<the original source_event_id>"`,
in place of a fresh `direction`/`confidence` (both are optional in this
shape; the payload carries neither strike, spread, nor quantity of its own).
It looks up every `torque_orders` row the original signal produced — one per
qualified account it fanned out to — and, **for each account independently**:

- Cancels that account's entry order, then trusts only a *fresh* `get_order`
  read of its real post-cancel status (`_cancel_or_close_entry` — the same
  "cancel best-effort, then re-confirm before acting" discipline the
  stop-exit ladder's `_resolve_stalled_stop` uses, because a cancel call
  commonly fails/no-ops precisely because the order just filled).
- **Never executed** → the cancel alone is the whole story (`action:
  "canceled"`).
- **Fully or partially executed** → a genuine market order closes **exactly
  the executed quantity** — regardless of current profitability or the
  stop-loss threshold, per spec (`action: "closed_at_market"`, tagged
  `torqueCancelMkt…` so it's distinguishable from a routine close in the
  Orders/Past-Orders table).
- If a TP/SL watcher (`_WATCHERS`) is still actively polling that exact entry
  when the cancel lands, it's marked **superseded** so it stops acting on a
  position the cancel just closed out-of-band — checked cooperatively at the
  top of every poll iteration in `_monitor_stop` and the stop-exit ladder
  (there's no way to forcibly interrupt a running task mid-`sleep`); its
  terminal state becomes `canceled_by_signal`.
- An account with no broker connection left, or one whose final order state
  can't be confirmed even after the cancel attempt, never blocks the unwind
  of the rest (`action: "skipped"` / `"needs_attention"` respectively) — same
  "one account's failure can't stop the others" posture as the entry
  fan-out. Same fail-closed market-hours gate as an entry signal.

## Stop guardian (stop-loss on every position)

Torque's order-level protections only cover orders Torque placed — and even
those had gaps: a broker bracket's exits are day orders (gone at 4pm on a
position that outlives the day), cancelling a filled bracket removed its stop,
the app watcher gave up after 6 hours, and nothing watched positions opened on
Tradier's own site or the `tp` CLI. The **stop guardian**
(`app/torque_guardian.py`, migration `0024`) closes all of these by watching
what each account actually **holds**:

- **Every ~`GUARDIAN_INTERVAL_SEC` (10s), market hours only** (any time in
  DEVMODE/sandbox), for every account Torque trades on — the house account and
  every user entitled to Torque with an active Tradier connection — it reads
  the open option positions in **Torque tickers** (others are ignored).
- **Units.** A lone option is one unit. Several on the same ticker are grouped
  only as far as **today's order history confirms** (Tradier lists only the
  current session's orders): a filled multi-leg order whose legs match open
  positions in the same proportion is one spread; a leg whose filled
  single-option opening orders today account for the **whole** held quantity,
  on the held side, is a single (a leg partly from an earlier day — e.g. one more long put
  added to a prior-day put spread — is not: selling it all would strip the
  short leg's cover). Anything it can't confirm is **left alone and
  flagged** (`unconfirmed`) — stopping one leg of a spread could leave the
  other naked.
- **Which stop applies — by who opened it** (the source is saved in the guard
  row once seen, so it survives today's orders rolling off; the strongest wins):
  - **Reflex** (its contract is on a news-signal order in `torque_orders`):
    **always** protected at the ticker default (or 30%), whatever the page says.
  - **Torque** (a filled entry today tagged `torque…` — Torque's exits,
    `torqueClose/Stop/Cancel…`, don't count): the page's saved per-ticker
    stop-loss setting (`torque_sl_settings`, per broker account); **no row = on**
    at the ticker default.
  - **Outside Torque** (Tradier's site, the `tp` CLI, anything else): guarded
    **only once stop-loss is saved ON** for that ticker on the page; **no row =
    not guarded** (`sl_off`, "opened outside Torque"). The page shows "default ·
    save to guard outside positions" until the setting is saved.
- **Breach** is measured at mid against the position's cost basis, the same
  `stop_breached` rule the watcher uses.
- **Who acts.** A position Torque's own app watcher manages is never touched.
  If a **broker-held stop** is resting (a bracket's stop leg or a stop order),
  the guardian stands back — but if the price is past the stop and that stop
  hasn't filled within `GUARDIAN_TAKEOVER_SEC` (10s), it **takes over**. With
  no stop resting, it acts on the breach (after one re-check if the book is
  wider than `STOP_MAX_EXIT_SPREAD_PCT`).
- **The exit.** First cancel every working order that would close the position
  (bracket exits, a resting profit target, a stale exit) so the exit isn't
  rejected as over-closing; then **re-read the position** — if it already
  closed, nothing more happens; if its size changed, `needs_attention`, never a
  guessed size — then run the normal stop-exit ladder (bounded re-quotes, then
  market) for exactly what's held. Exit pricing quotes the legs directly when
  they're not on the nearest-expiry chain.
- **Already past the stop when first seen** (e.g. at first deploy): **alert
  only** (`past_stop_alert`) — never auto-closed; it re-arms once the price is
  back above the stop.
- **State** per unit is persisted in `torque_guarded_positions` (source,
  stop, state, note) and removed when the position is gone; the Orders panel
  shows it.
- **Where it runs.** `GUARDIAN_ENABLED` is **off** by default and off in the
  shared `edgelane_market.config`; the production compose file turns it on with
  the `GUARDIAN_ENABLED=true` env var (which overrides the file), so a local
  `make run` / `run-dev` never starts a second guardian. In DEVMODE/sandbox it
  guards **sandbox accounts only** — users' own Tradier connections are
  production whatever DEVMODE says, and those are refused (logged once).
  Each pass it must hold the **single-runner lease** (`torque_guardian_lease`,
  migration `0025`; one row per environment, `guardian:live` /
  `guardian:sandbox`, TTL 3× the interval, min 30s) — a second container, or a
  misconfigured one, finds it held and stands by. No lease (or Supabase
  unreachable) = no action.
- **No market orders where they're off.** For a ticker with
  `market_orders_allowed = false` (DJX), the guardian's no-quote exit doesn't
  go to market — it marks the unit `needs_attention` — and a cancel signal on a
  filled entry closes with a marketable limit at the live bid/ask
  (`closing_at_limit`), or `needs_attention` with no quote.

**Order status in the panel.** A bracket's own (parent) status doesn't say
whether the entry filled or which exits are still working, so the panel shows
what's actually live, read from its legs: `pending`, `filled · TP + SL live`,
`filled · SL live`, `filled · TP live · SL by guardian`, `filled · SL by
guardian`, `filled · no SL (turned off)`, `closed · TP`, `closed · SL`.
`torque_orders.status` for signal orders uses the same entry-leg reading.

## Multi-user order persistence

Every watcher (`_watch_and_close` → `_monitor_stop` → the stop-exit ladder)
is still driven live by the in-memory `_WATCHERS` dict for a *running*
process — that never changed, and never should (persistence must not add
latency to the actual trading logic). What's built on top is a
**checkpoint-and-resume** layer for when the process *isn't* running:

- **Checkpointing** (`_checkpoint_watcher`, `app/routes/torque.py`) — a
  fire-and-forget write to Supabase's `torque_watchers` table (see
  `supabase/migrations/0014_torque_signal_persistence.sql`) at every
  meaningful state transition (entry filled, close placed, each stop
  requote, market fallback, terminal states) — never on a price tick, and
  never awaited by the caller: a slow or unreachable Supabase must never
  delay or break a live order. Skipped entirely for a non-per-user
  (dev/admin/house) watcher — those aren't tied to a real Supabase uid.
- **Recovery** (`resume_active_torque_watchers`, called once at backend
  startup in `main.py`) — queries every not-yet-`done` row and resumes each
  independently; one account with a broker connection revoked since its last
  checkpoint is marked `stop_needs_attention` and skipped, never allowed to
  block the rest. Resumption is deliberately conservative, because
  `_checkpoint_watcher` is fire-and-forget and never retried — a real close
  or stop-exit can land at the broker while the write recording it fails or
  simply never lands, leaving the row stuck one step behind indefinitely (not
  just a millisecond race). A watcher whose row carries a specific resting
  order id (`stop_placed`/`stop_requoted`) **never blindly restarts breach
  detection** — it resolves that order's ground truth first
  (`_resolve_stalled_stop`, the same function the ladder itself uses for
  every in-process stall) and only then decides: already filled → done;
  confirmed dead → exactly one fresh bounded re-cross; state unconfirmable →
  `stop_needs_attention`, refuse to guess. A row with **no** order id to
  ground-truth against (`watching_fill`, or `close_placed`/
  `stop_blocked_wide_market` before a stop has ever been recorded) doesn't
  get to assume the stale state is accurate either — it first **reconciles
  with the broker's own order list** (`_find_resting_order`) and adopts any
  already-resting order it finds instead of placing a new one; only when
  nothing turns up does it fall back to placing fresh (`_watch_and_close` /
  `_monitor_stop`), exactly like a genuinely new watcher. The match is scoped
  to **this specific entry**, not just the option symbol or tag prefix: every
  close/stop tag has the entry's own order id stamped into it
  (`_close_tag` — `torqueClose<entry_order_id><strategy>`, trimming or
  dropping the strategy suffix first if the ~30-char tag budget is tight) and
  `_find_resting_order` recomputes and matches that exact tag. Without this,
  an old **filled** close/stop from an *earlier, unrelated* trade in the same
  contract — routine with 0DTE strikes reused across signals in one session —
  could be mistaken for "this position already closed/stopped out" and
  silently leave the current one with no exit at all.
- **Why Supabase, not the backend's own DuckDB** — these are low-frequency,
  low-volume rows (checkpoints at transitions, not ticks), where a network
  round-trip is a non-issue, and DuckDB is single-process by design: it
  cannot safely be shared across multiple backend containers, which this
  state needs to survive as much as a single-process restart.
- **Purge** — piggybacked on `poller.py`'s own open→closed transition (the
  same background loop that already recomputes market state every cycle,
  independent of any browser being open), not a second standalone scheduler.
  `supabase_admin.purge_stale_torque_records()` drops claimed news signals,
  their order-correlation rows (`torque_orders` — see News-signal ingestion,
  above), and `done=true` watcher rows older than **24h** — matching the
  Orders panel's own "Past Orders" window, so persisted state never outlives
  what the UI itself already treats as current.

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
- `market/backend/app/torque_config.py` — strategy registry, global defaults, and the in-memory
  copy of `torque_tickers_setup` (per-ticker settings, refreshed from Supabase).
- `supabase/migrations/0021_torque_tickers_setup.sql` — the per-ticker table + seed.
- Reuses `order_builder.py` (multileg payloads) and the async `TradierClient`
  (`quotes`, `place_order`, `get_order` added for Torque).

## DJX (Dow) — DJXW root only

DJX is Cboe's 1/100th-DJIA index option (spot ~525 when the Dow is ~52,500).
Torque trades **only the `DJXW` root** — the daily/weekly, PM-settled listing
(dailies launched 2026-05-18). The AM-settled monthly `DJX` root is excluded via
`allowed_roots` on DJX's `torque_tickers_setup` row, and any expiration that lists *only* that root
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
  DJX's `torque_tickers_setup` row sets `close_target_default 60 / close_target_min 45` and marks the ticker
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
* **Auto-close is MANDATORY on DJX** (`close_target_required` on DJX's row). The
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
  Config: DJX's row in `torque_tickers_setup` (`stop_loss_default = 50`).
* **Market orders are disabled for DJX** (`market_orders_allowed = false` on DJX's row), enforced in
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

Rates (`commission_per_contract` in `torque_tickers_setup`) were read off Tradier's own order-preview
`commission` field against a live production account (sandbox agrees):

| | 1 contract | 10 contracts | 2-leg vertical |
|---|---|---|---|
| DJX | 0.53 | 5.30 | 1.06 |
| SPX | 0.95 | — | — |

Preview reports `fees = 0` — regulatory pass-throughs (ORF, TAF) are assessed at
settlement, not at order time. We do **not** invent them; `extra_fee_per_contract`
defaults to `0.0` and is configurable per ticker (`extra_fee_per_contract` in `torque_tickers_setup`).

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
| Auto-close / stop-loss | ✅ | ❌ 501 — needs order-status polling |
| Native OTO/OTOCO bracket | ✅ | ❌ (no OCO in this SDK surface) |
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
premium-optimized wing selection for condors/flys; a broker-native OCO
substitute for spread stop-losses, if Tradier ever adds one (today the
`_monitor_stop` reactive watcher is the only option — see
[Auto-close + stop-loss](#auto-close--stop-loss-hybrid-30-default)).
