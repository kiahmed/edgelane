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
recommendation. See [Reading the market](#reading-the-market-positioning--flow).

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
  Read it as positioning context only. Full breakdown:
  [Reading the market](#reading-the-market-positioning--flow).

## Reading the market (Positioning + Flow)

This panel is the one piece of "analysis" in Torque. It does **not** predict
price — it describes **where options money is positioned now** and **where it's
moving this minute**, then leaves the trade to you. Two layers:

### Layer 1 — the Lean (state: where positioning *sits*)

The big signed number (−100 … +100) is a deterministic heuristic blend of three
things in the current chain (±3% around spot):

| Component | What it measures | Weight |
|---|---|---|
| Volume P/C | call vs put **contracts traded today** | 40% |
| **Premium skew** *(tile)* | call vs put **$ resting in open interest** (mid × OI) | 30% |
| **Magnet** *(tile)* | pull toward the most **OI-concentrated strike** near spot | 30% |

(These are the Lean's *inputs*; only **Premium skew** and **Magnet** still show as
tiles — the `Volume P/C` and `OI P/C` tiles were replaced by the **Session flow**
block, see Layer 3.)

Thresholds: **≥ +25 → call-leaning**, **≤ −25 → put-leaning**, in between →
**balanced** (deliberately no side — a small score isn't an edge). The bar under
the number is the same value drawn left↔right. Hover the number for the formula.

Think of the Lean as **where the crowd already is**. It's slow — built from
standing positioning — so on its own it can be *stale* (call-heavy because of
yesterday, not because of now).

### Layer 2 — the Flow (momentum: where money is moving *now*)

The **flow** block reports the **premium $ of *new* contracts traded** on calls
vs puts over the last 3 minutes (`calls +$1.2M · puts +$0.4M · net +$0.8M call`).
It's kept in **browser memory for this tab only** (cleared on refresh), sampled
every 5s.

Why volume, not the premium-skew $? Because **open interest only settles
overnight** — intraday the premium-skew number moves mostly because price moved,
so its "rate of change" would just echo the chart. **Volume is intraday and
cumulative**, so its delta is real, fresh order flow.

**How it's measured (and why `$0` is meaningful):** each side's figure is
`Δvolume × current $-per-contract` — the *new contracts* in the window, valued at
the side's current average premium. It deliberately does **not** diff
`mid × cumulative-volume`, because a mid tick would then revalue the *whole day's*
volume and leak the price move into the "flow" (e.g. price drops → put mids rise →
a fake `+$1.5M` put flow that's really just repricing). Because cumulative volume
only rises, each side is monotonic, so **`+$0` on a side means exactly one thing:
zero new contracts traded there in the window** — not "mids fell." A one-sided
read like `calls +$0 / puts +$1.5M` is then a genuine *"all the fresh flow is
hitting puts"* — which, under a call-leaning Lean, is a real **diverging** signal.

### The tag that ties them together: confirms / diverging

The little tag on the Lean (and the word on the net-flow line) compares the two
layers:

- **confirms** — fresh money is flowing the **same** side as the Lean → the lean
  is **live**, the move has fuel.
- **diverging** — money is flowing the **opposite** side → the lean is being
  **faded**, possible exhaustion.
- **flat** — no decisive net flow (within ~6% of total) → chop.
- On a **balanced** lean there's nothing to confirm, so it just names the side
  the money is hitting (`call flow` / `put flow`) — an early tell before the
  Lean itself tips.

**`call flow`/`put flow` and `confirms`/`diverging` are different states, but the
first often *matures* into the second.** On a **balanced** lean, persistent
`put flow` is usually the **precursor**: as that flow keeps up it drags the Lean
itself down into **put-leaning**, and then the *same* flow reads as **confirms**.
So `put flow → confirms` (and `call flow → confirms`) is frequently **one move
maturing**, not two unrelated signals — watching that hand-off is an early read on
a lean that's about to establish.

**The key idea:** *the Lean tells you where the crowd is; the Flow tells you
whether the crowd is still right.* A **small lean that flow confirms** is often
more tradeable than a **big lean that flow is fading** — the first is fresh
momentum with room to run, the second is a crowded position the money is already
leaving.

### How to read every combination

Rows = the Lean (where positioning sits). Columns = the Flow tag.

| Lean ↓ \ Flow → | **Confirms** (money same side) | **Flat** (no net flow) | **Diverges** (money opposite) |
|---|---|---|---|
| **Strong call** (+50+) | Crowded long **and still being bought** — trend has fuel, but late-stage; watch for a blow-off top. | Extended long, no fresh fuel — likely to drift/stall. | ⚠ **Exhaustion / reversal risk** — heavily call-positioned but money rotating to puts. Fade or stand aside. |
| **Mild call** (+25–50) | **Sweet spot** — fresh call bias *with money behind it*. Cleanest long read. | Weak call tilt, no momentum — low conviction. | No edge — a mild lean already being faded. Stay flat. |
| **Balanced** (−25…+25) | *(call flow)* money picking the call side **before** positioning shows it — early bullish tell. | True **range / chop** — premium-selling turf (Condor / Fly). | *(put flow)* early put-side tell forming under a flat surface. |
| **Mild put** (−25–−50) | **Sweet spot down** — fresh put bias with money behind it. Cleanest short read. | Weak put tilt, no momentum. | No edge — mild put lean being faded. |
| **Strong put** (−50+) | Crowded short **and still being sold** — fuel, but late-stage. | Extended short, no fresh fuel. | ⚠ **Exhaustion / bounce risk** — heavily put-positioned but money rotating to calls. |

None of these is a "buy/sell" instruction — they're context for the structure
*you* already had in mind. Torque's job is to make that read fast and then
execute cleanly.

### Layer 3 — Session flow (trend vs chop over the day)

The 3m flow is the tape; a single reading is noisy and swings side to side. The
**Session flow** block integrates it over a **trailing 60 minutes** — a
**cumulative delta (CVD)** in premium dollars — to answer the mid-day question:
*is there a persistent direction, or is this just chop?* It shows:

- **Net $** — `Σ(call$ − put$)` over the hour. Steadily one way = sustained
  pressure. A lone opposite print (your `+$1M` call against `−$42M` puts) barely
  dents it — **the noise filters itself.**
- **Trend-strength bar + regime** — judged by **persistence**, i.e. how many of
  the 3-min windows in the hour agree on a side (`X/Y windows put-side`):
  - **mostly one side** (≳70%) → **`trending put` / `trending call`** → a
    directional debit spread is in play, and a small counter-print is a
    pullback/entry, not a reversal.
  - **~50/50, sign flipping** → **`chop / range`** → two-sided, rangebound →
    **Iron Condor / Fly** territory.
  - in between → **`mixed`** (building or transitioning).
  - *Why persistence, not `|net|/gross`?* A **mild but relentless** drift (every
    window slightly call-side) is a *trend* even though its net/gross is low —
    counting windows catches that; a raw lopsidedness ratio would miscall it chop.
- **`% one-sided flow`** — the `|net|/gross` ratio, shown as a secondary stat:
  how lopsided the raw flow is (size of the edge), distinct from how *persistent*
  it is (the bar).

Regime turnover — e.g. a `chop → trending put` shift, or the trend-strength bar
climbing off a flat base — is your **early mid-day regime-change** cue. As always:
this is *pressure*, not a forecast; dealers can absorb sustained flow.

**Browser-only, this tab** — the 60m series (and the 3m flow) live in memory and
clear on refresh; they're per-tab, so two tabs on different tickers each keep their
own. (Past Orders, by contrast, is account-wide — see the Orders panel.)

**Market-hours pause.** Torque only ever trades **same-day (0DTE)** premiums, which
stop at 16:00 ET — so in **production** *all* polling (positioning, flow, live
reprice, **and** the orders panel) **pauses whenever the market isn't in its regular
session**, and resumes automatically when it reopens. Nothing fills outside RTH, so
there's nothing to poll for. The header shows `MARKET <state> · PAUSED`, the session
flow **freezes** (rather than resetting), and the orders panel is fetched **once** at
the pause (so resting GTC orders still show) then held — **cancel/modify still work**
(broker actions, not polls).

The open/closed signal is **Yahoo's live `marketState`** (`GET /torque/clock`,
cached ~30 s, polled slowly and always so a reopen is noticed) — which reflects the
real exchange session, so **holidays and early closes are handled automatically with
no hardcoded calendar**. On the rare Yahoo failure it **falls back** to a local
weekday + 09:30–16:00 ET check. In **sandbox/dev** (`make run-dev`) nothing pauses,
so the tool stays testable off-hours (mirrors the backend's DEVMODE gating). Note:
Torque is **stateless** — none of this is persisted; it holds no DuckDB, unlike the
market poller/evaluator.

### The other tiles & levels

- **Session flow replaced the `Volume P/C` + `OI P/C` tiles.** `OI P/C` was
  overnight-static (near-useless intraday) and `Volume P/C` was just an unweighted,
  windowless cumulative-flow proxy — the CVD net + trend-strength strictly
  dominate both.
- **Premium skew** *(tile)* — call vs put **$ resting in OI**; the standing dollar
  balance (a Lean input).
- **Magnet** *(tile)* — the near-spot strike with the most concentrated OI, and its
  distance from spot; a tight near-spot magnet is what flips the balanced
  auto-pick from **Iron Condor** to **Iron Fly** (sell the pin).
- **premium / OI clusters** pill — the side OI/premium leans; for single-leg
  strategies a **use →** button drops you straight into Long Call / Long Put.

### Does the auto-selected strategy keep changing?

**No.** The grid **auto-lands once per ticker on load**, then stays put — your
manual picks stick and it never re-selects underneath you while you build. The
live **`positioning now: …`** caption beside "Strategy" *does* update with the
Lean, but it's **context only** ("where the market sits, not a recommended
play") — it changes the words, not your selection.

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
| `GET /torque/clock` | `{market_state, open}` from Yahoo `marketState` (holiday/early-close aware; cached ~30s success / ~8s failure so an outage doesn't re-hit each poll) — gates the pause |
| `GET /torque/analyze/{sym}` | spot + positioning snapshot (poll ~5s) |
| `POST /torque/build` | auto-filled legs for (ticker, strategy, step adjustments) |
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
