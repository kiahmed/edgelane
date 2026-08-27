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

**Now add the Session bar's two axes.** The table above reads the *3-minute* flow
against the Lean. The **Session flow bar** (Layer 3, below) carries the same
call/put *side* but adds two **independent** facts the 3m tag can't:

- **Length = magnitude** — how hard the hour's net is pushing *right now*
  (`|net|/gross`). Longer = harder.
- **Color = commitment** — how *persistent* that push is: **solid green/red** once
  ≳70% of the 3-min windows agree, **amber** while contested/building.

Because they're independent, **read them separately.** A **longer amber** bar (hard
but not yet trusted) sitting next to a **shorter solid-red** bar (milder but
persistent) is a normal, informative pairing — **not** a contradiction. Length
answers *"how hard now?"*; color answers *"can I trust the direction?"* A short,
committed drift is more tradeable than a long, contested lurch — so **don't read a
longer/higher-% bar as "closer to red."** The `%` only sets length; only the window
count (persistence) turns it solid.

Crossing the **session bar's color** with the **Lean** gives the persistent read
(side of the bar vs side of the Lean):

| Session bar vs Lean | **Solid** (persistent) | **Amber** (contested / building) |
|---|---|---|
| **Confirms** (bar side = Lean) | **Strongest continuation** — the book sits one way *and* the hour keeps pushing it. Ride the directional debit; a small counter-print is a pullback, not a reversal. | Flow agrees with the lean but isn't sticking → **chop around an established lean** → Condor / Fly, not a trend. |
| **Diverges** (bar side ≠ Lean) | ⚠ **Early rotation — the most valuable contradiction.** Positioning still sits one way but persistent hourly flow is *fading* it. Flow leads, OI/premium lag, so a regime change often starts **here, before the Lean has repriced.** | **Tentative fade** — likely noise testing the lean. Watch `held` and the window count; don't act until it hardens to solid. |

So when the two bars **agree by side**, conviction is high; when they **split**, let
the session bar's **color** break the tie — *diverges + solid* is the book about to
be dragged, *diverges + amber* is just chop testing it. Always correlate the bars by
**side, never by their raw numbers** — the Lean is a −100…+100 composite, the bar `%`
is `|net|/gross` of flow dollars; different scales, so only their direction is
comparable.

None of these is a "buy/sell" instruction — they're context for the structure
*you* already had in mind. Torque's job is to make that read fast and then
execute cleanly.

### Putting it together — flow leads, positioning lags

The three reads update at **different speeds**, and that lag *is* the signal.
Fastest to slowest:

| Tab | What it shows | Speed | Example (an SPX day) |
|---|---|---|---|
| **Session flow bar** | fresh money over the trailing hour (flow) | **fastest** — flips first | **solid green, trending call** |
| **Lean** | where the whole book sits (blended snapshot) | medium | still **balanced (0)** |
| **Premium skew** | resting option richness (positioning) | **slowest** — repositions last | still **puts rich** |

Read it top-to-bottom: **flow leads, positioning lags.** When the fast tab has moved
but the slow ones haven't, you're watching a rotation *in progress* — not yet a done
deal. The gap between the tabs is where early rotations live — and where fakeouts hide.

**A worked life-cycle** (a full regime turn, top of the day to the flip):

1. **Strong put regime** — session bar solid red, high `%`, skew puts-rich, Lean
   put-leaning. Every tab agrees → high-conviction short.
2. **Bleed-out** — bar shrinks (e.g. 60% → 16%), red → **amber**, windows slip under
   70%. Puts losing grip; magnitude *and* persistence fading together.
3. **Flip zone** — `%` drops under 10%, bar **flashes amber** through parity. No
   trusted side → chop / indecision.
4. **New side commits** — `%` re-grows on the other side, bar hardens to **solid
   green, trending call**. The *flow* has rotated.
5. **Positioning still lagging** — Lean stuck **balanced**, skew still **puts rich**.
   The *book* hasn't repriced, so the tag reads **`call flow`, not `confirms`.**

Step 5 is the state to recognize: **the tape has flipped, the book hasn't.**

**Compare / contrast — the two ways step 5 resolves:**

