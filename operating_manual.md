# EdgeLane Operating Manual

How to run and operate the stack: Torque, Simmer, the admin tools, deployment,
and troubleshooting.

> **Looking for Matrix?** Its badges, scores, thresholds and decision rules live in
> **[docs/matrix_operating_manual.html](./docs/matrix_operating_manual.html)** — open
> it in a browser.
>
> This file used to open with ~630 lines describing the *retired* single-file
> optimizer (`edge_lane.html`): a manual "Detect bias from Greeks" button, hand-set
> strategy / target-delta / wing-width inputs, three side-by-side candidate cards,
> and broker credentials kept in `sessionStorage`. None of that matches the shipped
> product any more — Matrix polls continuously, picks the structure itself, and
> stores broker connections in Supabase — so it was removed in 2026-08 rather than
> left to mislead. Git history has it if you want the old text.

---

## Torque — fast order builder

Separate page on the market backend (`:8789/torque`) for placing multi-leg
orders fast — **no** bias logic. Run: `cd market/backend && make run-dev` (paper)
or `make run-prod` (real). Build/deploy doc: `docs/torque.md`; Torque's own
badge/reading reference (this file's counterpart, for Torque): `docs/torque_operating_manual.html`.

1. Pick a **ticker** + one **strategy** (10, incl. single-leg). Legs auto-fill at
   per-ticker offsets snapped to the live grid; −/+ nudges strikes.
2. **Spot** is live (Yahoo, parity fallback); strikes + net bid/mid/ask re-anchor
   every ~3s. Type a limit or **Send Market**. **DRY RUN** validates without placing.
3. **Auto-close +N%** (default 30%): on confirmed fill it places the profit-target
   close (single-leg limit → native OTO; spreads/market → background watch-then-close).
4. **Orders panel** (bottom): working orders + armed closes — click a limit to
   modify, or cancel. Env badge **SANDBOX** (gold) / **PRODUCTION** (red).

## Simmer — premium-selling readiness engine (live: backend + UI)

Watches your ticker watchlist every 5 minutes and tells you **when a name is
conditioned to sell a credit spread** — structure, strikes, credit, POP at the
breakeven, payoff-integrated EV — gated hard on catalysts (SEC 8-K hard-blocks,
earnings blackouts, macro calendar), liquidity, VRP, and the 0.20–0.35 short-delta
band. Most names are **vetoed most of the time; the veto reasons are the
product.** Full doc: `docs/simmer.md`.

**Backend** (live — rides in the market service, no separate process):

- Endpoints under `/simmer/*`, gated on the `simmer` entitlement
  (`profiles.tools_enabled`); admin token works too:
  `/simmer/status` (watcher health + regime), `/simmer/analyze/{sym}` (full
  envelope), `/simmer/news/{sym}` (scored headline clusters + velocity),
  `/simmer/watchlist`, `/simmer/alerts`, `/simmer/settings`,
  `/simmer/outcomes/summary` (paper-outcome calibration).
- **Watchlist writes are client-side under RLS** (frontend, when built); today
  seed rows into `simmer_watchlist` via service role. Alerts are written only by
  the backend and fan out per user with that user's own thresholds.
- News: Alpaca (free paper-account keys) → dedup into clusters → Gemini scores
  only never-seen headlines (~$0.25/mo at 20 tickers). No keys = clean no-op;
  sentiment can only pick the side or veto, never promote a score.

**Config check before any deploy** (all in `edgelane_market.config`):

| Key | Check |
|---|---|
| `DEVMODE` | **`false` for prod.** The flag flips Tradier base+token+account AND the DuckDB file in one go — a prod container with `true` serves users sandbox quotes and writes history into the sandbox DB while looking perfectly alive. Local testing flips it `true`; always flip back. |
| `SIMMER_DATA_PROVIDER` | `tradier` (default) or `yahoo` — Yahoo keeps the sweep off the 120 req/min Tradier budget Matrix/Torque share, and supplies the daily OHLC bars that IV-history/outcomes need. |
| `SIMMER_NEWS_PROVIDER` | `alpaca` \| `rss` \| `off` |
| `ALPACA_KEY_ID` / `ALPACA_SECRET_KEY` | From a free paper account (app.alpaca.markets → enable MFA first, or the API-keys widget stays hidden). |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | Same key the legacy frontend uses; default model `gemini-2.5-flash-lite`. |

**Deploy (backend — the only Simmer surface today):**

```bash
# one-time per schema change
make db-push                 # applies supabase/migrations (0010 = Simmer tables)

# every code change
sed -n 's/^DEVMODE=/&/p' edgelane_market.config   # eyeball it: must be false
make deploy-be-restart       # rebuilds the container from the working tree
```

The config file is bind-mounted into the container, so key changes need only a
container restart, not a rebuild. `make stop` is safe for the local dev server
(SIGTERM + grace — a hard kill corrupts the DuckDB WAL and the next boot dies
replaying it; if that happens: delete the `.wal` file next to the DB and reboot).

**Frontend** (live): a SvelteKit 5 SPA (adapter-static, runes) at
`https://edgelane-simmer.vercel.app`. Sign-in, watchlist/settings writes, and every
read go through the backend API — the **browser never contacts Supabase directly**
(auth flows through the backend `/auth/*` proxy; see `market/backend/app/routes/auth_proxy.py`).

### The moving pieces (and how they find each other)

| Piece | Lives on | How it's reached |
|---|---|---|
| Backend (market API + Torque + Simmer) | local Docker container `edgelane-backend` | Cloudflare **named tunnel** → permanent hostname **`edge.facades.trade`** |
| Cloudflared connector | container `edgelane-cloudflared` | `cloudflare/cloudflared` running `tunnel run` with `CF_TUNNEL_TOKEN` |
| Matrix UI | Vercel project `edgelane-matrix` at `matrix.facades.trade` | calls the baked `window.__EDGELANE_API_BASE__` |
| Simmer UI | Vercel project `edgelane-simmer` at `simmer.facades.trade` | calls the baked `window.__EDGELANE_API_BASE__` |
| Torque | served by the backend itself | `https://edge.facades.trade/torque` |

One host serves all three products. The hostname belongs to the **tunnel**, not to
the container, so restarts, rebuilds and host moves all keep the same URL — which is
why both frontends simply **bake** it at deploy time from `EDGELANE_API_BASE`. There
is no runtime pointer, no publisher, and no self-heal step any more; if you change
the hostname, redeploy the frontends.

> The Supabase `app_config.api_base` row and the Vercel Edge Config store that the
> old rotating-URL scheme used are no longer read or written by anything. They're
> inert — drop them whenever convenient.

### One-time setup — `make frontend-setup`

Run once (and again whenever a Vercel project is added or renamed).
Idempotent; safe to re-run.

```bash
vercel login            # setup reads this CLI session for provisioning — no token to paste
make frontend-setup     # 1) links/creates BOTH Vercel projects (Matrix + Simmer)
                        # 2) syncs Supabase Auth uri_allow_list with both origins
```

`deploy-fe` / `deploy-simmer` **refuse** until the project is linked and point you
here. Provisioning uses your Vercel CLI login (`~/.local/share/com.vercel.cli/auth.json`).

**The tunnel is created once, in the Cloudflare dashboard** (Zero Trust → Networks →
Tunnels → Create tunnel), with public hostname `edge.facades.trade` →
`http://edgelane-backend:8789`. Paste the connector token into `deploy/.env`:

```bash
CF_TUNNEL_TOKEN=eyJ...                        # deploy/.env — compose fails fast if blank
EDGELANE_API_BASE=https://edge.facades.trade  # baked into both SPAs; deploy.sh dies if blank
```

### Deploy / update — what to run, in what order

Do only the steps for what changed. The **sequence matters** (schema → backend →
frontends) because a frontend may depend on a new backend field, not because any URL
moves — the tunnel hostname is fixed.

1. **DB schema changed** → `make db-push` (applies `supabase/migrations`).
2. **Backend / engine changed** → verify `DEVMODE=false`, then `make deploy-be`
   (rebuild) or `make deploy-be-restart`. The tunnel hostname is unchanged, so no
   frontend redeploy is needed unless the UI itself changed.
   - Config-only change (keys in `edgelane_market.config`): the file is bind-mounted —
     a container restart suffices, no rebuild.
3. **Matrix UI changed** → `make deploy-fe` (also re-syncs Supabase `site_url` +
   `uri_allow_list`, which is where the Simmer origin gets added if setup didn't).
4. **Simmer UI changed** → `make deploy-simmer`. (npm ci + `vite build` can take
   >2 min; that's normal.)
5. **Always finish with** → `make check-tunnel` — verifies container health, tunnel
   `/status`, CORS for the frontend origin, and the Turnstile path.

**Make sure, before/after:**

- `DEVMODE=false` in `edgelane_market.config` before any prod backend deploy (it flips
  Tradier token/base/account **and** the DuckDB file in one switch).
- `CF_TUNNEL_TOKEN` and `EDGELANE_API_BASE` are set in `deploy/.env` (**not** in
  `edgelane_market.config` — compose reads them from `.env`).
- After a backend restart, confirm the connector reattached —
  `docker compose -f deploy/docker-compose.yml logs -f edgelane-cloudflared`.
- The Simmer origin is in Supabase `uri_allow_list` (so confirmation/reset emails can
  redirect back) — added by `make frontend-setup` or the next `make deploy-fe`.

### Simmer UI internals worth knowing

- **Deployed-vs-dev is a build-time constant** (`import.meta.env.PROD`), not baked
  config. A production `vite build` locks the `?api=` override and carries **no**
  Supabase creds or secrets in the browser; the backend URL comes from the baked
  `window.__EDGELANE_API_BASE__`.
- **SPA routing on Vercel:** `simmer/ui/vercel.json` uses `rewrites: /(.*) → /index.html`
  and **no `cleanUrls`**. Vercel's `source` is path-to-regexp — **no lookaheads**
  (`(?!api)` silently matches nothing); `/api/*` stays safe because functions/static
  files are matched before rewrites.

## Strike Profiles — debit strike picker config (admin)

Admin-only page on the market backend for tuning **how the engine builds debit
verticals** (`bull_call` / `bear_put`) per ticker — the OTM-OTM "buy the move,
sell the wall" strike picker. Config only; **no** orders are placed here. Standalone
page (not linked from the dashboard). Full doc: `docs/strike_profiles.md`.

**How to access:** open `…/strike-profiles/admin?token=<ADMIN_API_TOKEN>` on the
backend host — local `http://127.0.0.1:8789/strike-profiles/admin?token=…`, or prod
the Cloudflare tunnel URL + the same path. Auth (same admin secret as Torque):
- Browser nav → append `?token=<ADMIN_API_TOKEN>` (the page then sends it as the
  `X-Admin-Token` header on every API call).
- API/curl → send `X-Admin-Token: <ADMIN_API_TOKEN>`.
- When `AUTH_ENABLED=true`: anonymous → **401**, signed-in non-admin user → **403**,
  admin token → **200**. When `AUTH_ENABLED=false` (dev) the gate is a no-op.

1. **List** — every saved profile (one row/symbol; `DEFAULT` is the fallback for any
   unconfigured ticker). Shows enabled, long Δ-band, target source, width window,
   snap, min OI.
2. **Edit** — click a row → form prefilled from the current values. Save is a
   **full-object replace** (the page loads then re-sends the whole object). Blank
   nullable fields (`long_offset_pts`, `min/max_width_pts`) mean **auto / derive
   from expected move**, not `0`.
3. **Add symbol** — type a ticker → a `DEFAULT`-seeded form → save creates the
   override. Also add it to `SYMBOLS=` in `edgelane_market.config` so the poller
   fetches it.
4. **Applies on the next poll cycle (~15s)** — no restart needed. SPX + NDX ship
   seeded; unconfigured symbols inherit `DEFAULT`. (No delete endpoint yet.)

## tp — positions & P&L CLI

`tp` (`tools/tradier_positions.py`) shows positions and realized P&L. Full doc:
`tools/tp_operating_manual.html`.

- `tp` — open positions. `tp -G` — realized gain/loss (merges today's `/orders`
  with prior-day opens via `/history`, back to 7 days; partials use the
  whole-trade average as cost basis).
- `tp -B SYM STRIKE [DATE]` — open a single-leg position. The date is optional
  (defaults to today) and can be an explicit date **or** `+N` for "N trading
  days out": `tp -B spy 738c+1 -x` (or `tp -B spy 738c +2`). `+N` resolves to
  the Nth real expiration in the live chain, so weekends/holidays are skipped
  automatically and the target date is guaranteed tradeable — `+1` on a Thursday
  whose Friday is a market holiday lands on Monday.

---

## Troubleshooting

| What you see | What's happening |
|---|---|
| Heavy single-name ticker (MU, AMD, SMCI, COIN, PLTR, ARM, AVGO, MARA, MSTR) is slow on the first poll | The full options chain is being pulled in pieces. Normal for these tickers. |
| Matrix pill reads `Closed · … · paused` | Market is closed, so the UI drops to the backend's slow display-only cadence instead of re-fetching unchanged data. Full cadence resumes within a minute of the bell. |
| Lookup cell says "intrinsic fallback" | At least one leg is missing IV data from the provider; that cell uses intrinsic value instead. |
| Symbol fails after ~75 s with a timeout error | Provider didn't respond. Try again in a minute; if persistent, the symbol may need a wider timeout configuration. |
| `Accuracy —` never resolves to a percentage | Fewer than `EVAL_MIN_GRADED` (10) graded outcomes, or the regime alert is active. Both are deliberate — see the self-evaluation section of the Matrix manual. |

### Simmer / deployment / tunnel

| What you see | What's happening / fix |
|---|---|
| Simmer prod **login fails** or the app calls `edgelane-simmer.vercel.app/auth/...` (404) | The build didn't bake `EDGELANE_API_BASE`, so it fell back to its own origin. Check `simmer/ui/build/edgelane.config.js` sets `window.__EDGELANE_API_BASE__` to `https://edge.facades.trade`, then `make deploy-simmer`. (`deploy.sh` now refuses a blank base, so this should be impossible on a fresh deploy.) |
| Simmer **subroutes 404** on direct load/refresh (`/settings`, `/news`) but `/` works | SPA fallback not firing. `simmer/ui/vercel.json` must have `rewrites: /(.*) → /index.html` and **no `cleanUrls`** (it conflicts with the rewrite target). Never use a lookahead in `source` — path-to-regexp silently no-ops it. Redeploy with `make deploy-simmer`. |
| After a backend restart, neither UI can reach the backend | The tunnel hostname is permanent, so this is the connector rather than a rotation. Check `docker compose … logs edgelane-cloudflared` for a registered connector, and that the tunnel still routes `edge.facades.trade` → `http://edgelane-backend:8789`. |
| Frontends can't reach the backend after a restart | The hostname is permanent, so this is the tunnel connector, not a rotation. Check `docker compose … logs edgelane-cloudflared` for a registered connector, and that the tunnel's public hostname still routes to `http://edgelane-backend:8789`. |
| Simmer email confirmation / password-reset link is **rejected on redirect** | The Simmer origin isn't in Supabase Auth `uri_allow_list`. Run `make frontend-setup` or `make deploy-fe` (both PATCH it), or add `https://edgelane-simmer.vercel.app/**` manually. |
| `make frontend-setup` can't provision | You're not logged in. Run `vercel login`, then re-run. |
| Simmer prod `edgelane.config.js` shows `API_BASE = null` | The deploy didn't bake `EDGELANE_API_BASE`. Set it in `deploy/.env` and re-run `make deploy-simmer`. |
