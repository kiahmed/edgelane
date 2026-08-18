# Simmer — premium-selling readiness engine

> *Let it simmer. We'll tell you when it's ready.*

Simmer is the third EdgeLane product surface, alongside the **market dashboard**
(Matrix) and **Torque**. You give it a watchlist. It watches those names
continuously, and tells you **when a name is ready to have premium sold against
it** — and at which strikes, for one expiration you choose.

It is not a spread *optimizer* (that's Matrix) and it is not an order *builder*
(that's Torque). Simmer answers one question, repeatedly, per ticker:

> **Is this name cooked yet — and if so, what do I sell?**

The output is a ranked, gated recommendation for a **credit spread** (bull put,
bear call, or iron condor) whose short strike sits outside both the expected
move and the structural dealer wall, sold when volatility is rich enough to pay
for the risk.

---

## Table of contents

- [Scope and non-goals](#scope-and-non-goals)
- [Where the code lives](#where-the-code-lives)
- [Auth, product gating, and cross-app SSO](#auth-product-gating-and-cross-app-sso)
- [Data model](#data-model)
- [Backend API surface](#backend-api-surface)
- [The readiness engine](#the-readiness-engine)
- [Data sourcing](#data-sourcing)
- [The watcher loop](#the-watcher-loop)
- [Alerts](#alerts)
- [User settings and overrides](#user-settings-and-overrides)
- [Frontend](#frontend)
- [Deployment](#deployment)
- [Configuration](#configuration)
- [Testing](#testing)
- [Gap analysis and open risks](#gap-analysis-and-open-risks)
- [Phased build plan](#phased-build-plan)

---

## Scope and non-goals

### In scope

- Per-user watchlist of equity/ETF tickers, persisted server-side.
- Continuous (5-minute) evaluation of each watched ticker.
- A **composite readiness score** per ticker per expiration, built from
  volatility richness, structural walls, expected move, news sentiment,
  catalyst proximity, and liquidity.
- **Hard gates** that veto a name outright regardless of score (unpriced binary
  catalyst, IV too low, chain too illiquid).
- A concrete **suggested credit spread**: structure, short strike, long strike,
  width, credit, max loss, POP, and the reasoning behind each.
- **Alerting** when a watched name crosses into "ready".
- Single-expiration selection: auto-picked by default, user-overridable, **one
  expiry at a time** per ticker.

### Explicitly out of scope (v1)

- **Order placement.** Simmer recommends; Torque places. A "Send to Torque"
  hand-off is the integration point, not an embedded order ticket.
- **Directional trading.** Simmer only sells premium. It never suggests debit
  structures or naked directional plays.
- **Backtesting UI.** The outcome-evaluation loop records results (see
  [Testing](#testing)), but a backtest explorer is a later product.
- **Index products entirely** (SPX/NDX/SPY/QQQ/IWM, 0DTE or otherwise). That is
  Matrix's domain. Simmer targets multi-day **single-name** equity premium
  capture. **Decided 2026-08-15** — see the note below, because this decision has
  a real cost that the design must compensate for.
- **Portfolio/position management.** No position sizing across a book, no
  correlation limits, no margin modelling in v1.

### The honest limitation

Simmer's directional inputs (news sentiment, order-flow proxies) are **weak
signals over a 1–5 day horizon**. Its structural inputs (IV rank, expected move,
GEX walls, liquidity) are **strong**. The engine is therefore weighted so that
structure decides *whether* to sell and *where*, while sentiment only decides
*which side* to lean and can **veto** but never **promote**. This is stated up
front because it is the single most important design decision in the product,
and it is what keeps Simmer from becoming a sentiment toy.

### The cost of excluding indexes, and how the design compensates

Excluding index products is a deliberate product boundary, but it forfeits the
one variance risk premium the literature actually supports. Driessen, Maenhout &
Vilkov find a significantly negative VRP for the S&P 100 while **individual-stock
variance risk premia are "often zero or even positive"** — the index premium is a
*correlation* risk premium, and correlation risk does not exist in a single name.

So Simmer is **not harvesting a market-wide premium.** It cannot be, by
construction. What it does instead:

1. **Cross-sectional selection, not average capture.** Ranking `IV30 / RV20`
   *within the user's watchlist* does not require a positive average premium to
   exist — only that some names are richer than others *today*. A far weaker and
   more defensible claim.
2. **Refusal as the primary output.** If the average single name offers no edge,
   the value is in rejecting the ~85–90% that clearly don't and surfacing only
   the rich tail.
3. **Left-tail removal via the catalyst veto.** In a negative-skew carry
   strategy, removing blowups is worth more than collecting extra premium. This
   is the most plausible source of real edge in the product.

**Consequence for validation:** the cross-sectional VRP percentile must be shown
to discriminate out-of-sample *net of costs* before this product can claim to
work. That is why outcome recording moves to Phase 2 — see the build plan.

---

## Where the code lives

Simmer follows the repo's existing product-folder convention (`market/`), with
one deliberate deviation documented below.

```
simmer/
  ui/                          # SvelteKit 5 SPA -> edgelane-simmer.vercel.app
    src/
      lib/
        components/            # ReadinessCard, WatchlistManager, ...
        charts/                # GexWallChart, IvGauge, ExpectedMoveViz
        stores/                # auth.svelte.ts, watchlist.svelte.ts, poll.svelte.ts
        api.ts  config.ts  supabase.ts  sso.ts  fmt.ts
      routes/                  # +layout, +page, settings/, sso/
    static/edgelane.config.js  # placeholder, overwritten at deploy
    svelte.config.js  vite.config.ts  vercel.json

market/backend/app/
  simmer_engine.py             # readiness scoring (pure, testable)
  simmer_config.py             # per-ticker + global knobs (Torque-config pattern)
  simmer_news.py               # news fetch + Gemini sentiment client
  simmer_watcher.py            # the 5-minute background loop
  routes/simmer.py             # all /simmer/* endpoints

supabase/migrations/
  0010_simmer.sql              # watchlists, alerts, settings, RLS

docs/simmer.md                 # this file
```

### Why the backend lives under `market/backend/`

The requirement says *"put its code in new simmer folder in the root like
market"*. The **frontend** does exactly that. The **backend** deliberately does
not, for a concrete reason:

`deploy/docker-compose.yml` builds the backend image with build context
`../market/backend`, and `market/backend/Dockerfile` copies only `app/` and
`ui/`. A Python package at repo-root `simmer/backend/` is **outside that build
context** and would not ship. Fixing that means moving the build context to the
repo root and adding a `.dockerignore`, which enlarges every backend build.

The requirement also says *"for backend it could use same backend engine for now
but on a different endpoint like /simmer"* — which is what this layout delivers.
It matches the **Torque precedent exactly** (`torque_engine.py`,
`torque_config.py`, `routes/torque.py` all live in the market backend while
Torque is its own product).

> If you'd rather have `simmer/backend/` literally, the change is: set compose
> `build.context: ..`, `dockerfile: market/backend/Dockerfile`, add a root
> `.dockerignore`, and `COPY simmer/backend ./simmer`. It works; it just costs
> build-context size on every image build. Flagging it as your call.

---

## Auth, product gating, and cross-app SSO

### Product gating — already built, nothing to migrate

`profiles.tools_enabled` is a `text[]` with **no enum constraint**
(`supabase/migrations/0009_tool_entitlements.sql`), and
`market/backend/app/entitlements.py::ensure_tool()` accepts an arbitrary string.
Adding the Simmer product therefore requires **no schema change**:

```python
# market/backend/app/routes/simmer.py
async def require_simmer_access(request: Request,
                                user: dict = Depends(get_current_user)) -> dict:
    await ensure_tool(user, "simmer")
    return user

_GATE = [Depends(require_simmer_access)]

@router.get("/simmer/watchlist", dependencies=_GATE)
async def get_watchlist(...): ...
```

Three touch points:

1. The gate above on every `/simmer/*` route.
2. Add `"simmer"` to the startup grant list in `main.py` (currently
   `grant_user_tools(_uid, ["market", "torque"])`).
3. Expose `tools_enabled` to the frontend so the UI can show a proper
   "not enabled" screen instead of a bare 403. **Today no frontend reads
   `tools_enabled`** — Matrix gates purely on session presence. Simmer should
   read it via the owner-scoped `profiles_select_own` RLS policy.

Enable/disable per user is then a single service-role array update — the
existing `grant_user_tools()` handles the union; a matching `revoke_user_tools()`
is a small addition.

### Cross-app SSO — the blocking architectural decision

**Requirement:** a user signed into Matrix who browses to Simmer should be let
straight in, and vice versa.

**Finding: this is impossible ambiently between two `*.vercel.app` origins.**
Vercel states that `vercel.app` is on the Public Suffix List, so a cookie cannot
be scoped to it. And every other shared-state mechanism is origin-scoped by
specification:

| Mechanism | Shared across the two apps? | Why |
|---|---|---|
| Cookie with `Domain=.vercel.app` | No | Browser rejects — PSL entry |
| Host-only cookie | No | Different hosts |
| `localStorage` / `sessionStorage` / IndexedDB | No | Origin-scoped by spec |
| `BroadcastChannel` | No | Origin-scoped |
| Hidden iframe + `postMessage` | No | Third-party storage partitioning (shipped in Chrome, Firefox TCP, Safari ITP) |

There are exactly two workable paths.

#### Option A — shared parent custom domain (target state)

Move both apps under one registrable domain: `app.edgelane.io` (Matrix) and
`simmer.edgelane.io` (Simmer), with the Supabase session in a cookie scoped
`Domain=.edgelane.io`. This is a **same-site** relationship, so third-party
cookie blocking and storage partitioning are irrelevant. Both apps must use an
identical storage adapter and the **same `storageKey`**
(`edgelane-auth-<projectref>`).

Cost: ~$15/yr and a Vercel domain attach per project (supported on Hobby).
This is the only genuinely transparent solution and it is the least code.

Caveats to plan for:
- **4 KB cookie limit.** A Supabase session (access JWT + refresh token + user
  object) routinely runs 2–6 KB. Use `@supabase/ssr`'s `createBrowserClient`,
  which chunks automatically, or hand-roll chunking. Keep `user_metadata` small.
- **Set `Max-Age` far out** — a cookie expiring before the session does causes
  silent logouts.
- **Refresh races.** Two origins can't share `navigator.locks`. Supabase's
  10-second refresh-token reuse interval absorbs this; do not set an aggressive
  custom refresh margin.

#### Option B — opaque one-time ticket hand-off (build this now)

Works on `*.vercel.app` **today**, and keeps working unchanged after a later
move to a custom domain — so it is never wasted work.

1. Matrix (signed in) calls `POST /simmer/sso/ticket` with its bearer JWT and
   `{target: "https://edgelane-simmer.vercel.app"}`.
2. Backend verifies the JWT (`auth.py` already does), runs
   `ensure_tool(user, "simmer")`, validates `target` against a **server-side**
   allow-list, and stores a 256-bit random ticket bound to
   `(uid, target, exp = now + 30s, used = false)`.
3. Browser redirects to `https://edgelane-simmer.vercel.app/sso#t=<ticket>`.
4. Simmer calls `POST /simmer/sso/redeem {ticket}`. The backend atomically marks
   it used and mints a **brand-new, independent** Supabase session for that uid
   via the service-role Admin API, which Simmer establishes client-side.

Each app ends up with its **own** refresh token, so Supabase's reuse detection
never fires. Nothing sensitive ever appears in a URL.

> **Do not** simply pass `access_token`/`refresh_token` in the URL fragment.
> Fragments land in browser history and are readable by extensions, and worse,
> both apps would then hold the *same* refresh token — outside the 10-second
> reuse window one side's rotation invalidates the other and **Supabase
> terminates the session for both**. That produces random logouts.

Ticket hygiene: single-use, ≤30 s TTL, ≥128 bits entropy, bound to uid *and*
target origin, allow-list server-side only, and rate-limited through the
existing `ratelimit.py`.

#### The conflict you must resolve

Matrix stores its session in **`sessionStorage`**, deliberately, for per-tab
multi-user isolation (two different users in two tabs). A domain-wide cookie
under Option A **destroys that property**, and makes the `BroadcastChannel`
sign-out shim redundant.

You cannot have both per-tab multi-user *and* ambient cross-product SSO.
Recommendation: per-tab multi-user is an operator convenience; cross-product SSO
is a customer-facing requirement. Drop per-tab, or keep it behind a `?multiuser=1`
dev flag.

### Anonymous teaser

Reuse `session_auth.py` as-is. Simmer's teaser should show the watchlist UI and
a **redacted** readiness card — score band and structure name visible, exact
strikes and credit locked — mirroring `routes/snapshot.py`'s `_redact_for_teaser`
pattern with `_GATED_FIELDS = ("suggestion", "strikes", "credit")`.

---

## Data model

Split follows the existing convention exactly: **user-scoped data in Supabase
Postgres (RLS), market/time-series data in DuckDB (backend-only).**

### Supabase — `supabase/migrations/0010_simmer.sql`

```sql
-- Simmer: per-user watchlists, alert history, and product settings.
-- RLS: owner-scoped CRUD, mirroring user_settings (0003).
-- APPLY: make db-push  (idempotent)

create table if not exists public.simmer_watchlist (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users(id) on delete cascade,
    symbol      text not null,
    expiration  date,                       -- null = engine auto-picks
    position    int  not null default 0,
    active      boolean not null default true,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    unique (user_id, symbol)
);

create index if not exists simmer_watchlist_user_idx
    on public.simmer_watchlist(user_id) where active;

create table if not exists public.simmer_alerts (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references auth.users(id) on delete cascade,
    symbol       text not null,
    expiration   date not null,
    score        numeric(5,2) not null,
    state        text not null,             -- 'ready' | 'cooling' | 'vetoed'
    structure    text,                      -- 'bull_put' | 'bear_call' | 'iron_condor'
    payload      jsonb not null default '{}'::jsonb,
    acknowledged boolean not null default false,
    created_at   timestamptz not null default now()
);

create index if not exists simmer_alerts_user_created_idx
    on public.simmer_alerts(user_id, created_at desc);

create table if not exists public.simmer_settings (
    user_id            uuid primary key references auth.users(id) on delete cascade,
    min_score          numeric(5,2) not null default 70,
    min_iv_rank        numeric(5,2) not null default 30,
    max_dte            int not null default 45,
    min_dte            int not null default 7,
    notify_email       boolean not null default false,
    risk_profile       text not null default 'balanced',  -- conservative|balanced|aggressive
    structures_enabled text[] not null default '{bull_put,bear_call,iron_condor}',
    gate_overrides     jsonb not null default '{}'::jsonb, -- TOGGLEABLE tier only;
                                                           -- validated server-side
    regime_strictness  text not null default 'balanced',   -- relaxed|balanced|strict
    max_concurrent     int  not null default 5,
    prefs              jsonb not null default '{}'::jsonb,
    updated_at         timestamptz not null default now()
);
```

All three get owner-scoped RLS (`select/insert/update/delete using (auth.uid() =
user_id)`), reusing the `public.set_updated_at()` trigger from `0001`. This
mirrors `user_settings` (0003), which is browser-managed under RLS — so the
Simmer frontend can CRUD the watchlist **directly via Supabase**, no backend
round-trip needed for list management. The backend reads it service-role for the
watcher loop.

### DuckDB — appended to `db.py`'s `_SCHEMA`

Idempotent DDL in the existing house style (`CREATE TABLE IF NOT EXISTS`).

| Table | Purpose |
|---|---|
| `simmer_snapshots` | ts, symbol, expiration, spot, iv_rank, iv_pct, atm_iv, expected_move_1sd, expected_move_2sd, put_wall, call_wall, zero_gamma, skew_25d, term_slope, liquidity_score |
| `simmer_readiness` | ts, symbol, expiration, composite score + every component sub-score + gate results (JSON) + suggested structure/strikes/credit |
| `simmer_iv_history` | session_date, symbol, atm_iv — the rolling series IV Rank is computed from (see below) |
| `simmer_news` | ts, symbol, headline_hash, source, published_at, sentiment, confidence, url — dedup by `headline_hash` |
| `simmer_catalysts` | symbol, event_type, event_date, session (bmo/amc), confirmed, source |
| `simmer_research_cache` | **Tier-1 cache**, PK `symbol`. One row per ticker holding the expiry-independent research block + a `computed_at` per field group, so a different-expiry run reuses it. See [Computation reuse](#computation-reuse--the-two-tier-cache) |
| `simmer_outcomes` | Post-hoc: did the short strike hold through expiration? Feeds calibration. |

`simmer_readiness` is the **tier-2** table and is keyed `(symbol, expiration,
ts)` — a second expiry on the same ticker is a separate record by construction,
while `simmer_research_cache` is keyed on `symbol` alone so both share it.

**None of these DuckDB tables carry a user identifier.** They are market data.
All user-scoped state lives in Supabase under RLS.

**`simmer_iv_history` is the critical bootstrap dependency.** IV Rank is defined
against a trailing window (conventionally 52 weeks). On day one you have zero
history, so IV Rank is **undefined** — and a gate keyed on it would either pass
everything or block everything. Handling is specified in
[Gap analysis](#gap-analysis-and-open-risks).

---

## Backend API surface

All routes carry `dependencies=_GATE` (the `ensure_tool(user, "simmer")` gate),
follow the repo's no-prefix convention (full path in each decorator), and return
plain dicts.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/simmer/config` | Tickers allowed, knob defaults, engine version |
| `GET` | `/simmer/watchlist` | Current user's watchlist + latest readiness per row |
| `POST` | `/simmer/watchlist` | Add `{symbol, expiration?}` — validates the symbol has a tradable chain |
| `DELETE` | `/simmer/watchlist/{symbol}` | Remove |
| `PATCH` | `/simmer/watchlist/{symbol}` | Set expiration / active / position |
| `GET` | `/simmer/analyze/{symbol}` | Full on-demand cook: readiness + all components + suggestion |
| `GET` | `/simmer/expirations/{symbol}` | Selectable expirations with DTE + a recommended flag |
| `GET` | `/simmer/news/{symbol}` | Last-24h scored headlines + velocity + catalyst flags |
| `GET` | `/simmer/alerts` | Alert feed, paginated |
| `POST` | `/simmer/alerts/ack` | Acknowledge |
| `GET` | `/simmer/status` | Watcher-loop health (mirrors `/status`) |
| `POST` | `/simmer/sso/ticket` | Issue SSO hand-off ticket (see above) |
| `POST` | `/simmer/sso/redeem` | Redeem for a fresh session |

`/simmer/analyze/{symbol}` is the workhorse and must be **pure given its
inputs** — the same function the watcher loop calls, so the on-demand and
scheduled paths can never diverge. This mirrors how `engine.compute_engine_output`
serves both the poller and `/snapshot`.

---

## The readiness engine

### Architecture: gates, then rank

Every surveyed platform that publishes anything uses **hard structural gates
followed by a soft ranking layer** — not one big weighted sum. Simmer follows
that, and it also matches EdgeLane's existing engine shape.

```
chain + news + catalysts
        │
        ▼
  ┌───────────┐   any gate trips → VETOED, score suppressed,
  │ HARD GATES│   reason surfaced. No amount of score overrides.
  └─────┬─────┘
        │ all clear
        ▼
  ┌───────────┐
  │ STRUCTURE │   which side(s) are sellable — bull put / bear call / IC
  └─────┬─────┘
        ▼
  ┌───────────┐
  │  STRIKES  │   short strike outside BOTH expected move AND wall
  └─────┬─────┘
        ▼
  ┌───────────┐
  │ COMPOSITE │   0–100 readiness, weighted components
  └───────────┘
```

### Hard gates (veto — no score overrides these)

| Gate | Rule | Rationale |
|---|---|---|
| **Catalyst lockout** | Any confirmed binary event (earnings, hard-block 8-K item, macro release) inside the tenor → veto. Unconfirmed earnings → block the whole week. | Selling premium through an unpriced binary is the fastest way to lose more than the trade could earn |
| **Liquidity floor** | Quoted spread width ≤ **8% of credit** (≈ ≤3% of spread width); OI ≥ several hundred per leg; non-zero size at the touch | Derived, not folklore — see below |
| **DTE window** | Configurable, default 7–45 DTE, **plus** a friction test: expected gross EV in **dollars** ≥ 2 × round-trip cost | DTE alone is the wrong gate — see the 0DTE table |
| **Volatility floor** | **IVP < 40** (or IVR < 25) → veto | Below IVR ~20, forward IV/RV < 1.0 — negative EV before costs |
| **VRP floor** | **IV / RV_YangZhang < 1.15** → veto | A *ratio*, never vol points |
| **Short delta band** | Outside **0.20–0.35** → veto | 5–10Δ shorts are measurably negative-EV |
| **Chain sanity** | Spot within chain range; both legs quoted; no crossed/locked markets; greeks present | Defensive |
| **Macro table validity** | Refuse to score any expiry beyond `macro_calendar.json`'s `valid_through` | Fails visibly instead of silently selling through a CPI print |

> **A high-IVR *filter* is deliberately NOT a gate** — only a low-IVR *floor* is.
> See the empirical finding below.

#### Why the liquidity gate is 8% of credit

Gross EV is ~7% of max risk at 20% relative VRP. Round-trip friction is
approximately the full quoted bid-ask. For a 5-wide spread at ~$3.75 max risk:

| bid-ask width | round-trip cost | net EV |
|---|---|---|
| $0.02 | 0.53% of risk | +6.87% |
| $0.05 | 1.33% | +6.07% |
| $0.10 | 2.66% | +4.74% |
| $0.20 | 5.32% | +2.08% |
| **$0.40** | **10.64%** | **−3.24%** |

**Friction eats the entire edge somewhere between $0.20 and $0.40 of width.** The
gate is derived from that, not chosen by convention.

#### Why DTE alone is the wrong gate

Frictions are fixed in dollars while the dollar edge shrinks with DTE. At 12Δ
short, width 0.4% of spot, $0.10 round-trip, 1 DTE:

| underlying | spot | friction (% risk) | net EV |
|---|---|---|---|
| $50 stock | 50 | 55.7% | **−48.7%** |
| $100 stock | 100 | 27.8% | **−20.8%** |
| $500 ETF | 500 | 5.5% | +1.5% |
| SPX | 5,000 | 0.54% | **+6.5%** |

**0DTE credit spreads cannot clear frictions on a $50–100 single name.** They work
on SPX/NDX because dollar widths are large relative to fixed costs — which is why
0DTE volume concentrates there. Since Simmer targets single names, short-DTE
candidates must pass the dollar-friction test, not merely a DTE bound.

### Volatility richness — and the finding that shaped it

**IV Rank** (ORATS' published formula, and the industry-standard one):

```
IV Rank = (IV_now − IV_min_52w) / (IV_max_52w − IV_min_52w) × 100
IV Percentile = % of trading days in the past year with IV < IV_now
```

⚠️ **Terminology trap:** Market Chameleon's "IV30 % Rank" is actually a
*percentile* despite the name. Ours follows the ORATS/tastytrade convention
above. Label both explicitly in the UI and the API to avoid ambiguity.

**The empirical result that matters:** Option Alpha's own published backtest (DIA
put credit spreads, ~50 DTE, 40-delta short, 50% profit target) found that
filtering to **IVR ≥ 50 collected 28% more premium per trade — and lost 71%
overall**, versus −4% with no IV filter, with a 93% early drawdown. High IV pays
more per trade because it is *correctly* pricing more risk.

A third-party study (595 symbols) found the **dual** filter IVR > 50 **AND**
IV Percentile > 50 lifted short-iron-condor win rate to **56.8% from 48.2%**.

Conclusion, and it is a real departure from the requirements doc's "only sell
when IVR is above 40–50%": **a high-IVR *filter* is not justified, but a low-IVR
*floor* is.** Bucketing by IVR against the subsequent 45-day IV/RV ratio:

| IVR bucket | forward IV/RV | % with IV > RV |
|---|---|---|
| **0–20** | **0.95** | 39.6% |
| 20–40 | 1.10 | 67.1% |
| 40–60 | 1.20 | 77.7% |
| 60–80 | 1.28 | 76.4% |
| 80–100 | 1.37 | 89.9% |

**Below IVR ~20 the forward IV/RV is under 1.0 — selling premium there is
negative-EV before costs.** That justifies a floor gate. Above it, treat the
level as a soft weight: a rich-IV name with broken structure should still score
badly, and a moderate-IV name with excellent structure should still be sellable.

#### Gate on percentile, display rank

IVR is a **two-point statistic** — a single vol spike pins the denominator for a
full year, and its mean drifts *down* as the lookback grows because the max only
ratchets up. IVP is an order statistic over all N points and self-centres near 50
at any lookback. This is the documented post-COVID SPY pathology, where IVR read
below 20 for months while IV sat well above its pre-COVID median.

**Compute both; gate on IVP; display IVR** (users recognise it).

Two implementation details that will silently corrupt the series:

- **Use ex-earnings IV for the history.** Otherwise every quarterly earnings
  cycle injects a spurious spike that pins the 52-week max for a year.
- **Interpolate in total variance, not in IV.** Near IV 0.350 @ 23d and far
  0.280 @ 37d gives 0.3150 IV-linear vs 0.3087 variance-linear — a 0.63 vol-point
  bias applied daily. Cboe's own VIX methodology interpolates variance.

#### The realized-vol estimator can invert the engine

Simulated with realistic overnight gaps (35% of variance overnight), true
vol = 0.300:

| estimator | measured | verdict |
|---|---|---|
| Close-to-close | 0.296 | unbiased, noisy |
| Parkinson | 0.234 | **~23% biased LOW** |
| Garman-Klass | 0.231 | **~23% biased LOW** |
| **Yang-Zhang** | **0.292** | unbiased **and** ~5× more efficient |

Parkinson and Garman-Klass only see the intraday range, so they miss overnight
gaps entirely. On a genuinely marginal name at true IV/RV = 1.10, they report
1.41–1.43 and **sail through a "IV/RV > 1.20" gate** — a universal false
positive. **Use Yang-Zhang.** This single choice determines whether the VRP gate
works at all.

#### Cold start — this solves the IV-history bootstrap problem

A **cross-sectional VRP percentile** — rank `IV30 / RV20` across the watchlist
*today* — needs **zero history** and performs on par with 252-day IVR:

| quintile | time-series IVR | cross-sectional VRP pct | 50/50 blend |
|---|---|---|---|
| Q1 | 0.918 | 0.926 | 0.906 |
| Q5 | 1.310 | 1.323 | 1.341 |
| **Q5−Q1 spread** | **0.392** | **0.396** | **0.435** |

The blend beats either alone by ~10%. **Ship the cross-sectional signal on day
one, add the time-series signal as history accumulates, then blend.** This
removes the day-one blocker entirely.

Storage needed for the time-series path: per ticker daily `iv30` ex-earnings
(504d), `iv60`/`iv90` (504d), Yang-Zhang `rv20` (504d), daily OHLC (756d — YZ
needs O/H/L/C), 25Δ put/call/ATM IV (252d), earnings dates, plus a row per
emitted signal with its realized outcome. **Minimum viable: 252 trading days of
`iv30` + OHLC.** Below ~120 days, surface IVR as *provisional* rather than
silently computing it on a short window.

### Skew — steep put skew HURTS a put credit spread

This inverts the common intuition and it is mechanically verifiable on any chain,
so it belongs in the engine explicitly.

A put credit spread sells K₁ and buys K₂ < K₁. Under put skew, **K₂ — the
further-OTM leg you BUY — carries the higher IV.** Steepening skew therefore
raises the price of the leg you buy by more than the leg you sell:

| Structure | Skew exposure | Steep put skew is… |
|---|---|---|
| Naked / cash-secured put | short skew | **good** — genuinely more premium |
| **Put credit spread** | net **long** skew at the wing | **bad** — compresses credit/width |
| Call credit spread (index) | index call skew slopes down, so the bought leg is *cheaper* | **good** — enhances credit |

**So do not gate on "high skew = more premium."** Instead, price the same
structure with `IV = IV_ATM` at both strikes and compare to the real quote. That
difference *is* what skew is costing or paying you on this specific structure —
a directly meaningful number, unlike a raw skew reading.

> **⚠️ Measure it by pricing the legs directly, NOT by integrating `N(−d2)`.**
> The identity `C(W) = ∫ N(−d2) dK` holds only at **flat** vol; along a smile the
> total derivative picks up a `vega · dσ/dK` term. Integrating with a per-strike
> vol reports skew as *paying* +13% on a put credit spread — the exact opposite
> of the truth. Direct Black-Scholes leg pricing gives −3.2% and reconciles with
> the quoted mid. Found during implementation.

Measure skew as `RR25 = IV(25Δ put) − IV(25Δ call)` (equity convention, positive
= put bias) and **name the field for the convention** (`rr25_put_minus_call`) —
FX uses the opposite sign, and a silent sign flip inverts every downstream gate.
Use forward moneyness `x = ln(K/F)`, and normalise by `√T` (`x / (σ_ATM·√T)`)
before comparing slopes across DTE — raw log-moneyness slope decays mechanically
with maturity, so a 7-DTE and a 45-DTE slope are not otherwise comparable.

Follow ORATS' *relative* framing rather than absolute levels: percentile-rank the
constant-maturity slope against its own trailing year, and compare against a
peer ETF. "Skew is rich versus its own history" is defensible; "skew is high" is
not.

> Two cautions. **Cboe's SKEW index is mid-redefinition** — a 2025 consultation
> proposes abandoning the moment-based methodology for a 25-delta differential,
> with full history recalculation, so any threshold calibrated to its historical
> 101–147 range will break. And there is credible published work
> (Kozhan–Neuberger–Schneider) arguing the skew premium is **not separable** from
> the variance premium — i.e. selling skew adds no independent edge. Treat skew
> as a regime variable, not a standalone signal.

### Term structure and the volatility risk premium

The VRP is the actual source of edge, and it is well measured on the index —
SPX 1990–2025: **average +3.0 vol points, positive 85% of the time, Sharpe
0.62**, stable across decades.

**Index VRP exceeds single-name VRP**, because index IV embeds a *correlation*
risk premium on top of the variance premium. That is a structural argument for
preferring index spreads — the edge is not incidental. Simmer targets single
names by design, so it should be honest that it is fishing in the weaker pool and
lean harder on the structural gates to compensate.

Backwardation (`VIX/VIX3M > 1.00`) is a **sizing** question, not a binary gate —
it carries both the highest expected return *and* the largest drawdowns:

| VIX/VIX3M | Action |
|---|---|
| < 0.95 | Contango — full size |
| 0.95–1.00 | Transition — reduce size, require wider credit |
| > 1.00 | Backwardation — significant size reduction, materially larger credit/width |
| > 1.10 | Severe stress — flat or hedged only |

**Match horizons in calendar days.** IV30 is 30 *calendar* days; pairing it with
an RV computed over 30 *trading* days (≈42 calendar) is a widespread silent bug.

Cross-check the RV leg: if Yang-Zhang shows edge but zero-mean close-to-close
does not, the "edge" is an overnight-exclusion artifact. Require agreement.

### Expected move and strike placement

Two standard methods; compute both and take the wider (conservative):

```
EM_sigma     = S × σ_atm × sqrt(DTE / 365)     # 1 standard deviation
EM_straddle  = 1.2533 × ATM_straddle_mid       # straddle → 1 SD
```

⚠️ **The common shortcut is wrong, and the error is ~20%.** `straddle / S` is the
**mean absolute deviation**, not one standard deviation. For a Gaussian,
`E|X| = σ·sqrt(2/π) = 0.7979σ`, so:

```
straddle / S  =  0.7979 × (1 SD)          →   1 SD  =  1.2533 × (straddle / S)
```

Most published "implied move" figures — SpotGamma's included — are the **raw ATM
straddle**, i.e. the MAD, which sits ~20% *below* 1 SD. The widely-quoted
`0.85 × straddle` is a convention with no derivation behind it. Using the raw
straddle as if it were a 1-SD band places short strikes systematically too close.

This also corrects a widespread claim: "price stays inside the implied move ~70%
of the time" sounds like edge, but the Gaussian baseline for a **1-SD** band is
**68.3%** — and for a `straddle/S` band only **57.5%**. Frequency statistics
quoted against the wrong null systematically flatter premium selling.

### Probability — three corrections to rules everyone repeats

```
|Δ_put| = N(−d1)          d1 = [ln(S/K) + (r−q+σ²/2)T] / (σ√T)
P(ITM)  = N(−d2)          d2 = d1 − σ√T
```

**1. Delta ≠ P(ITM).** `N(−d2) > N(−d1)` always, and the gap scales with σ√T.

**2. Probability of touch is `≈ 2 × N(−d2)`, NOT `2 × delta`.** The exact
first-passage form for GBM (ν = r − q − σ²/2, b = ln(B/S), s = σ√T):

```
down-barrier:  POT = N((b − νT)/s) + e^(2νb/σ²) · N((b + νT)/s)
```

Measured across IV/DTE/strike, `POT / N(−d2)` lands in **1.81–1.99** (good), but
`POT / |delta|` ranges **2.05–2.90**. **The folk rule understates touch risk by
5–45%, worst exactly where it matters — high-IV names and longer DTE.**

**3. POP must be evaluated at the breakeven, not the short strike.**

```
BE_put_spread = K_short − credit
POP = P(S_T > BE) = N(d2) with K := BE, σ := IV interpolated at BE
```

**Short-strike rule — and a correction to an earlier draft of this spec.**

An earlier version said "place the short strike outside BOTH the 1-SD expected
move AND the structural wall, whichever is further." **That is not simultaneously
satisfiable with the 0.20–0.35 delta band**, because the 1-SD point sits near
16Δ — which the delta gate itself vetoes. It is the *same* contradiction
documented above for "⅓ width at 16 delta", reintroduced two sections later.

The resolution, as implemented:

1. **The delta band is the hard gate and wins.** Candidates are built at the
   delta target (0.25–0.30) and vetoed outside 0.20–0.35.
2. **The expected-move and wall barriers are honoured *within* the band** — if a
   strike inside the band also clears them, take it.
3. **Otherwise keep the delta-target strike and let the expected-move-headroom
   component score it down.** A candidate that can't reach the barrier isn't
   rejected outright; it's just worse, which is what a soft component is for.

Consequence worth knowing: inside a 0.20Δ floor a candidate reaches at most
~0.84 EM, so scoring "full headroom" at 1.5 EM would be unreachable by
construction and silently cap the composite ~12 points below 100. **Full headroom
is therefore 1.0 EM, not 1.5.**

Then verify POP at the breakeven.

### The result that should drive the whole engine

**Under the risk-neutral measure, using the chain's own IV, the exact EV of any
credit spread priced at mid is EXACTLY ZERO.** Verified by numeric payoff
integration across multiple strikes.

Two consequences, and they are not optional:

**a) The naive binary EV formula is structurally biased and must not be used.**
`EV = POP × credit − (1 − POP) × maxloss` returns −0.20, −0.21, −0.13 on spreads
whose true EV is 0.00 — because it treats everything below breakeven as a *full*
max loss, ignoring the partial-loss zone between breakeven and the long strike.
**Integrate payoff × probability in $0.01 increments** (Option Alpha's published
method).

**b) All edge comes from the gap between implied and subsequent realized vol.**
If you price probabilities off the chain's IV, EV is zero by construction. Option
Alpha feed **historical** volatility into their probability engine precisely to
avoid this tautology. Simmer must do the same.

EV as a fraction of max risk, 30Δ short, 5-wide, 45 DTE:

| relative VRP (IV/RV − 1) | 10% | 15% | 20% | 25% |
|---|---|---|---|---|
| **EV / max risk** | ~3.5% | ~5.4% | ~7.4% | ~9.6% |

This is nearly invariant across IV *level* (20%–80% IV all land within ~0.7pp at
constant relative VRP) — **which is why the VRP gate must be a ratio, not vol
points.** An absolute "IV − HV > 5 points" rule passes every high-IV name and
rejects every low-IV one for no economic reason.

**Show the user two POP numbers: market-implied (chain IV) and forecast (our RV
forecast). The gap between them is the edge. If they are equal, there is no
trade.**

### Credit-to-width and short delta are not independent

A derived structural bound that resolves a contradiction in common advice:

> `C(W) = ∫₀^W N(−d2)(K_short − u) du`, so **C/W is the mean of `N(−d2)` over the
> spread's strike interval, and therefore `C/W < N(−d2)(K_short)` strictly**,
> approaching it as W → 0.

**Maximum achievable C/W = P(short strike finishes ITM) = 1 − POP_short.**

| K_short | \|Δ\| | N(−d2) | C/W @ W=1 | W=5 | W=10 |
|---|---|---|---|---|---|
| 97 | 0.366 | 0.407 | 0.388 | 0.316 | 0.238 |
| 95 | 0.295 | 0.332 | 0.314 | 0.248 | 0.181 |
| 90 | 0.146 | 0.172 | 0.159 | 0.115 | 0.077 |

**Therefore "collect ≥ ⅓ of the width" and "sell the 16-delta strike" are
mathematically incompatible.** A 16Δ short caps out at C/W ≈ 0.19 at 30% IV — at
*any* width. Collecting ⅓ requires short delta ≈ 0.33+ **and** a narrow spread.
This reconciles tastylive's own guidance, which pairs "1/3rd the width" with "~67%
probability of success" — those are *the same constraint*, and ⅓ sits exactly at
the feasibility boundary. The retail mashup of "⅓ width + 16 delta" is not
internally consistent.

**Engine implication: credit/width is a dependent variable. Fix short delta,
target C/W, solve for width.**

**And the C/W target must be a fraction of its own ceiling, not an absolute.** At
the optimal 25Δ the bound is ≈0.286, so an absolute 0.30 target is *infeasible* —
and chasing it drives the solver toward 1–2 wide spreads whose package bid/ask is
~8.9% of their own credit, tripping the very liquidity gate the target was meant
to support. As implemented: target `min(0.70 × bound, 0.30)`, and when the
absolute target is unreachable the engine says so in `avoid_if` rather than
silently retargeting.

### Optimal short delta — not where convention puts it

Simulated under stochastic vol at IV/RV = 1.25, managed at 50%-or-21-DTE:

| target Δ | EV/trade (% risk) | annualized | Sharpe |
|---|---|---|---|
| 0.05 | **−0.56%** | −21.9% | −0.36 |
| 0.10 | **−0.22%** | −7.4% | −0.09 |
| 0.16 | +0.32% | 9.3% | 0.09 |
| **0.25** | **+0.92%** | 23.0% | **0.18** |
| **0.30** | **+1.05%** | 24.3% | **0.18** |
| 0.35 | +1.10% | 23.9% | 0.16 |
| 0.45 | +0.46% | 8.9% | 0.05 |

**5–10 delta shorts are negative-EV** — the classic pennies-in-front-of-a-
steamroller result, quantified. **Target 0.25–0.30 delta**, and gate short delta
to the 0.20–0.35 band.

### Structural walls — the strongest external validation available

SpotGamma publishes dated, sample-specified hit rates for SPX (10 May 2019 –
28 May 2024). These are the only published, verifiable statistics found in the
entire survey, and they justify weighting walls heavily:

| Level | Statistic | Rate |
|---|---|---|
| **Call Wall** | sessions where the intraday high did not exceed it | **83%** |
| **Call Wall** | sessions closing below it | **88%** |
| **Put Wall** | sessions where the intraday low did not break it | **89%** |
| **Put Wall** | sessions closing above it | **93%** |
| Volatility Trigger | 5-day realized vol, opens **above** vs **below** | **13%** vs **18%** |
| Volatility Trigger | 1-day return stdev, closes above vs below | **0.9%** vs **1.3%** |

That last pair is a ~1.4× realized-vol regime shift across a single level — a
legitimate basis for a **conditional** weight: the same candidate should score
differently depending on which side of the gamma-flip level spot sits.

EdgeLane already computes bilateral walls with strength tiers (`walls.py`), so
this is reuse, not new math. Note the SPX rates are index statistics; single
names are noisier — treat them as an upper bound on confidence.

### Composite score

```
readiness = 100 × Σ(wᵢ · componentᵢ) × regime_multiplier
```

| Component | Weight | Contents |
|---|---|---|
| **Structural safety** | 0.30 | Short-strike distance beyond wall + wall strength tier + which side of gamma flip |
| **Expected-move headroom** | 0.20 | Short strike vs max(EM_straddle, EM_sigma), in σ units |
| **Volatility richness** | 0.20 | Dual IVR + IVPct condition, IV/HV ratio, term-structure slope |
| **Credit quality** | 0.15 | `Alpha = EV / MaxLoss`, credit/width ratio, POP |
| **Liquidity quality** | 0.10 | Spread **in IV points** (ORATS `mwAdj30` approach), OI depth, size at touch |
| **Sentiment / flow lean** | 0.05 | Side selection only — see the asymmetry rule below |

Weights live in `simmer_config.py` and must be tunable without a code change.

**`Alpha = EV / MaxLoss`** is Option Alpha's published ranking metric and the
only composite any surveyed platform documents. It is risk-normalized — $5 EV on
$50 max loss ranks identically to $25 EV on $250. Compute EV by integrating the
payoff across the full profit/loss range in $0.01 increments rather than using a
three-point approximation.

**Liquidity as a soft multiplier, not only a gate.** ORATS' published
`confidence` field derives a 0–1 data-quality weight from option count and
bid/ask width, and degrades a candidate's score by measured quote quality rather
than vetoing it. Expressing spread **in IV points** rather than dollars is the
one genuinely professional formulation found in the survey — it normalizes across
price levels and DTE. Adopt both.

### Squeeze risk — a call-side veto (v1.1)

The product's core loop is: *a name gets hyped, IV goes rich, we sell it.* But
hype is also when the tail is fattest, and a heavily-shorted name has a
**mechanical amplifier on upside moves**.

```
if days_to_cover >= SQUEEZE_DTC_VETO:      # config, start ~7
    block bear_call and the call side of an iron condor
    bull_put remains eligible
```

Asymmetric on purpose: forced covering only pushes price **up**, so it threatens
the call side alone. See
[Short interest](#short-interest--free-and-the-one-squeeze-guard-deferred-to-v11)
for sourcing and the daily-short-volume caveat.

### Market regime — the put-side guard

**The most dangerous property of this product: a crash is when IV is richest, and
the engine hunts rich IV.** Left alone it would look at a post-bad-CPI tape and
see "IV rank 95, premium fat, expected move wide, strikes comfortably far away"
and sell a bull put spread into a falling knife. Post-shock IV is **compensation
for genuinely elevated risk, not mispricing** — the same sign error as "IV
crushed after earnings, so sell now."

No sentiment input is needed to detect this. A macro shock leaves three *priced*
fingerprints, computed **once per sweep at the index level**, not per ticker:

| Fingerprint | Risk-off when | Why it works |
|---|---|---|
| **Term structure** | `VIX / VIX3M > 1.00` | Front end inverts because the market expects near-term realized vol to exceed longer-term — and it's usually right |
| **Index put skew** | 25Δ put/call skew steepening vs its own trailing distribution | Mechanically compresses put-spread credit: the further-OTM leg you *buy* gets expensive faster than the one you sell |
| **Gamma flip** | Index spot below the volatility trigger | Realized vol ~18% below vs ~13% above; 1-day return stdev 1.3% vs 0.9% |

Acting asymmetrically — the mirror of the short-interest veto:

```
risk_off  → suppress bull_put and the put leg of iron condors
            (selling into continued selling); bear_call stays eligible
stressed  → hard cap on total concurrent open positions
```

> **Index products are excluded from *trading*, not from *data*.** The regime gate
> requires SPY/QQQ/VIX chains every sweep. Do not remove that fetch when
> implementing the index exclusion — they are separate concerns.

**Two honest limits.** These describe *state*, not warning: spot below the gamma
flip means you are already through it. That is fine for the question actually
being asked ("should I open a position right now?") but it is not an early
warning and must not be presented as one. And `VIX/VIX3M > 1.00` is a fairly high
bar — a moderately bad print can drop the index 1.5% without inverting the curve,
which hurts a bull put without tripping the gate. Treat the bands as **sizing**
first and a veto only at the extreme, per the term-structure section.

### Sector dislocation — the cross-sectional check

The regime gate is index-level, and **we trade single names**. A sector shock —
an export restriction hitting AI memory, say — can tank SNDK and MU while
VIX/VIX3M barely moves and the index gamma flip is untouched. None of the three
fingerprints sees it.

That is the genuinely dangerous case, because on the name itself the shock looks
like *opportunity*: IV spikes, so IV/RV widens, so the VRP gate opens further. No
filing, so no catalyst veto. No index stress, so no regime veto. **Rich premium,
no red flags.** Only the per-ticker news-velocity detector guards it, and that is
a soft suppressor rather than a hard gate.

The fix falls out of the union-of-watchlists architecture for almost nothing —
every watched name is already computed in one pass:

```
if (names_with_IV_spike / names_watched) > DISPERSION_THRESHOLD
   and index_regime == calm:
       → sector dislocation; suppress the affected cluster
```

Cluster membership can start as a simple sector tag and improve later with the
pairwise correlation matrix (computable from the OHLC already stored for
Yang-Zhang). This is the same machinery as the concurrency flag: **many names
going "ready" together is one bet, not many**, and many names spiking IV together
without index stress is one shock, not many.

### Sentiment's constrained role

Per the sourcing section: sentiment **selects the side and can veto — it never
promotes.** Concretely:

- A ticker never becomes "ready" *because* sentiment is positive.
- Fresh negative sentiment **blocks bull put spreads** on that name (persistent
  downward drift, half-life ~7 weeks).
- Fresh positive sentiment does **not** symmetrically block bear calls, because
  positive sentiment decays with a ~1-week half-life and is gone by week three.
- A velocity burst above `ALERT_P` **suppresses the score** regardless of
  direction — unpriced uncertainty is the enemy of a premium seller.

This asymmetry is not arbitrary; it follows directly from Heston & Sinha's
positive/negative persistence split.

### Earnings and IV crush — why Simmer avoids rather than harvests

The requirements mention usefulness "pre/post earnings". The research points
firmly the other way, and this is worth stating because it is counter-intuitive.

**The crush is real, large, and largely knowable in advance.** IV falls after
earnings in **97.3%** of cases (ORATS, n=1,978) and the median removable
"earnings hump" in 30-day IV has stayed in a tight **18.4%–20.6%** band year over
year. It follows a clean `1/τ` law:

```
IV(T)² = σ² + σ_e²/(T − t)        # σ_e = one-shot jump SD, NOT annualized
crush  = 1 − sqrt(1 − σ_e²/(IV²·τ))
```

With σ = 30%, σ_e = 5%: **−70%** at 1 DTE, −36% at 7, −13.5% at 30, −4.6% at 90.
So **60–70% of the crush lives in the first expiration and it is exhausted by
~90 DTE.** Compute it per-expiration from σ_e; never apply a flat percentage.

**But predictability is not edge, because it is priced.** Three findings kill the
naive "sell into earnings" case:

1. **The gross edge is 2–5% of straddle value** (ORATS: long straddles across the
   event returned −2% to −5% over 12 quarters). Single-name earnings straddles
   routinely carry 1–3% bid/ask *and you cross it twice*. **The measured edge is
   at or inside round-trip transaction cost.** Any model assuming mid-price fills
   will show an edge that does not survive execution.
2. **It may be a model artifact.** Leung & Santoli show that under Black-Scholes
   the implied earnings move looks 15–25% rich, but under a **stochastic-vol**
   model the jump risk premium essentially vanishes (σ_e^P/σ_e^Q ≈ 0.99–1.11
   across five IBM quarters). The apparent richness is the 2-point estimator
   misattributing ordinary term-structure backwardation to the announcement.
3. **The payoff shape is hostile.** ~55% win rate, positive mean, but average
   loser exceeds average winner and single events reach −187%. A negative-skew
   carry trade into a binary is exactly what a "consistent income" product should
   not be doing.

**Therefore: earnings stays a hard gate, not an opportunity.** Simmer blocks the
tenor rather than trying to harvest the crush.

**Two corollaries that do matter:**

- **"IV crushed, so sell now" has the sign backwards.** Post-crush IV *is* the
  ex-earnings floor — you'd be selling ordinary vol at ordinary levels with no
  event edge, and realized vol is depressed too, so the ratio is unchanged.
- **The clean window is roughly T+5 to T+45** for a quarterly reporter. Before
  T+5 the crush is still completing (~28% arrives over T+1 to T+5); after ~T+45
  the *next* print starts inflating the term structure, and essentially the
  entire rebuild happens inside the last two weeks. Simmer should prefer this
  window and say why.

### Early-assignment risk

Relevant because Simmer sells spreads on dividend-paying equities. Early exercise
of an American call is only ever rational immediately before an ex-dividend date,
and the exact condition is:

```
D > P + K·(1 − e^(−r·τ))        # exact
D > P + K·r·τ                   # linearized
```

i.e. **assume assignment when the short call's extrinsic value drops below the
dividend.** Note the rate term cuts *against* exercise — higher rates make early
assignment less likely for a given dividend.

The consequence people forget: being assigned on a short call leaves you **short
the stock**, and if you are short through the ex-date **you owe the dividend** —
on top of the forfeited extrinsic. Simmer should flag any candidate whose short
call goes ex-dividend inside the tenor, and surface the extrinsic-vs-dividend
comparison rather than just a delta.

### Position management rules (surfaced with every suggestion)

Standard premium-selling practice, shown in the UI so the user knows the exit
before entering:

- **Close at 50% of max profit** — the canonical profit target.
- **Manage at 21 DTE** regardless of P&L — gamma risk accelerates sharply
  inside three weeks.
- **Stop at 2× credit received.**

Simmer does not place or manage orders (that's Torque), but it records these
targets in the suggestion payload so the hand-off carries them.

### Honest limitation

Nobody publishes a multi-factor weight vector — Option Alpha's `EV/MaxLoss` is
the only documented composite in the industry, and it is deliberately simple.
Building a weighted composite puts Simmer ahead of every published platform, but
it also means **there is no external benchmark to validate against.** The weights
above are reasoned starting points, not fitted parameters. The
`simmer_outcomes` table and its calibration loop are what turn them into
something defensible — treat the initial weights as a hypothesis to be measured,
and say so in the UI.

---

## Data sourcing

The governing principle, which inverts the priority implied by the requirements
doc: **the free signals are the ones with peer-reviewed support at our horizon;
the expensive ones have marketing support and no published evidence.** Build the
free stack first, prove a signal works, and only then pay for fidelity.

### Market data — provider switch (decided 2026-08-15)

Matrix and Torque already lean heavily on Tradier (120 req/min market data), and
Simmer's sweep should not become a chokepoint on that shared limit. Market data
therefore sits behind a **provider abstraction** with a config switch:

```
SIMMER_DATA_PROVIDER = tradier | yahoo      # edgelane_market.config, default tradier
```

**Honest sizing first:** at 20 tickers the sweep costs ~2–3 requests per symbol
per 5 minutes ≈ **12 req/min against the 120/min limit** — about 10%. The
chokepoint becomes real at 50–100 tickers, multi-expiry, or faster cadence. The
switch is future-proofing, not an emergency.

**Webull was evaluated and abandoned** (2026-08-15). Verified against the
installed SDK: its REST market data has **no greeks, no IV, and no open
interest** (`open_interest` exists only in the streaming protobuf). GEX walls
need OI and everything needs IV, so pure-Webull cannot drive the engine, and the
Tradier-OI hybrid wasn't worth its complexity. The SDK stays as a dependency for
per-user *brokerage* (Torque orders) only.

**Yahoo is the alternate provider.** Its unofficial options endpoint
(`query1.finance.yahoo.com/v7/finance/options/{symbol}`) returns, per contract:
strike, bid/ask/last, volume, **`openInterest`**, and **`impliedVolatility`** —
everything the engine needs, with greeks computed analytically from the supplied
IV. The v8 chart endpoint provides **daily OHLC**, which Tradier's client lacks —
upgrading Yang-Zhang to real high/low and letting outcomes fill `touched`. The
repo already talks to Yahoo (Torque's live spot).

Caveats, by design not afterthought:

- **ToS: unofficial, personal-use.** Fine for a personal deployment; must be
  revisited before Simmer is sold. Same posture as the Nasdaq earnings
  cross-check, and part of why `tradier` stays the default.
- **Auth churn**: cookie+crumb handshake, aggressive 429s. Graceful degradation
  only — a persistent 401/429 skips the symbol with a `data_quality` note, never
  crashes the loop, never retry-storms.
- **Stale IV on illiquid strikes**: clamp/reject IV outside [0.01, 5.0], reject
  bid=ask=0 contracts, stamp `data_quality` when >20% of a chain is rejected.

### Market data — Tradier (already owned)

Everything structural comes from the chain we already fetch. Per contract the
chain returns `bid, ask, last, volume, last_volume, bidsize, asksize,
open_interest, trade_date, exchange`, plus (with `greeks=true`) `delta, gamma,
theta, vega, rho, bid_iv, mid_iv, ask_iv, smv_vol`.

> Greeks and IV are supplied **courtesy of ORATS** and are *smoothed surface*
> values, not raw per-contract IV. Fine for wall detection and expected move;
> do not treat them as tick-accurate.

Rate limits: 120 req/min market data (production), 60 (sandbox).

### Options order flow — the significant finding

Tradier's **WebSocket market-events stream** (`wss://ws.tradier.com/v1/markets/events`)
accepts OCC option symbols and its `timesale` event carries **`bid` and `ask` on
every print**, plus `exch`, `size`, `seq`, and a `flag` field:

```json
{"type":"timesale","symbol":"SPY","exch":"Q","bid":"282.08","ask":"282.09",
 "last":"282.09","size":"100","date":"1557758874355","seq":352795,
 "flag":"","cancel":false,"correction":false,"session":"normal"}
```

That is precisely the input required for (a) quote-rule trade classification and
(b) multi-exchange sweep grouping — **included free with a funded brokerage
account**. This removes the need for any paid flow vendor.

Constraints to design around:

- **One streaming session at a time**; sessionid is valid ~5 min before connect.
- **Every OCC symbol must be named explicitly** — no wildcard subscription. Fine
  for a watchlist of near-the-money strikes; structurally rules out market-wide
  unusual-activity scanning.
- **No historical replay.** You record your own tape or you have nothing. *Start
  the recorder before building consumers* — every day of delay is history you
  cannot get back.
- Sandbox is 15-min delayed with no tick data, so this cannot be developed
  against sandbox.

**Required empirical test before relying on it** (~30 lines, must run during
market hours): subscribe to ~20 near-the-money SPY OCC symbols with
`filter:["timesale"]`, dump 5 minutes to JSONL, then check — are `bid`/`ask`
populated? does `exch` vary across venues? is `flag` ever non-empty? does summed
`size` reconcile against the chain endpoint's `volume` delta over the same
window? The last check reveals whether the feed is full OPRA or conflated, which
determines whether true net-premium-flow is buildable or only approximable.

#### Trade classification

Use the **plain quote rule**, not Lee-Ready. Savickas & Wilson (2003, *JFQA*),
using a proprietary CBOE dataset with true trade direction, measured on options:

| Rule | Accuracy |
|---|---|
| **Quote rule** | **83%** |
| Lee-Ready | 80% |
| Ellis-Michaely-O'Hara | 77% |
| Tick test | 59% |

The tick-test tiebreaker actively hurts in options. Dropping the ~15% of
outside-quote trades raises the quote rule above 87% — **drop them rather than
force a side**.

#### Sweep and block detection

Group child prints into a parent order:

```
same OCC symbol
AND all fills on the same side (all at/above ask, or all at/below bid)
AND timestamps within Δt of the first fill
AND ≥ N distinct exchange codes
AND Σ(price × size × 100) ≥ P_min
```

Starting parameters: `Δt = 100 ms` (500 ms loose), `N = 2` minimum / `N ≥ 3`
high-confidence, `P_min = $25k` on liquid names / `$100k` to genuinely cut noise.
Mixed-side child fills → reject the group. **Blocks** (≥500 contracts single
fill) are typically negotiated and print *between* bid and ask, so the quote rule
fails on them — classify them separately rather than forcing a side.

A trade is likely **opening** (not closing) when contract quantity exceeds the
strike's existing open interest — computable from data we already have.

#### ⚠ The spread-leg trap

OPRA trade conditions mark structure legs: `L` = leg of a spread, `M` = leg of a
straddle, `Q` = part of a combo, `P` = option leg of a buy-write; plus complex
codes `f`, `g`, `h`, `i`, `j`.

**A "$2.5M bullish call sweep" is frequently leg `L` of a spread or leg `P` of a
buy-write — one side of a structure, not a directional bet.** If Tradier's `flag`
field carries these codes, filter complex legs out and signal quality jumps
sharply. If it does not, build the detector anyway but treat its output as
low-confidence and **never let it alone flip a bias** — otherwise the engine
systematically reads institutional *hedges* as institutional *conviction*.

#### Free signals available today

Ordered by evidence strength at our 1–5 day horizon:

1. **Volume put/call ratio** — efficient predictor over **~2.5 days**; the OI
   put/call ratio predicts over **~12 days**. That is close to ideal: volume PCR
   for the holding period, OI PCR as a regime filter.
2. **Day-over-day OI delta** — OI is settled overnight by OCC, so it is the
   *authoritative* count of net new contracts per strike with **zero
   classification error**. Vol/OI says whether today's volume was new; ΔOI says
   how much stuck.
3. **Vol/OI ratio** — trivially available.
4. **Poll-sampled quote-rule tape** — each chain poll returns `last`, `bid`,
   `ask`, `last_volume` and `trade_date`; `trade_date` gives dedup so prints are
   never double-counted across polls. Honest limitation: only the most recent
   print per contract per poll is visible, so it undersamples non-uniformly.
   Scale it using the cumulative volume delta — take *direction* from sampled
   prints and *magnitude* from `Δvolume × mid × 100`. An estimator, not a
   measurement; label it as such in the UI.
5. **`bidsize`/`asksize` book imbalance** and `average_volume` normalization.

Note the literature's causal finding: options volume predicts underlying returns
mainly through **embedded leverage, not information** — informed traders use
options for leverage. That effect is captured by plain unsigned volume ratios
computable today; sweep tags are not required to harvest it.

#### Code gaps to close first

- `poller.py` **does** normalize `volume`, but `gex_snapshots` persists only
  `call_oi`/`put_oi` — **no volume columns**. Add them.
- `last_volume` and `trade_date` are **not** normalized. Add them to the
  contract normalizer.
- **IV Rank / IV Percentile is implemented nowhere in the backend** (zero grep
  hits). For a premium-selling engine this is a larger hole than order flow, and
  it makes the requirement "only sell when IVR > 40–50%" currently unenforceable.

### Dark pool — out of scope, deliberately

The requirements ask for "dark pool print concentrations". Recommendation: **do
not build this, and do not pay for it.**

Two different things get sold under one name:

- **FINRA ATS Transparency data** is weekly, *aggregated* share/trade counts per
  ATS, published 2 weeks (Tier 1) to 4 weeks (Tier 2/OTC) in arrears, with no
  prices and no timestamps. By the time it is visible, the week it describes is a
  month gone. **Useless at a 1–5 day horizon.**
- **What vendors actually sell** is consolidated-tape trades attributed to
  FINRA's TRF/ADF (exchange code `D`). That is real, near-real-time data — but
  off-exchange volume is ~45–50% of consolidated US equity volume and the large
  majority is **retail wholesaler internalization** (Citadel, Virtu, Jane
  Street filling retail marketable orders). A "dark pool print concentration"
  map is therefore substantially **a map of where retail traded** — the opposite
  of the smart money it is marketed as.

Beyond that, our instrument is *options on single names* and our edge is *where
price won't go over 1–5 days*. GEX walls already model that from a defensible
forced-flow mechanism (dealer hedging). "Big prints acted as a magnet" is
after-the-fact pattern matching.

If it is ever wanted anyway: use any stock-trades API exposing per-trade
exchange ID, filter for `D`, bucket notional by price level. No "dark pool
product" purchase is warranted.

### Short interest — free, and the one squeeze guard (deferred to v1.1)

The natural contrast with dark pool: same "positioning" theme, but this one is
**free, licensed, and has a defensible mechanism.**

| Source | Gives | Cadence | Cost |
|---|---|---|---|
| FINRA short interest | Shares short outstanding | Semi-monthly, ~2-week lag | **Free** |
| FINRA daily short sale volume | Short vs total volume per ticker | Next-day | **Free** |

```
days_to_cover = short_interest / average_daily_volume
```

**Why the lag doesn't matter here.** This is not a directional forecast — it is a
description of the **tail shape**. Short interest changes slowly, so a two-week
lag that would ruin a momentum signal barely dents a structural one.

**Why it matters to Simmer specifically.** A bear call spread is **short upside
convexity**. Shorts who cover *must buy* — forced buying stacked on top of any
rally — and days-to-cover measures how trapped they are. Two names at identical
delta, IV and wall distance are **not** the same trade if one has days-to-cover
of 10 and the other 0.5: the first has a mechanically fatter right tail, and that
tail is exactly what breaks the position.

So it enters as a **call-side veto**, never as a promoter — the same asymmetry as
sentiment.

> ⚠️ **Do not use daily short *volume* as a level.** It includes market-maker
> hedging (a MM filling your buy sells short to you, then covers), so the
> baseline routinely sits at 40–50% on liquid names. That is plumbing, not
> bearish positioning — the same misreading trap as dark pool prints. Use short
> **interest** and days-to-cover for the veto; if the daily series is used at
> all, use it as a change-versus-its-own-baseline signal per ticker.

**Priority: deferred to v1.1.** Nothing else depends on it, and it is cheap to
add once the core engine is proven.

### Vendors evaluated and rejected

| Vendor | Verdict |
|---|---|
| FlowAlgo, Cheddar Flow, BlackBoxStocks, Market Chameleon | **No usable programmatic API** — they are dashboards. Eliminated on architecture, not price. |
| Cboe All Access API | **$2,499/mo** (Tier 3), plus separate OPRA SIP subscription and professional designation. Institutional pricing. |
| Databento full OPRA | Technically excellent, but paying to ingest the entire options market to watch a dozen tickers is the wrong shape. |
| Unusual Whales | The only retail brand with a genuine documented REST API, so it wins by default rather than merit. Its sweep flags are *inferences over the same tape we can now record ourselves* — buying convenience, not unobtainable information. |

**Dark horse worth one sales email:** Cboe **Open-Close Volume Summary**
classifies every trade by participant type (customer / pro customer /
broker-dealer / market maker), action (buy/sell) **and position (open/close)**.
That is *ground truth* for the two things every flow vendor only estimates.
Drawbacks: Cboe exchanges only (~35–40% of volume, not consolidated), SFTP or
Snowflake delivery rather than REST, price not public, and the intraday dataset
changes 8 Nov 2026.

**The best-value paid purchase is probably not a flow feed at all — it is
historical options data for backtesting.** After building the free stack we will
have live signals but no way to know whether any of them work, because the
WebSocket has no replay and our recorded tape starts at zero. An unfalsifiable
signal in a live money engine is worse than no signal.

### News, sentiment, and catalysts

#### Recommended stack — free, and the constraint is licensing not rate limits

**Alpaca news (REST + WebSocket) → Gemini for scoring → cache by article id.**
Total cost **$0–2/month**. Webull fundamentals (already a dependency) covers
earnings, filings and price targets.

| Layer | Primary | Fallback |
|---|---|---|
| News (live) | **Alpaca WebSocket** `wss://stream.data.alpaca.markets/v1beta1/news` | Wire RSS firehose + regex ticker match |
| News (24h backfill) | **Alpaca REST** `/v1beta1/news` | Massive free tier (hourly) |
| Sentiment | **Gemini** (structured output) | Massive `insights[].sentiment` as slow cross-check |
| Earnings / filings / price targets | **Webull fundamentals** | SEC EDGAR (free) |
| Macro | Hardcoded FOMC/CPI table | FRED |

**Alpaca is the find.** Benzinga-sourced, **free with a paper account** (email
signup, no funding), REST *and* WebSocket, **200 calls/min** on the free tier,
full article body via `include_content`, history back to 2015, params
`symbols/start/end/sort/limit/exclude_contentless`. It is push-based, so it is as
fast as paying Benzinga directly — Benzinga's own API is sales-gated well above
their $197/mo terminal and buys only server-side filtering and replay, which are
irrelevant at 20 tickers.

**Wire RSS is the free fallback and the fastest source that exists** — it *is* the
origin, ahead of every aggregator. `https://www.prnewswire.com/rss/news-releases-list.rss`
is verified working (RSS 2.0, ~26 items/3.5h), with tickers inline in the text
(`(NASDAQ: WIX)`) requiring regex extraction rather than a structured symbol
field. It covers exactly the binary-catalyst category.

#### Providers ruled out — and why it's licensing, not limits

| Provider | Verdict |
|---|---|
| **Finnhub `/company-news`** | Technically ideal (free, 60/min, `from`/`to`, full fields, 1yr history) but **legally disqualifying**: ToS restricts plans to *personal use*, bars business use "even internally without written approval", and bars redistributing "data **or derived results from the data**" — which covers publishing even our own sentiment score |
| **Alpha Vantage `NEWS_SENTIMENT`** | Fails twice: **25 req/day = one snapshot per ticker per day**, not 5-minute polling (that needs ~5,760/day → $49.99/mo floor); and ToS grants only personal, non-commercial use. Its score bands also compress to ~±0.4 — weak dynamic range |
| **TradingView** | **Hard no.** ToS: *"we do not permit commercial usage of any of our services or APIs"*, plus a non-display ban covering algorithmic decision-making and a redistribution ban, with named audit/termination rights. Every "TradingView news API" is a scraper on undocumented internals |
| Massive (ex-Polygon) | Free tier includes news + pre-computed sentiment, but **updated hourly** — useless for velocity |
| NewsAPI.org / NewsData.io | 24h and 12h delays respectively |
| Marketaux free | 3 articles per request |
| Tiingo $30/$50 | "Internal Use Only", and no sentiment field |
| yfinance / `api.nasdaq.com` | Unofficial/undocumented, personal-use-only, 429s under load — prototype only |

> **The architectural rule that keeps this clean:** store and display **only our
> own derived outputs** (score, velocity flag, catalyst boolean) plus headline
> text with a clickthrough to the publisher URL. **Do not re-host article
> bodies.** If Simmer becomes a sold multi-user product displaying headlines,
> confirm terms with Alpaca or move to their Broker API.

#### Webull news does not exist — but its fundamentals do

Verified three ways (API reference has only Market Data / Trading / Connect /
Broker; all 59 SDK request classes, zero match "news"; changelog never lists
one). **However**, Webull's 2026-06-27 Fundamentals release covers much of the
catalyst layer, already per-user authenticated in our codebase:

| Request class | Covers |
|---|---|
| `get_earnings_calendar_request.py` | Earnings dates |
| `get_sec_filings_request.py` | Filings |
| `get_analyst_rating_request.py`, `get_analyst_target_price_request.py` | **The "latest price targets" requirement** |
| `get_corp_action_request.py`, `get_dividend_calendar_request.py` | Ex-dividend dates → the early-assignment check |
| `get_capital_flow_request.py`, `get_noii_*` | Order-flow / imbalance inputs |

⚠️ Response shapes are **unverified** (docs 404 without auth) — in particular
whether earnings rows carry confirmed-vs-estimated and BMO/AMC. Verify
empirically before trusting it as the primary catalyst source; keep SEC EDGAR as
ground truth either way.

⚠️ **Also check the SDK pin.** `webull-inc/openapi-python-sdk` was
**archived 2026-06-22**; the live repo is `webull-inc/webull-openapi-python-sdk`.
Confirm which our `webull-openapi-python-sdk>=2.0` dependency resolves to.

#### FinBERT — excluded, and the obvious workaround does not work

`ProsusAI/finbert` has **no license field at all** (confirmed via the HF API).
The GitHub repo's Apache-2.0 covers the *code*; nothing addresses the weights.
The fine-tuning corpus, Financial PhraseBank, is **CC BY-NC-SA 3.0** and
commercial use requires contacting the authors. Whether the NC restriction is
transitive to fine-tuned weights is **genuinely unsettled — no controlling case
law either way.** That is a risk to accept, not a question with an answer.

**The `distilroberta` swap does not fix it.** `mrm8488/distilroberta-finetuned-
financial-news-sentiment-analysis` declares apache-2.0 but was fine-tuned on *the
same* Financial PhraseBank. An uploader's tag does not cure upstream provenance;
it only changes who is making the claim. Its reported 0.98 accuracy is on its own
split of a 4,840-sentence set — not comparable to real headline accuracy.

**Verdict: do not put PhraseBank-derived weights in a sold product.** Use Gemini,
which has clean commercial terms and makes the question disappear. FinBERT also
scores *sentences, not entities* — it cannot tell that "JPMorgan raises price
target on Goldman Sachs" is positive for GS and neutral for JPM.

#### Gemini configuration — the settings matter more than the model

- **Model:** `gemini-3.5-flash-lite`, `thinking_level: "minimal"`. Google's own
  docs say to use minimal thinking for classification. `gemini-2.5-flash-lite`
  is the alternative if thinking genuinely **off** is wanted (the only model in
  the lineup with that default) plus ~0.29 s TTFT.
- **Cost trap:** avoid `gemini-3.7-flash` at default `medium` thinking. Thinking
  tokens bill at the **output** rate and you are charged for reasoning you never
  see — that alone is the difference between ~$0.30/mo and ~$8/mo, and 3.7's
  promotional pricing **doubles on 1 Jan 2027**.
- **Structured output:** number schemas support `minimum`/`maximum`, so pin the
  score to `{"type":"number","minimum":-1.0,"maximum":1.0}` with a string `enum`
  reason code. Google explicitly warns about "schema-compliant but semantically
  incorrect outputs" — **clamp and validate server-side anyway**.
- **Determinism is best-effort, not contractual.** `seed` and `temperature: 0`
  exist but Google documents them as best-effort, with reproduced counter-examples.
  **Cache scores by article id / headline hash** — the only real reproducibility
  guarantee, it makes backtests stable, and it collapses API volume since the same
  wire story appears across many sources.
- **⚠️ Score only NEW headlines.** With 288 polls/day, re-scoring the whole 24h
  window every poll costs **~100× more for identical output**. The cache is a
  correctness *and* cost mechanism.

**Measured cost at 20 tickers, 5-min polling, ~500 new headlines/day** (batched
25 per call → ~20 calls/day):

| Model | Monthly |
|---|---|
| `gemini-2.5-flash-lite` ($0.10/$0.40 per M) | **~$0.22** |
| `gemini-3.7-flash` (promo $0.75/$3.75 through 31 Dec 2026) | ~$1.92 |
- Note the API is migrating to the **Interactions API**; `generateContent` still
  works but the two have different parameter shapes for structured output and
  thinking. Pick one and don't mix.

Log the provider score alongside our own from day one. There is no ground truth
here and nobody has one — after ~4–6 weeks, hand-label a few hundred headlines
where the sources disagree and that becomes a Simmer-specific validation set
worth more than any published benchmark.

#### Deduplication is not hygiene — it is where the signal lives

Boudoukh, Feldman, Kogan & Richardson (NBER w18725) found that on **identified,
firm-specific** news days return variance is **120% above** no-news days, while
on *unidentified* news days it is only **20% above**. Raw headline volume buys a
20% variance bump; correctly classified, deduped, firm-specific news buys 120%.

One press release becomes a dozen wire copies within 90 seconds. Undeduped, the
velocity detector measures **syndication breadth, not novelty**.

Recommended approach: normalize the headline (lowercase, strip punctuation,
ticker tags, source suffix), take token 3-shingles, and collapse into a cluster
at **Jaccard ≥ 0.70 within a 6-hour window**. Count **clusters**, not articles —
but keep cluster size as a separate `breadth` feature. A 6-cluster burst where
each cluster has size 1 (six outlets writing distinct stories) is a far stronger
volatility signal than six syndicated one-offs.

#### News velocity — the algorithm

The naive approach (z-score on headline counts per hour) fails three ways:
overdispersion from syndication clustering makes thresholds far too tight;
intraday seasonality makes it fire every morning at 08:30 ET; and at a base rate
of ~2 articles/day a 5-minute bucket has μ ≈ 0.007, where *any* nonzero count is
a >10σ "event".

**Statistic: negative-binomial upper-tail p-value on deduped cluster counts over
a 60-minute rolling window, against a seasonally-matched, empirical-Bayes-shrunk
baseline.**

| Constant | Value | Why |
|---|---|---|
| `WINDOW` | 60 min (12 buckets), rolling every 5 min | Gets expected counts into a range where a count test is meaningful |
| `LOOKBACK_DAYS` | 20 trading days | Baseline depth |
| `GUARD_BUCKETS` | 6 (30 min) | EARS-C2-style guard band, so a burst that began 20 min ago cannot inflate its own baseline |
| `DEDUP_JACCARD` | 0.70, 6h window | See above |
| `MIN_CLUSTERS` | 3 | **Hard gate** — stops the one-article-is-infinity-sigma failure |
| `ALERT_P` | 0.001 | ≈1 false alert per ticker per 13 days; ~4/day across a 50-ticker list |
| `WARN_P` | 0.01 | Softer "elevated" tier |

Three design choices carry the weight:

- **Seasonally-matched baseline, not a trailing mean.** Baseline for the current
  window = counts in the *same bucket-of-day* on the previous 20 trading days.
  This deseasonalizes by construction — no STL, no harmonics — and is immune to
  the 08:30 spike. Pool the intraday shape across all tickers (it is a
  market-wide property, not a ticker property).
- **Negative binomial, not Poisson.** Poisson forces Var = Mean; news violates
  this structurally because one event generates a *correlated cluster* of
  articles. Expect dispersion φ ≈ 3–10 on 5-min buckets — **measure it on our own
  feed before trusting any threshold.** Using Gamma-Poisson (empirical Bayes)
  gives the NB predictive distribution for free.
- **Empirical-Bayes shrinkage across tickers.** Fit a Gamma prior to the
  cross-sectional distribution of per-ticker rates; sparse tickers get pulled
  toward the pooled rate, dense ones barely move. This also removes the
  cold-start problem entirely — a brand-new ticker starts at the pooled rate.

Report an **exact NB tail probability, not a z-score**: a p-value is directly
comparable between a 2-article/day small-cap and a 50-article/day mega-cap; a
z-score is not. Apply hysteresis — a burst stays "on" until p > 0.05 for 3
consecutive polls.

*Kleinberg's burst automaton is the canonical method but is a batch/offline
optimizer; use it for backtest labelling, not the 5-minute loop. Twitter's
S-H-ESD is unusable here because MAD is frequently exactly 0 on integer counts of
0–3.*

#### Sentiment persistence — asymmetric, and this is the key finding

Heston & Sinha (2017, *Financial Analysts Journal* 73(3); Fed working paper
FEDS 2016-048), >900,000 stories, found:

- **Daily news predicts returns for only 1–2 days. Weekly-aggregated news
  predicts for one quarter** — up to 13 weeks, even for stocks with a single news
  event per week. *Aggregation is what converts a 2-day signal into a 13-week
  one.*
- **Positive sentiment decays fast:** coefficient +0.0304 (t=27.6) at week 0,
  +0.0017 at week 1, +0.0008 at week 2, ~0 by week 3. Half-life ≈ 1 week.
- **Negative sentiment persists:** −0.0329 at week 0 and still statistically
  significant at **week 13**. Half-life on the order of 6–10 weeks. "Bad news
  travels slowly."
- Week-0 long-short return is 3.4–4.2%, then collapses ~95% to a small, nearly
  *constant* drift — not exponential decay, a large jump plus a flat tail.
- **The delayed response is realized at the next earnings announcement**, not
  smoothly: the pre-earnings 13-week cumulative abnormal return is only 0.25%.

Tetlock (2007, *JF* 62(3)) shows the *index-level* picture is different again:
1-sd pessimism → −8.1 bp next day, reversed +6.8 bp over days 2–5, with the
5-lag sum statistically indistinguishable from zero. **At index level, sentiment
is a ~1-day impulse with near-complete reversal inside a week.**

Implementation:

```python
HALFLIFE_POS = 5    # trading days (~1 week; dead by week 3)
HALFLIFE_NEG = 35   # trading days (~7 weeks; significant at week 13)

def persistence(s: float, h: float) -> float:
    """Will this sentiment still hold at horizon h (trading days)?"""
    hl = HALFLIFE_NEG if s < 0 else HALFLIFE_POS
    w = 0.5 ** (h / hl)
    if h < 1:
        w *= 0.25   # day-0 move is mostly contemporaneous impact + reversal,
                    # not predictive — discount hard
    return w
```

Feed it **sentiment averaged over a trailing 5 trading days**, not a single
day — this is the single most evidence-backed design choice available.

Three mandatory caveats:

1. **Gate `HALFLIFE_NEG` on earnings.** Because negative-sentiment persistence
   is realized *at* the next earnings announcement, if the spread expires before
   that date the long half-life is largely unrealizable — fall back to the short
   half-life. Treat this as required, not optional.
2. **Do not apply this to index underlyings.** For SPX/DJX use a ~1-day
   half-life with an explicit sign flip on days 2–5.
3. **Do not apply this intraday.** Sub-daily the empirical shape is
   impulse-then-reversal; a monotone decay would hold a signal that has already
   inverted.

The 5- and 35-day half-lives are fits to published coefficient decay, not
numbers any paper states — the coefficients are real, the exponential
parameterization is an approximation.

#### The asymmetry's consequence for Simmer

Positive news: the volatility we sell into resolves fast, and the directional
signal is gone within about three weeks. Negative news: the vol resolves fast
*but the directional tail risk does not*. That argues for **treating bull put
spreads and bear call spreads asymmetrically after bad news** — a put spread sold
under fresh negative sentiment carries persistent drift against it that a call
spread sold under fresh positive sentiment does not.

#### Unclosed evidence gap

No study was found directly validating that abnormal news *volume* predicts
*implied* volatility expansion — which is precisely the question a premium seller
cares about. **Treat it as untested and measure it on our own data** rather than
assuming it. This is the most valuable gap to close before weighting velocity
heavily in the composite.

### Catalyst detection — the hard gate

This is the input that **vetoes**, and the one the product's safety rests on.
Selling a credit spread through an unpriced binary event is the fastest way to
lose more than the position was ever going to earn.

#### SEC EDGAR — free, official, and the highest-leverage piece here

The `getcurrent` Atom feed spells out 8-K **Item numbers in plain English in the
`<summary>`**, so no document parsing is needed:

```
GET https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom&count=40
```

```xml
<summary type="html"><b>Filed:</b> 2026-08-14 <b>AccNo:</b> 0001104659-26-097364
<br>Item 2.02: Results of Operations and Financial Condition
<br>Item 9.01: Financial Statements and Exhibits</summary>
```

`data.sec.gov/submissions/CIK##########.json` is documented by the SEC as having
**sub-second processing delay** and exposes a columnar `filings.recent` with an
`items` array. Full-text search (`efts.sec.gov`) lags by hours — backfill only,
never live alerting.

**⚠️ Key on `acceptanceDateTime`, not `filingDate`.** After-hours 8-Ks carry the
*next* business day's date. The 17:25–17:30 ET cluster is exactly the earnings
8-K wave that gaps the underlying at tomorrow's open — keying on `filingDate`
misses precisely the filings that matter most.

> **⚠️⚠️ And `acceptanceDateTime` lies about its timezone.** `data.sec.gov`
> appends a `Z` to the value, but the timestamp is **Eastern, not UTC**. Parsing
> it as UTC shifts a 17:32 ET earnings 8-K to 13:32 ET — reclassifying an
> after-hours gap-maker as a harmless mid-session filing, which is precisely the
> failure this gate exists to prevent. Strip the `Z` and tag ET; honour a real
> numeric offset when one is present (the Atom feed does send one). Found by a
> failing test during implementation, not by reading the docs.

> **Implementation note:** `Database.connect()` runs migrations while holding the
> non-reentrant `_lock`, so any persistence helper must resolve the connection
> *before* taking that lock or it self-deadlocks.

Hard-block list (block outright, don't merely down-weight):

| Item / form | Meaning | Why |
|---|---|---|
| **4.02** | Non-reliance on prior financials (restatement) | Extreme; −20%+ gaps |
| **1.03** | Bankruptcy or receivership | Extreme |
| **3.01** | Delisting notice / listing-rule failure | Extreme |
| **4.01** | Change of certifying accountant | Auditor resignation |
| **NT 10-K / NT 10-Q** | Late-filing notification | **Severely underrated** — often precedes a 4.02 restatement or 3.01 delisting |
| **424B5 / 424B4** | Priced offering / shelf takedown | Files after hours, gaps small caps overnight |

Elevated-risk (down-weight, not necessarily block): `1.01`/`1.02` (material
agreement), `2.01` (completed acquisition), `2.05`/`2.06` (impairment), `5.02`
(officer departure), `7.01` (Reg FD — often guidance), `8.01` (catch-all, where
biotech readouts and legal outcomes land), `SC 13D` (activist stake; `SC 13G` is
passive and mostly noise), `SC TO-T`/`SC 14D9` (tender offer), `PREM14A`/
`DEFM14A` (merger proxy).

**Item 2.02 is ground truth that earnings actually happened** — use it to repair
vendor calendar dates. If a 2.02 lands for a ticker whose calendar didn't predict
it, that vendor just lost a reliability point for that name.

Compliance: ≤10 req/s, declare `User-Agent: EdgeLane <email>` and
`Accept-Encoding: gzip`. **`data.sec.gov` does not support CORS**, so this must
live in the backend. Map CIK→ticker via the cached
`https://www.sec.gov/files/company_tickers.json` (10,391 entries; `cik_str` is an
int and must be zero-padded to 10 digits).

#### Earnings dates

The number to internalize: **~35–45% of forward earnings dates in any calendar
are model estimates, not company-announced facts.** Wall Street Horizon has
published seasons where only 55% and 62% of their universe had *confirmed*. Cheap
vendors do not tell you which is which.

| Source | Confirmed flag? | Time of day? | Verdict |
|---|---|---|---|
| **Benzinga via Massive** (`/benzinga/v1/earnings`) | ✅ `date_status` = `confirmed`\|`projected` | ✅ exact HH:MM:SS EST | **Best value.** $99/mo partner add-on |
| Nasdaq `api.nasdaq.com/api/calendar/earnings` | ❌ | ✅ `time-pre-market`/`time-after-hours`/`time-not-supplied` | **Free, unauthenticated**, but undocumented internal endpoint — no SLA, ToS risk. Cross-check, not a dependency |
| FMP `earning-calendar-confirmed` | ✅ | ❌ | Stuck on the **legacy v4 path**, never migrated to `/stable` — deprecation risk |
| Finnhub `/calendar/earnings` | ❌ | ✅ `bmo`/`amc`/`dmh` | Documented history of *not correcting* stale dates. Not alone |
| Alpha Vantage `EARNINGS_CALENDAR` | ❌ | ❌ | 12-month horizon is its one advantage — coarse screening only |
| Wall Street Horizon / TMX | ✅ gold standard | ✅ | Five figures/yr. Out of reach |

> **Tradier is not a catalyst source.** Its `/markets/calendar` is market
> open/close/holidays. The corporate-events endpoint exists only on the old
> portal under `/beta/` and has been in beta since **October 2014** — twelve
> years, and it isn't listed in the current Market Data docs at all.

**Design rule: no cheap source is trustworthy alone.** Cross-check ≥2 sources;
when they disagree on the date, or when the date is unconfirmed, **block the
entire week** rather than trying to be precise. An unconfirmed earnings date is a
*distribution*, not a date.

#### Macro calendar — a hardcoded table is the correct choice

A deliberate decision, not laziness:

- **No free machine-readable source exists.** The Fed publishes **no ICS, no
  JSON, no feed** (verified — the FOMC calendar page contains zero feed links).
  BLS **actively 403s scripted requests** to `bls.gov` under an explicit anti-bot
  policy; `api.bls.gov` works but serves *data, not the release schedule*.
- **Forward visibility is enormous** — as of July 2026 the Fed had already
  published all eight 2027 meetings plus January 2028.
- **The dates essentially never move.**

So: `market/backend/app/macro_calendar.json` with `{date, time_et, event,
severity, confirmed, source}` for FOMC (plus `sep: true` on dot-plot meetings —
the higher-vol ones), CPI, PPI, NFP and PCE, plus **`valid_through`** and
**`confirmed_through`**.

**As built: 113 rows, `valid_through` 2027-12-31, `confirmed_through`
2026-12-23.** Verified against primary sources: all FOMC 2026+2027 with SEP
flags (federalreserve.gov), and CPI/PPI/NFP for all of 2026 (bls.gov 403s
scripted requests, but reads cleanly through a text-extraction proxy) plus
PCE Aug–Dec 2026 (bea.gov).

**55 rows are derived, not verified** — all 2027 CPI/PPI/NFP/PCE, because BLS's
calendar index stops at December 2026 and BEA's 2027 query returns 2026 rows;
neither agency had published 2027 yet. Each derived row records its rule and an
`uncertainty_days`, backtested against 2026 actuals:

| Series | Derivation | Backtest | `uncertainty_days` |
|---|---|---|---|
| CPI | 8th business day | 6/12 exact, all within 2 days | 3 |
| PPI | CPI + 1 business day | — | 3 |
| NFP | First Friday (second when the 1st is a Friday) | all 12 of 2026 correct | 2 |
| PCE | Business day nearest the 27th | 2026 spanned the 23rd–30th | 5 |

**A derived date blocks a *range*, not a day** — unconfirmed rows expand to
`date ± uncertainty_days`. Same principle as unconfirmed earnings: an
unverified date is a distribution.

> Worth knowing when reading the 2026 rows: **NFP Feb 11 is not a Friday**, and
> PPI lands Jan 30 / Feb 27 / Mar 18. Those are 2025-shutdown backlog artifacts,
> verified — not derivation errors. Don't "fix" them.

**Refresh path:** BLS and BEA publish the following year in autumn. Replace the
derived rows and bump `confirmed_through` then — roughly 15 minutes once a year.

**The failure mode is asymmetric, so bound it.** A stale table fails *open* —
you'd hold a spread through a CPI print. Add a startup assertion that
`valid_through` is ≥90 days out, and **refuse to score any candidate whose expiry
falls beyond the table's last date**, logging loudly. One assertion turns a
silent wrong answer into a visible refusal.

FRED (free key) is optional belt-and-braces — but its `releases/dates` endpoint
defaults the realtime window to *today*, which **excludes future dates**. Set
`realtime_end` far out and pass `include_release_dates_with_no_data=true`, and
enumerate `fred/releases` once to record real release IDs rather than guessing.

*Avoid: Trading Economics' `guest:guest` key is discontinued (every tutorial
recommending it is stale); Finnhub's economic calendar is premium/enterprise;
ForexFactory's unofficial JSON works and carries an `impact` rating but is
one-week-forward and unlicensed.*

---

## The watcher loop

`simmer_watcher.py`, started from the existing `lifespan` in `main.py` as a
third task alongside `poll_task` and `eval_task`:

```python
simmer_task = asyncio.create_task(simmer_loop(client, db, runtime_settings),
                                  name="edgelane.simmer_loop")
app.state.simmer_task = simmer_task
```

Shape follows `poller.py` exactly:

- A module-level `state = SimmerState()` singleton holding `latest_by_key`
  (keyed `symbol|expiration`), `last_sweep_at`, `last_error`, `is_running`,
  `watched_count`. Routes import it directly.
- `try: while True:` with inner `except asyncio.CancelledError: raise` and
  `except Exception: log.exception(...); await asyncio.sleep(10)`.
- Per-symbol failures are swallowed and recorded — one bad ticker never kills
  the sweep.
- All DuckDB writes via `await asyncio.to_thread(...)`.

### Cadence and cost control

The requirement is a 5-minute sweep. Not every input needs that cadence, and
some are expensive:

| Input | Cadence | Rationale |
|---|---|---|
| Quote + chain (Tradier) | 5 min | Cheap, drives everything structural |
| GEX/DEX walls, expected move, skew | 5 min | Pure math over the chain already fetched |
| IV rank | Once daily (at close) | It's a *rank over daily history*; intraday recompute is meaningless |
| News fetch | 15 min | Headlines don't arrive faster than this in practice |
| Gemini sentiment scoring | On new headlines only | Deduped by `headline_hash` — never re-score a seen headline |
| Catalyst calendar | Once daily + on watchlist add | Earnings dates move rarely |

Sweeps run only during market hours plus a pre-market window, reusing
`poller._is_market_open()`. Outside those, the loop idles like the existing
poller does.

**Union-of-watchlists model:** the loop fetches the distinct set of
`(symbol, expiration)` across *all* users' active watchlists, computes each once,
and stores one readiness row. Per-user thresholds are applied at **read/alert
time**, not compute time. Twenty users watching NVDA cost one computation.

### Computation reuse — the two-tier cache

Research is expensive (external API calls, LLM scoring, EDGAR polling). Scoring
is cheap (pure math over a chain already in hand). So they cache differently, and
conflating them is the main way this design goes wrong.

| Tier | Key | Holds | Lifetime |
|---|---|---|---|
| **1 — Research** | `symbol` | News + scored sentiment, velocity state, catalyst flags, earnings/ex-div dates, IV rank/percentile, Yang-Zhang RV, flow aggregates (Vol/OI, ΔOI, PCR) | **Per-field TTL, up to 24 h** |
| **2 — Readiness** | `(symbol, expiration)` | Composite score, gate results, suggested structure/strikes/credit, POP, EV, Alpha | **Recomputed every sweep — never cached across sweeps** |

This gives exactly the behaviour wanted: a second user adding the same ticker
**and** the same expiry reads a readiness row computed seconds ago and costs
nothing. A user adding the *same ticker, different expiry* creates a **new tier-2
record** but reuses the entire tier-1 research block — no re-fetching news, no
re-scoring headlines through Gemini, no second EDGAR sweep, no repeat earnings
lookup.

> **Never let a 24-hour TTL reach the score.** The chain moves continuously; a
> cached "ready" would tell a second user to sell a spread whose credit has since
> collapsed. Tier 2 is always fresh math over fresh quotes. Tier 1 is what gets
> amortised.

#### Per-field TTLs

A single 24-hour TTL across the board would be wrong in both directions — too
long for some fields, pointlessly short for others.

| Field | TTL | Why |
|---|---|---|
| Quote / chain / greeks | **Sweep only (5 min)** | Drives credit and fill; the one thing that must never be stale |
| GEX / DEX walls, expected move, skew | Sweep only | Pure math over the chain — cheaper to recompute than to cache |
| Headline sentiment, keyed by article id | **Permanent** | The same wire story never needs re-scoring; this is also the main Gemini cost control |
| Aggregate ticker sentiment + velocity | **15 min** | Matches the news poll cadence |
| Catalyst flags (8-K, earnings, ex-div) | **24 h** — but see invalidation below | Calendar data moves rarely |
| IV rank / IV percentile | **Until next close** | It is a rank over *daily* history; intraday recompute is meaningless |
| Yang-Zhang RV | Until next close | Daily OHLC input |
| Flow aggregates (Vol/OI, ΔOI, PCR) | Sweep for Vol/OI; **daily** for ΔOI | OI settles overnight at OCC |

#### Mandatory invalidation

TTL expiry is not the only trigger. A stale "no catalyst" is precisely the
dangerous value, so tier-1 research is **invalidated immediately, regardless of
age**, when any of these fire:

- A hard-block SEC filing lands for that symbol (8-K item 1.03/3.01/4.01/4.02,
  NT 10-K/Q, 424B4/B5)
- A news-velocity burst crosses `ALERT_P`
- An earnings date for that symbol changes, or a two-source cross-check starts
  disagreeing
- The symbol's chain fails a sanity check (crossed/locked, missing greeks)

#### Why cross-user sharing is safe here

Worth stating explicitly, because everything else in the system is strictly
user-isolated: **tier-1 research and tier-2 readiness are market data, not user
data.** They contain no user identifier and are derived entirely from public
sources. They therefore live in **DuckDB (backend-only)**, while
`simmer_watchlist`, `simmer_alerts` and `simmer_settings` stay in **Supabase
under owner-scoped RLS**. The only user-specific step is applying that user's
thresholds at read time. Nothing about which tickers a given user watches is
inferable from the shared cache.

#### What this is worth

At 20 users with realistic watchlist overlap on liquid names, the external call
volume — Alpaca, Gemini, EDGAR, earnings calendar — drops by roughly an order of
magnitude versus computing per user. It also makes the product's marginal cost
per additional user close to zero for any ticker already being watched, which is
what makes a free tier viable later.

*(Note: dark-pool and block-print work is currently out of scope — see
[Data sourcing](#dark-pool--out-of-scope-deliberately). If it is ever added, it
belongs in tier 1 on a daily TTL, since the underlying FINRA data is published in
arrears anyway.)*

---

## Alerts

State machine per `(user, symbol, expiration)`:

```
cold ──score ≥ user.min_score AND no veto──▶ ready ──score < min_score − hysteresis──▶ cooling ──▶ cold
  ▲                                            │
  └──────────────any hard gate trips───────────┴──▶ vetoed
```

- **Hysteresis is mandatory.** Without it a score oscillating around the
  threshold emits an alert every 5 minutes. Require the score to fall a margin
  below the trigger before re-arming, plus a minimum dwell time in `ready`.
- **Fire once per transition**, not per sweep.
- Delivery: in-app feed always (`simmer_alerts` table, which the UI polls);
  email optional via the existing `emailer.send_email()`.
- Every alert row stores the **full component breakdown** in `payload` so the UI
  can show *why* it fired without recomputing, and so post-hoc calibration can
  ask which components actually predicted outcomes.

---

## User settings and overrides

Not every gate should be adjustable. The governing distinction is **safety gates
vs preference gates** — the first exist to prevent a loss the user won't see
coming, the second encode legitimate taste.

### Three tiers

**🔒 Locked — never user-adjustable**

| Gate | Why it cannot be optional |
|---|---|
| Catalyst lockout | Selling through an unpriced binary is the fastest way to lose more than the trade could earn |
| Liquidity floor | An un-exitable spread is not a position |
| Dollar-friction test | Below it the edge is arithmetically inside costs |
| Chain sanity | Defensive; a crossed/locked market produces nonsense |
| Macro-table validity | Must fail visible, not open |

These are correctness, not preference. Making them optional would also **poison
calibration**, because outcomes across users would no longer be comparable.

**🎚 Tunable within bounds** — exposed as bounded ranges, never free text:

`min_score` · `min_iv_percentile` · DTE window · **short-delta band (hard-clamped
to 0.15–0.40)** · max concurrent alerts · regime-gate strictness (three levels,
not a raw ratio) · alert hysteresis margin.

> The clamp matters. A user who sets short delta to 0.05 has silently opted into
> the measurably negative-EV region. Bounds are how the research reaches the user
> without a lecture.

**🔘 Freely toggleable**

Structures traded (bull put / bear call / iron condor) · notification channels ·
per-ticker overrides · and the **soft** signals: sentiment weighting, squeeze
veto, sector-dispersion check.

### Why this is nearly free

Per-user thresholds apply at **read/alert time, not compute time**. The engine
produces one readiness row per `(symbol, expiration)` regardless of who is
watching; each user's filter runs on read. **Twenty users with twenty different
configurations still cost one computation.** Settings do not multiply the sweep —
they are a view over shared state.

### The hard requirement: calibration is config-independent

**`simmer_outcomes` records the *engine's* verdict, never the user's filtered
view.** If outcomes were keyed to whatever thresholds happened to be set, the
table becomes a mush of configurations and can never answer "does this component
predict anything" — which is the entire point of the loop.

Useful consequence: because every alert already stores the full component
breakdown in `payload`, a user can be shown retroactively what they *would* have
seen at a looser threshold, with no recomputation.

### Schema

Extends `public.simmer_settings` (migration `0010`):

```sql
structures_enabled  text[]  not null default '{bull_put,bear_call,iron_condor}',
gate_overrides      jsonb   not null default '{}'::jsonb,  -- toggleable tier only
regime_strictness   text    not null default 'balanced',   -- relaxed|balanced|strict
max_concurrent      int     not null default 5,
```

Per-ticker overrides live on `simmer_watchlist` rather than here, so a user can
disable the squeeze veto for one name without loosening it globally.

**Server-side enforcement is authoritative.** `gate_overrides` is validated
against the toggleable allow-list on write; a client asking to disable a locked
gate is rejected, not silently ignored.

### UI shape

**Presets first** — Conservative / Balanced / Aggressive (`risk_profile`) — with
an **Advanced** drawer for individual knobs. Most users should never open it.

**Surface active overrides on the card, not buried in settings:**

> ⚠️ *Squeeze veto off — this name has 11 days-to-cover.*

A user who sees a suggestion they don't understand will lose trust in the engine
rather than in their own configuration. Say which guard is down, where they are
looking.

---

## Frontend

**Stack: SvelteKit (Svelte 5 runes) + TypeScript + Tailwind v4 +
`adapter-static` in SPA mode.**

Chosen over SolidJS on solo-dev velocity grounds rather than benchmarks: at a
5-second poll cadence the runtime difference is invisible, while SvelteKit's
maturity, first-party Vercel adapter, first-party Tailwind v4 guide, larger
ecosystem, and substantially larger AI training corpus all compound daily.
Svelte's failure mode under AI assistance is a compiler error; Solid's is a
component that silently renders once and never updates (destructured props).

Guard against the one real Svelte risk — models emitting Svelte 4 idioms — by
pinning `svelte@^5`, running `svelte-check` in CI, and adding to `CLAUDE.md`:

> Simmer is Svelte 5 runes only — `$props()`, `$state`, `$derived`, `$effect`,
> snippets. Never `export let`, `$:`, `<slot>`, or stores unless cross-module.

### Visual parity with Matrix

Matrix's design system lives in a hand-written `<style>` block
(`market/ui/index.html:21–120`), not in Tailwind utilities — `.card`, `.pill`,
`.badge-*`, `.liq-*`, `.composite-good/meh/bad`, `.neon`, `.toast`, `.gate-*`,
ground `#07090d`. Port that block verbatim into `src/app.css` under Tailwind v4's
`@theme` + `@layer components`. That gets ~95% parity by construction. Matrix
uses the Tailwind **v3** Play CDN and Simmer will be on **v4** (OKLCH ramps), so
spot-check `slate-*`/`emerald-*` shades side by side.

### Config injection — keep the runtime pattern

**Do not use `VITE_*` env vars for `api_base`.** They inline at build time, so a
rotated Cloudflare quick-tunnel URL would require a full rebuild — defeating the
entire `app_config.api_base` discovery mechanism. Instead, mirror
`deploy.sh:197–240`: write `build/edgelane.config.js` **after** `npm run build`,
defining the same `window.__EDGELANE_*__` globals, plus
`__EDGELANE_MATRIX_ORIGIN__` for the SSO peer. Simmer then inherits the tunnel
self-heal (`getJSON` re-resolves the pointer and retries once) for free.

Serve that file with `Cache-Control: no-store` via `vercel.json` headers.

> **Pre-existing bug worth fixing regardless of Simmer:** Matrix's
> `dist/edgelane.config.js` carries no cache header today, so Vercel's CDN can
> serve a stale tunnel URL after rotation.

### Component inventory

Reusable from Matrix (port CSS + logic near-verbatim): `AuthGate` (signup/signin
tabs, Turnstile, resend), `AppShell`/`Header`/`StatusPills`, `ProfileMenu`,
`SettingsPanel`, `Toast`, `Tooltip`, `Modal`, `ScoreBadge`/`LiquidityBadge`,
`fmt` helpers, `ExpiryPicker`.

Net-new: `TickerWatchlistManager`, `ReadinessCard`, `SentimentNewsPanel`,
`GexDexWallChart`, `IvRankGauge`, `ExpectedMoveViz`, `AlertFeed`, `ProductGate`
(the "not enabled for Simmer" screen).

Matrix has **no chart library** — every visual is text/DOM. All four
visualisation components are genuinely new; build them as inline SVG and load
the `dataviz` skill before starting them.

`ProductGate` is the one genuinely new gating concept: Matrix gates on session
presence alone, Simmer must additionally check `tools_enabled ∋ 'simmer'`.

---

## Deployment

New Vercel project **`edgelane-simmer`**, deployed from `simmer/ui/`, built with
`adapter-static` (pure static — no serverless cost).

### `deploy-simmer-fe`

`deploy.sh` already has the right seams: `stage_frontend()` (:197),
`deploy_frontend()` (:241), `sync_supabase_site_url()` (:297). Add a
`-s|--simmer` flag with parallel `stage_simmer()` / `deploy_simmer()` functions
rather than a second script, then in the root `Makefile`:

```make
deploy-simmer-fe:
	./deploy.sh -s $(ARGS)
```

`stage_simmer()` runs `npm ci && npm run build` in `simmer/ui/`, then writes
`build/edgelane.config.js` from `deploy/.env` exactly as `stage_frontend()` does.

### Four things that will break if missed

1. **CORS — already satisfied, and this is why the hostname was chosen.**
   `edgelane-simmer.vercel.app` matches the existing regex
   `^https://edgelane[a-z0-9-]*\.vercel\.app$`, so **no config change is needed.**
   ⚠️ This is load-bearing: a hostname that does *not* start with `edgelane`
   (e.g. `simmer-edgelane.vercel.app`) fails the regex and silently breaks every
   API call from the browser. **Do not rename the Vercel project** without also
   widening `CORS_ALLOW_ORIGINS` / `CORS_ALLOW_ORIGIN_REGEX` in
   `edgelane_market.config`.
2. **Supabase redirect allow-list.** `sync_supabase_site_url()` rewrites
   `uri_allow_list` on every deploy from `$VERCEL_PROJECT`. Extend it to include
   the Simmer origin or confirmation/magic-link emails will redirect wrong.
3. **Turnstile allowed-domains.** Add `edgelane-simmer.vercel.app` to the widget
   config, or the teaser dies with error `110200` (hostname not allowed).
4. **Vercel project linking.** `.vercel/project.json` at the repo root is pinned
   to `edgelane-matrix`. Simmer needs its own project link — deploy with
   `VERCEL_PROJECT=edgelane-simmer` from the `simmer/ui/` directory, which keeps
   its own `.vercel/`.

Backend needs **no new deployment surface**: `/simmer/*` routes ship inside the
existing image via `make deploy-be-restart`, and reach the frontend through the
existing Cloudflare tunnel and `app_config.api_base` pointer.

---

## Configuration

Follows the **Torque config precedent**: a Python module of defaults, optionally
deep-merged with a JSON override file — not a second KEY=VALUE file.

`market/backend/app/simmer_config.py`:

- Global engine knobs: component weights, gate thresholds, DTE window, sweep
  interval, news lookback, cache TTLs.
- Per-ticker overrides: min liquidity, wall-strength floor, whether the name is
  eligible at all (some tickers have chains too thin to sell).
- Accessors mirroring `torque_config.py`: `tickers()`, `ticker_rule(symbol)`,
  `weights()`, `gates()`.
- Overrides loaded from `simmer_tickers.json`, discovered via env
  `SIMMER_TICKERS_CONFIG` then by walking `Path(__file__).parents` (so it
  resolves at `/srv/app` in the container, where `parents[3]` doesn't exist).

Secrets and environment-level settings (Gemini API key, any news-provider key)
belong in `edgelane_market.config` alongside the existing keys, surfaced through
`config.py`'s `Settings` model with the usual `_coerce` line per key. **Do not
put API keys in `simmer_tickers.json`** — that file is not gitignored by the
existing patterns.

---

## Testing

Mirrors the existing layout: `market/backend/tests/simmer/`, `asyncio_mode =
"auto"`, package with `__init__.py`.

| Suite | What it covers |
|---|---|
| `test_engine.py` | Scoring math against hand-computed fixtures — IV rank, expected move, POP, skew, composite. Pure functions, no I/O. |
| `test_gates.py` | Every hard gate: catalyst lockout, IV floor, liquidity floor, DTE window. Each must veto in isolation. |
| `test_auth_gate.py` | The 7-case pattern from `tests/torque/test_auth_gate.py`: anon 401, admin token 200, plain JWT 403, entitled JWT 200, auth-disabled no-op. |
| `test_watcher.py` | Loop resilience — one bad symbol doesn't kill the sweep; cancellation propagates; union-of-watchlists dedup. |
| `test_alerts.py` | Hysteresis: a score oscillating around the threshold fires exactly once. |
| `test_news.py` | Headline dedup by hash; sentiment client with a mocked Gemini response; malformed-response handling. |
| `test_sso.py` | Ticket single-use, TTL expiry, target allow-list rejection, uid binding. |

Simmer has **no JSX counterpart**, so there are no parity tests — the Python
engine is ground truth. That makes `test_engine.py` fixtures the only guard
against silent math regressions; they should be checked in as explicit
input→expected tables, not generated.

Outcome evaluation (did the short strike hold?) reuses the `evaluator.py`
pattern and writes `simmer_outcomes`, giving the same accuracy-calibration
capability Matrix has.

---

## Gap analysis and open risks

### Corrections to widely-repeated rules

These are load-bearing. Each one, implemented the common way, produces a wrong
engine:

| # | Common rule | Correct |
|---|---|---|
| 1 | POT ≈ 2 × delta | **POT ≈ 2 × N(−d2)** — the folk rule understates touch risk 5–45% |
| 2 | `EV = POP×credit − (1−POP)×maxloss` | Structurally biased negative — **integrate the payoff** |
| 3 | "The engine has positive EV" | **Risk-neutral EV is exactly zero** — all edge is IV vs realized vol |
| 4 | "Collect ⅓ width at 16 delta" | **Mathematically impossible.** Max C/W = P(short ITM) |
| 5 | `EM = straddle × 0.85` | `1 SD = 1.2533 × straddle`; straddle/S is the **MAD**, ~20% below 1 SD |
| 6 | "45 DTE is the theta sweet spot" | **Θ/Γ is constant across DTE** — the rationale is false (the rule still works, for a different reason) |
| 7 | Garman-Klass / Parkinson for RV | **~23% biased low** with overnight gaps → universal VRP false positives. Use Yang-Zhang |
| 8 | "IV − HV > 5 vol points" | VRP gates must be **ratios** |
| 9 | "Sell 5–10 delta, it's safer" | **Negative-EV.** Optimum is 0.25–0.30 |
| 10 | "Steep put skew = more premium" | True for naked puts; **backwards for put credit spreads** |

### Blocking — resolve before or during Phase 1

**1. Cross-origin SSO needs a decision.** Ambient SSO between two `*.vercel.app`
origins is impossible (Public Suffix List). Either buy a domain (~$15/yr,
transparent) or build the opaque-ticket hand-off. **Build the ticket now
regardless** — it works today and survives the later domain move.

**2. Matrix's per-tab session model conflicts with SSO.** `sessionStorage` gives
per-tab multi-user isolation; a shared cookie destroys it. Mutually exclusive.
Decide in the same PR that ships SSO.

**3. The options tape has no history and accrues only forward.** Tradier's
WebSocket offers no replay. **Start the recorder in Phase 0, before any consumer
exists.** Every day of delay is unrecoverable data.

**4. Tradier permits one streaming session at a time.** Nothing uses the
WebSocket today, so no conflict now — but it is a hard ceiling. The recorder must
be a single shared process on the house account feeding all users, never
per-user streams.

> **Resolved:** the IV-history bootstrap, which looked like a hard blocker, is
> solved by the **cross-sectional VRP percentile** — ranking `IV30/RV20` across
> the watchlist needs zero history and performs on par with 252-day IVR. Ship
> that on day one and blend in the time-series signal as history accrues.

### Significant — plan for, don't block on

**5. No LLM client exists on the backend.** Zero references in
`market/backend/app/`. `httpx` is already a dependency, but client, retry,
schema validation and caching are net-new.

**6. Signals will be live but unfalsifiable.** After Phase 1 there will be flow
and sentiment signals with **no way to know whether they work.** The
highest-value purchase is therefore *historical options data for backtesting* —
not a real-time flow feed. It also backfills IV history, solving two problems
with one spend.

**7. Composite weights barely matter; signal quality dominates.** Simulated
selection showed <0.1pp difference between arithmetic, geometric and hard-gated
scoring forms, versus a 2–5pp gap to the oracle. **Do not spend effort tuning
weights. Spend it on estimating VRP and liquidity accurately.**

**8. Two requirement claims are unvalidated by any literature found:** that
abnormal news *volume* predicts *implied* volatility expansion, and that the
weights are correct. Both testable on our own data via `simmer_outcomes`.

**9. Sandbox cannot exercise the tape** (15-min delayed, no tick data). The
WebSocket path can only be developed against production — use a read-only
production credential and keep the recorder strictly separate from order paths.

**10. Feed quality is unverified.** Whether Tradier's option `timesale` is full
OPRA, and whether `flag` carries OPRA condition codes, are unknown. **Run the
reconciliation test during market hours before building the sweep detector.** If
`flag` is empty, spread legs can't be filtered and sweep output stays
low-confidence.

**11. Deployment details that break silently:** `sync_supabase_site_url()`
rewrites the redirect allow-list from `$VERCEL_PROJECT` on **every** deploy, so
with two Vercel apps each deploy can clobber the other's allow-list — it must
emit both origins, not just the one being deployed. Turnstile needs the new
hostname (else error 110200). And Matrix's `edgelane.config.js` has no cache
header, so a rotated tunnel URL can be served stale.

*(CORS is no longer on this list — `edgelane-simmer` was chosen precisely so it
matches the existing regex.)*

**12. No frontend reads `tools_enabled`** — needed for a proper "not enabled"
screen instead of a bare 403.

**13. Simmer fishes in the weaker pool.** Index VRP exceeds single-name VRP
because of the embedded correlation risk premium. Targeting single names is a
product decision, but it means leaning harder on structural gates to compensate —
and being honest with the user about it.

### Unresolved practitioner disagreements — make configurable, log outcomes

IVR threshold 30 vs 50 · IVR vs IVP primacy · IV-vs-own-history vs
IV-vs-forecast-RV as the gate (Sinclair/ORATS take the latter) · whether stop
losses help or hurt short premium (our simulation says help, contradicting common
claims) · whether steep skew is opportunity or warning · selling into earnings.

Where practitioners split, the engine should expose the knob and record the
outcome. That signal→outcome table is what turns Simmer from a toy into something
validatable.

### Resolved — news sourcing

- **Can v1 ship free? Yes** — Alpaca (Benzinga-sourced, free paper account,
  200 calls/min, REST + WebSocket) plus Gemini at $0–2/month. **The binding
  constraint turned out to be licensing, not rate limits.**
- **Finnhub and Alpha Vantage free tiers are unusable in a sold product** —
  both restrict to personal, non-commercial use, and Finnhub explicitly extends
  that to *derived results*, i.e. our own sentiment score.
- **TradingView is off the table** — ToS bans commercial API usage,
  non-display/algorithmic use and redistribution, with named enforcement rights.
- **Webull has no news endpoint**, but its Fundamentals release covers earnings,
  filings, dividends and **analyst price targets** — the requirement's "latest
  price targets" — and is already an authenticated dependency.
- **FinBERT stays excluded**, and the apache-2.0 `distilroberta` alternative does
  **not** cure the problem (same Financial PhraseBank provenance).

Two items still need empirical verification rather than research: Webull
fundamentals response shapes (docs 404 without auth), and which repo our
`webull-openapi-python-sdk>=2.0` pin resolves to — the old one was archived
2026-06-22.

### Deliberately out of scope

Dark pool / ATS data (stale or retail-contaminated), paid flow feeds (inferences
over a tape we can record ourselves), earnings premium harvesting (edge is inside
transaction costs and may be a model artifact), and order placement (Torque's).

---

## Phased build plan

Each phase is independently shippable and leaves the system working.

### Phase 0 — foundations (no user-visible change)

1. `supabase/migrations/0010_simmer.sql` + `make db-push`.
2. Add `simmer` to the startup grant list; scaffold the `ensure_tool` gate.
3. Extend `gex_snapshots` with volume columns; add `last_volume` and
   `trade_date` to the contract normalizer.
4. Create `simmer_iv_history`; **start recording daily ex-earnings ATM IV, plus
   OHLC for Yang-Zhang.**
5. **Stand up the WebSocket `timesale` recorder** and start persisting the tape.
6. Run the feed-quality reconciliation test during market hours; record the
   answer in this doc.

> Steps 4 and 5 have a time cost that cannot be bought back. Do them first even
> though nothing consumes them yet.

### Phase 1 — the engine, headless

7. `simmer_config.py` — weights, gates, per-ticker rules.
8. `simmer_engine.py` — hard gates, Yang-Zhang RV, cross-sectional VRP
   percentile, expected move (1.2533 × straddle), payoff-integrated EV,
   `Alpha = EV/MaxLoss`, POP at breakeven, delta-solved width. Pure functions.
9. **Market-regime state** — fetch SPY/QQQ/VIX chains each sweep (index data is
   needed even though index *trading* is excluded); compute VIX/VIX3M term
   structure, index put skew vs its trailing distribution, and gamma-flip
   position; expose a single `risk_off` / `stressed` state that suppresses the
   put side.
10. `macro_calendar.json` + the `valid_through` startup assertion.
11. SEC EDGAR `getcurrent` poller, CIK→ticker map, hard-block item list, keyed on
    `acceptanceDateTime`.
12. Earnings calendar with two-source cross-check and week-blocking on disagreement.
13. `tests/simmer/test_engine.py` + `test_gates.py` with checked-in
    input→expected fixtures. **Include a regression test asserting risk-neutral
    EV ≈ 0** — it catches whole classes of pricing error.

### Phase 2 — API and watcher

14. `routes/simmer.py`, full surface behind the gate.
15. `simmer_watcher.py` — 5-minute union-of-watchlists sweep.
16. **Two-tier cache**: `simmer_research_cache` (keyed `symbol`, per-field TTLs)
    behind a single accessor, so tier-1 reuse and invalidation are enforced in
    one place rather than at each call site. Tier-1 is mostly empty until
    Phase 3 fills it with news — build the mechanism now so news lands into it.
17. **Cross-sectional dispersion check** — in the same sweep, flag a sector
    dislocation when a disproportionate share of watched names spike IV together
    while the index regime reads calm; suppress the affected cluster. Start with
    sector tags, upgrade to the pairwise correlation matrix later.
18. Alert state machine with hysteresis; `simmer_alerts` writes.
19. **`simmer_outcomes` table + paper-outcome evaluator** — for every
    recommendation the engine has ever emitted, revisit it at expiry and record
    the one fact that matters: *did the short strike hold?* Reuses the
    `evaluator.py` pattern; needs no broker, no fill, no Torque.
20. **Settings enforcement** — apply per-user thresholds at read/alert time;
    validate `gate_overrides` against the toggleable allow-list on write so a
    client can never disable a locked gate; clamp the short-delta band to
    0.15–0.40. Outcomes keep recording the **engine's** verdict, not the user's
    filtered view.
21. `test_auth_gate.py`, `test_watcher.py`, `test_alerts.py`, `test_outcomes.py`,
    `test_settings.py` (locked gates reject; bounds clamp; calibration
    unaffected by config).

At this point Simmer is fully functional via `curl`. **Validate it there before
building any UI.**

> **Why outcome recording is here and not in Phase 5.** This data only accrues
> forward. If the engine starts emitting recommendations in Phase 2 and nothing
> records what happened to them, then Phase 5 begins with an empty table — you'd
> have run a live engine for months with no idea whether it was right. Paper
> outcomes are cheap (the chain data needed to answer "did the strike hold?" is
> already being fetched) and they are the only way the composite weights ever
> stop being guesses. **Start the scorekeeping the day the engine starts
> scoring** — same logic as starting the tape recorder in Phase 0.

### Phase 3 — news and sentiment

22. Alpaca news client (REST backfill + WebSocket live) with wire-RSS fallback;
    headline-cluster dedup (Jaccard ≥ 0.70 / 6h). Store derived outputs and
    headline+URL only — never re-host article bodies.
23. Gemini client — structured output, clamping, article-id-keyed caching so only
    *new* headlines are ever scored.
24. Velocity detector (NB tail-p, seasonal baseline, EB shrinkage, min-cluster gate).
25. Sentiment persistence with asymmetric half-lives and the earnings gate.
26. Wire sentiment in as **side-selection and veto only**.

### Phase 4 — frontend

27. Scaffold `simmer/ui` (SvelteKit 5 + TS + Tailwind v4 + `adapter-static`);
    port `app.css` from Matrix; deploy an empty shell to validate the pipeline.
28. `deploy-simmer-fe`; CORS, Turnstile and Supabase allow-list updates.
29. AuthGate → ProductGate → AppShell → WatchlistManager → ReadinessCard.
30. SSO ticket hand-off — **update Matrix in the same PR**; SSO is symmetric and
    half of it is useless.
31. **SettingsPanel** — presets (Conservative/Balanced/Aggressive) front and
    centre, Advanced drawer behind them, and active overrides surfaced **on the
    readiness card** rather than only in settings.
32. Charts: `GexWallChart`, `IvRankGauge`, `ExpectedMoveViz` (load the `dataviz`
    skill first). Surface **two POP numbers — market-implied and forecast — since
    the gap between them is the edge.**

### Phase 5 — closing the loop on *real* fills

Paper outcomes already exist from Phase 2. This phase upgrades them to actual
ones and puts the results on screen.

33. **"Send to Torque" hand-off**, writing a `simmer_positions` row: the
    recommendation, the **actual fill price**, and the exit targets. This is what
    turns a paper outcome into a real one.
34. **Predicted vs achieved credit** — feeds straight back into the fill model,
    so the ranking gets more honest the longer the product runs.
35. **Calibration surface** — forecast vs realized POP, and per-component
    predictive value, so components that don't predict can be *retired*.
36. **Public scorecard** — live, timestamped, including losers. No competitor in
    the category has third-party-audited performance; publishing a falsifiable
    one is a cheap and durable differentiator.
37. Email alerts via the existing `emailer`.

> This loop is the only genuinely durable asset in the design. A screener can
> never close it (it never sees a fill); a broker will never build the engine
> (liability). Simmer is the only party that can hold both ends.

### Deferred to v1.1 — not on the critical path

38. **Short interest + days-to-cover as a call-side squeeze veto.** Ingest the
    free FINRA short-interest file (semi-monthly) and daily short-sale volume;
    compute `days_to_cover`; block bear calls and the call leg of iron condors
    above the configured threshold. Nothing else depends on it, so it lands after
    the core engine is proven. *(Use short **interest**, not daily short volume
    as a level — see the market-maker hedging caveat in Data sourcing.)*

### Sequencing note

Phases 0–2 deliver a working engine with **zero external dependencies beyond
Tradier**. Phase 3's provider choice is the only thing gated on outstanding
research, and it is deliberately last among the backend phases so it cannot block
anything else.