| | **Confirmed rotation** | **Fakeout** |
|---|---|---|
| Session bar | stays solid green, holds / widens | shrinks back toward 10%, **re-flashes** |
| Premium skew | eases off puts-rich → neutral / calls | stays puts-rich |
| Lean | climbs and crosses **+25** → tag flips to **`confirms`** | never crosses; drifts back |
| Read | flow *and* book now agree — **validated** | flow-only pop; the puts-rich book was right, puts can reassert |

**What actually flips the tag to `confirms`:** the **Lean crossing ±25** — *not* any
single tab. Skew is only ~30% of the Lean, so it doesn't have to go fully call-side;
it just has to stop dragging hard enough that call volume + OI-magnet carry the Lean
past the line. In practice skew is usually the heaviest anchor, so **it's the one to
watch first** — but the literal trigger is the Lean number, not the skew label.

**What to watch, in order:**

1. **Premium skew** — the first thing that has to give; `puts rich → neutral` is the
   earliest tell that the book is starting to agree with the flow.
2. **Lean number** — `0 → +25` (or `→ −25`) is the literal trigger; the tag turns
   `call flow → confirms` there.
3. **Session bar** — must *hold* solid green meanwhile. If it fades and **re-flashes**
   before the Lean crosses, the rotation failed — treat it as a fakeout.

**One-line rule:** *when all three tabs agree, conviction is highest; when they split,
the flow tells you a move is **starting**, the skew and Lean tell you it's **real**.*

### Verdict cheat-sheet — reading the whole board at once

Quick refreshers for the cross-tab states, as verdicts to come back to. Each is a
**prior, not a promise** — the named **invalidation** is what tells you the prior is
breaking. *(Most of this is a read you assemble across tabs — the tags surface the
raw pieces. The one exception is the **book-leads / hour-flow-lags** case below, which
Torque now flags for you as a slim **⚡ STRETCHED** chip in the Positioning panel when
the full config lines up.)*

- **All tabs agree** — session bar, Lean, skew, and 3m flow all the same side.
  → **Highest conviction**; ride the directional debit; a small counter-print is a
  pullback, not a reversal. *Invalidation:* 3m flow flips **and** the session bar
  starts shrinking off its high.

- **Flow leads, book lags** — the session bar has flipped **solid** to a new side,
  but the Lean is still balanced and skew still reads the *old* side *(the SPX case)*.
  → Rotation **in progress, not yet real.** **Verdict:** wait for the Lean to cross
  ±25 and the tag to turn `confirms`. *Invalidation:* the session bar fades and
  **re-flashes** before the Lean crosses → it was a flow-only pop (**fakeout**).

- **Book leads, hour-flow lags** — the Lean, skew, *and* freshest 3m flow are all one
  side, but the 60m **session bar is still deep the *other* way** *(the NDX case:
  Lean +34 / calls-rich / 3m call, yet session net −$18.6M trending put)*.
  → The hour's flow is **fighting the entire structure.** **Verdict / prior:** the
  tension **tends to resolve by flow reverting *toward* positioning** — the session
  bar grinds back and flips — *not* by the book collapsing to meet the flow. Two
  reasons: the resting book (OI / skew / magnet) is where dealers defend, and the 60m
  residue **decays as its oldest prints age out of the window** (so a big negative net
  under a `held 55m` regime is about to start deflating from the back, not just the
  front). So a deeply-negative session net **under a call-leaning, calls-rich book
  reads as room to recover**, not a reason to keep pressing the losing side.
  *Invalidation — the one that flips it to a **real regime change**:* **skew
  capitulates** (calls-rich → puts-rich) and/or fresh 3m flow swings back to the
  session-bar side. **Until skew gives, the outnumbered side is fighting uphill.**
  *(Torque flags exactly this config with a slim **⚡ STRETCHED · {call/put} favored**
  chip in the Positioning panel — amber, appears only when the session bar is trending
  one way while the Lean is committed, skew is rich, and the 3m flow has turned the
  other way. Hover it for the full read + invalidation. It's a state, not advice.)*

- **Chop / contested** — session bar amber (contested near the flip), Lean balanced,
  3m flow flat. → No trusted side → **premium-selling / range** (Condor / Fly), not a
  directional debit. *Invalidation:* the bar hardens solid off the flip and windows
  push past 70% one way → a trend is establishing.

> **Prior, not law.** The "flow reverts toward positioning" verdict is an intraday
> *tendency* (price pins toward dealer positioning; the 60m residue decays), **not a
> guarantee.** On a true trend day positioning **does** collapse to meet the flow —
> that *is* a regime change — and the **skew flip is your early warning** that you're
> in that case, not the reversion case. Trade the prior; respect the invalidation.

### Layer 3 — Session flow (trend vs chop over the day)

The 3m flow is the tape; a single reading is noisy and swings side to side. The
**Session flow** block integrates it over a **trailing 60 minutes** — a
**cumulative delta (CVD)** in premium dollars — to answer the mid-day question:
*is there a persistent direction, or is this just chop?* It shows:

- **Net $** — `Σ(call$ − put$)` over the hour. Steadily one way = sustained
  pressure. A lone opposite print (your `+$1M` call against `−$42M` puts) barely
  dents it — **the noise filters itself.**
- **Signed flow bar + commitment color** — one bar carries two facts at once:
  - **Length = magnitude past the flip.** A center line marks the **flip point**
    (call/put parity). The fill grows *from* center — **right / green = call**
    pressure, **left / red = put** — and its length is the `|net|/gross` ratio (how
    far past the flip the hour's net currently sits).
  - **Color = how committed it is.** **Amber & pulsing** when the net is within
    ~10% of the flip line (**contested** — the sign isn't trustworthy yet);
    **solid green/red** once it's persistent (≳70% of windows agree) *and* clear of
    the flip; **amber steady** in between (**building**).
- **Persistence (`X/Y windows put-side`)** — how many of the 3-min windows in the
  hour agree on a side. This is what decides **trend vs chop** (and colors the bar):
  - **mostly one side** (≳70%) → **`trending put` / `trending call`** → a
    directional debit spread is in play, and a small counter-print is a
    pullback/entry, not a reversal.
  - **~50/50, sign flipping** → **`chop / range`** → two-sided, rangebound →
    **Iron Condor / Fly** territory.
  - in between → **`building`** (transitioning).
  - *Why persistence, not `|net|/gross`?* A **mild but relentless** drift (every
    window slightly call-side) is a *trend* even though its net/gross is low —
    counting windows catches that; a raw lopsidedness ratio would miscall it chop.
- **`% call/put dominance`** *(was `% one-sided`)* — the `|net|/gross` ratio: how
  lopsided the raw flow is (size of the edge), labelled by the side the net sits on
  (`call dominance` when net is call-side, `put dominance` when put-side). Distinct
  from how *persistent* it is (the windows). **Read it as a live magnitude, not a
  running total** — see the box below.
- **`held Nm`** — how long since the hour's net last **crossed zero** (the flip).
  This is the *only* "since the flip" number; the `%` is **not**.

> **What the `%` is — and isn't.** The `%` is `|net| / gross` computed over the
> **trailing 60 minutes** (a rolling window) — the **current** magnitude of the
> hour's net call/put imbalance. It is **not** an accumulation "since the last flip,"
> and it is **not** unbounded: increments older than 60 min continuously drop out of
> the window, so it always reflects roughly the last hour — whether or not a flip
> happened inside it. When the net drifts back toward parity the `%` **falls toward
> 0**; if it crosses, the side label flips, `held` resets to `<1m`, and the `%`
> re-grows on the *other* side. So a rising `%` means the *current* hourly imbalance
> is widening — it says nothing about how long it's held (that's what `held` is for).

Regime turnover — e.g. a `contested → trending put` shift, the bar **hardening from
amber-pulse to solid** as it clears the flip line, or `held` resetting while the fill
re-grows on the other side — is your **early mid-day regime-change** cue. As always:
this is *pressure*, not a forecast; dealers can absorb sustained flow. To read this
bar's **color and length against the Lean**, see the bar-vs-Lean matrix in
[*How to read every combination*](#how-to-read-every-combination) above — length is
magnitude, color is commitment, and the two are independent.

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

### Layer 4 — Counter Read (relative richness vs the opposite structure)

Everything above reads the market. **Counter Read reads the *other side of your own
trade*** — a live band, in the order-builder panel itself, showing whether premium
is leaning toward the structure you're building or its natural opposite, so a
richening counter-side shows up **before** you send, not after you're already in
the wrong one.

**Why this isn't redundant with put-call parity.** A vertical and its mirror at the
*identical* strikes are locked together by conversion arbitrage — one side richening
necessarily cheapens the other by the same amount, so comparing them would carry no
information beyond what one side's mid already tells you. Torque's counter side
uses its **own independently-configured strikes** (call-side and put-side offsets
are configured separately per ticker), so the two sides are priced off genuinely
separate parts of the chain — this is a live, delta-matched **risk-reversal** read
(the same skew concept behind the Premium skew tile), not an identity.

**Pairing** — every strategy has exactly one counter, same "shape," opposite side.
Iron Condor / Iron Fly have no separate counter build — both wings are already in
the structure, so the comparison is *within* the trade, not against another one:

| Current | Counter |
|---|---|
| Long Call | Long Put |
| Bull Call | Bear Put |
| Bull Put | Bear Call |
| Call Fly | Put Fly |
| Iron Condor | *(self)* bear-call wing vs bull-put wing |
| Iron Fly | *(self)* call wing vs put wing |

**Data source — no separate poll.** `POST /torque/build` returns a `counter` field
alongside the primary structure: the opposite strategy's `legs` + `price`, built
from the **same already-fetched chain and the same anchor spot** as the primary
build (`null` for Iron Condor/Fly, where both wings are already in `legs`). Counter
Read piggybacks on the **existing 3s rebuild** — it is not a new timer, new request,
or new poll cadence; it just reads one more field off the response that already
arrives every tick.

**Richness (per side).** For a 2+-leg side: `abs(Σ signed leg mids) / width` — width
is the strike distance within that side (near↔far, or wing body↔wing), which makes
a $2 mid on a 10-pt spread and $18 on a 100-pt spread comparable. For a single-leg
side (Long Call vs Long Put): richness is just that leg's raw mid — nothing to
normalize against. A side with a missing/incomplete leg quote or zero width skips
that tick and holds its last good reading (same "never show a partial number"
convention as the net price card).

**Band position — an instant ratio, lightly smoothed.** `ratio = richness(current) /
(richness(current) + richness(counter))`, 0.5 = balanced. This is a **level**, not
an accumulation — the mid prices behind it don't carry the print-spike noise volume
flow does — so it does **not** use the Session Flow's 3-minute bucket scheme. It
uses a short time-based EMA (**τ ≈ 15s**) to kill bid/ask jitter, giving a slider
that's near-live without dancing on every tick.

**Color — a continuous hold-timer since the ratio last crossed center**, the same
flip-timer pattern as Session Flow's `held`, adapted to a continuous value instead
of window-counting:
- **± 8 points of 50%** (42–58%) → **contested**, amber & pulsing — too close to
  center to trust a side.
- **outside that band, held < 60s** → **building**, amber steady — leaning, not
  yet trusted.
- **outside that band, held ≥ 60s** → **committed**, solid — colored by **option
  side**, the same call=green/put=red convention used everywhere else in Torque
  (leg badges, the skew tile) — *not* a bullish/bearish framing, since a credit
  side's side-label (e.g. Bull Put is a *put* structure) doesn't always match its
  directional bias.
- Resets on ticker/strategy change, same as the other flow state.

**Reading it.** Caption reads `leaning {side} · {state}` (+ `held Nm` once
committed, via the same `fmtHeld` helper as Session Flow). This is a **relative-value
lean, not a forecast** — it tells you where premium is currently being bid between
the two structures, not which way price will go. A counter side that's richening
and **committed** while you're about to send the other side is the exact
"don't-enter-blind" case this was built for; use it to adjust strikes/distance, not
as a standalone signal.

**Placement.** The band sits in the previously-empty space beside the Limit
Price/Qty/TIF/auto-close controls and the Send row, inside the order-builder panel
— it does not add a row, does not resize or reflow the existing controls, and is
hidden below a narrow-viewport breakpoint rather than wrapping underneath them.

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
| `POST /torque/build` | auto-filled legs for (ticker, strategy, step adjustments); optional `anchor_spot` freezes the strikes (Lock Strikes); also returns `counter` — the opposite structure's legs+price from the same chain/spot, `null` for Iron Condor/Fly (see [Counter Read](#layer-4--counter-read-relative-richness-vs-the-opposite-structure)) |
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
