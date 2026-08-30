# EdgeLane — Deployment & Operations Manual

The single consolidated guide for getting EdgeLane running on a new workstation,
building it after code changes, validating it end-to-end, and shipping it to
production (market backend on Docker + Cloudflare tunnel, dashboard on Vercel).

> This file replaces the old split between `deployment.md` and `deployment_manual.md`
> — both are now folded in here.

---

## Contents

1. [What EdgeLane is, briefly](#what-edgelane-is-briefly)
2. [The three deployables](#the-three-deployables)
3. [Architecture in one diagram](#architecture-in-one-diagram)
4. [File layout](#file-layout)
5. [Prerequisites](#prerequisites)
6. [Initial setup (one-time)](#initial-setup-one-time)
7. [Configuration reference](#configuration-reference)
8. [Tools: the `tp` shell alias](#tools-the-tp-shell-alias)
9. [Test scripts — what to run before committing](#test-scripts--what-to-run-before-committing)
10. [Daily workflow](#daily-workflow)
11. [Switching between sandbox and production](#switching-between-sandbox-and-production)
12. [Market backend & Torque (local run)](#market-backend--torque-local-run)
13. [Troubleshooting](#troubleshooting)
14. [Production deployment](#production-deployment)
15. [Quick reference card](#quick-reference-card)

---

## What EdgeLane is, briefly

A hybrid options-spread optimizer:

- Pulls live options chain + Greeks from Tradier (EdgeLane originally used Atlas's API (mind-vest.io) as its data provider; Atlas was fully retired ~May 2026 and Tradier is now the sole provider)
- Computes a deterministic market-bias signal in Python, server-side
- Scores candidate spreads (verticals, condors, butterflies, iron flies)
- Lets the user sign in, attach a Tradier brokerage connection, copy a ticket to clipboard, or push an order straight to the broker
- A separate Python toolchain (`tp` and friends) covers everything the UI doesn't: listing / closing / modifying / cancelling live positions and working orders.

The product surface is the **market backend** (FastAPI) plus three UIs: **Matrix**
(Vercel), **Simmer** (Vercel), and **Torque** (served by the backend itself).

> The original single-file `edge_lane.html`, built from a ~5,500-line JSX file by
> `edge_lane_build.sh` with Babel-standalone compiling in the browser, was removed
> in 2026-08 along with its dev-only Gemini CORS proxy. Matrix replaced it.

---

## The three deployables

EdgeLane has **three** shippable pieces. Knowing which serves what is the difference
between a one-command deploy and chasing a change that never went live:

| Deployable | Source | Served from | Shipped by |
|---|---|---|---|
| **Market backend API + Torque page** | `market/backend/` (`app/` + `ui/torque.html`) | **Docker container** (FastAPI, behind a Cloudflare tunnel) | `make deploy-be` / `make deploy-be-restart` |
| **Matrix UI** | `market/ui/index.html` | **Vercel** (`matrix.facades.trade`) | `make deploy-fe` |
| **Simmer UI** | `simmer/ui/` | **Vercel** (`simmer.facades.trade`) | `make deploy-simmer` |

> ⚠️ **Torque is part of the backend, not Vercel.** `market/backend/ui/torque.html`
> is `COPY`-baked into the backend Docker image (`Dockerfile`: `COPY ui ./ui`) and
> served by FastAPI at `GET /torque`. So a Torque UI change ships with
> **`make deploy-be-restart`** (which rebuilds the image), **not** `make deploy-fe`.
> `deploy-fe` only pushes `market/ui/index.html` to Vercel. See
> [Production deployment](#production-deployment) for the full serving model.

---

## Architecture in one diagram

```
                        the browser
   ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
   │ Matrix           │  │ Simmer           │  │ Torque           │
   │ matrix.facades…  │  │ simmer.facades…  │  │ …/torque         │
   │ (Vercel, static) │  │ (Vercel, Svelte) │  │ (backend-served) │
   └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘
            └─────────────────────┼─────────────────────┘
                                  ▼   https://edge.facades.trade
                       ┌──────────────────────┐
                       │ Cloudflare named     │  permanent hostname
                       │ tunnel (connector)   │
                       └──────────┬───────────┘
                                  ▼
                       ┌──────────────────────┐        ┌──────────────────┐
                       │ market backend       │───────▶│ Tradier API      │
                       │ FastAPI + DuckDB     │        │ (sandbox / prod) │
                       │ Docker, port 8789    │        └──────────────────┘
                       │  + Torque at /torque │
                       └──────────────────────┘
                        polls · scores · persists · self-evaluates

Per-user broker orders route through the backend using each user's own
Tradier connection — the house token is read-only market data.

Python side (no browser involved):
    tests/tradier_smoke.py            chain/quote sanity check
    tests/tradier_execute_ticket.py   parse a copy-button ticket + submit
    tools/tradier_positions.py        list / sell / open / modify / cancel
```

Bias decision is **always deterministic**: same chain inputs always produce the
same trade pick, and no LLM is in the decision path. Torque itself does
**no** forecasting (no bias engine) — just positioning/flow reads + order building.

---

## File layout

### Frontend / local-dev

| Path | Role | Commit? |
|---|---|---|
| `market/ui/index.html` | Matrix UI. Static, no build step. | ✓ |
| `simmer/ui/` | Simmer UI (SvelteKit SPA). | ✓ |
| `market/backend/` | The backend: engine, routes, Torque page. | ✓ |
| `edgelane_market.config.example` | Template config with placeholders. | ✓ |
| `edgelane_market.config` | Your real keys + DEVMODE. | ✗ (gitignored) |
| `tools/tradier_positions.py` | `tp` — Tradier position + order manager. | ✓ |
| `tools/tp_operating_manual.html` | Operating manual for `tp` (HTML). | ✓ |
| `tests/tradier_smoke.py` | Tradier API sanity check. | ✓ |
| `tests/tradier_execute_ticket.py` | Parses a copy-button ticket, posts via Tradier. | ✓ |
| `tests/utils.py` | Shared test helpers (config loader, etc.). | ✓ |
| `docs/` | Implementation notes (`torque.md`, `torque_operating_manual.html`, `tradier_implementation.md`, etc.). | ✓ |
| `docs/matrix_operating_manual.html` | Matrix: onboarding, badges, scores, decision rules. | ✓ |
| `docs/torque_operating_manual.html` | Torque: controls, readings, labels. | ✓ |
| `simmer/simmer_operating_manual.html` | Simmer: readiness scoring + operations. | ✓ |
| `deployment.md` | This file. | ✓ |

### Deploy / production

| File | Role | Commit? |
|---|---|---|
| `deploy.sh` | Orchestrator (frontend Vercel + backend Docker/tunnel). | ✓ |
| `Makefile` (repo root) | `deploy-be` / `deploy-fe` / `deploy-be-restart` / `doctor` / … targets. | ✓ |
| `deploy/docker-compose.yml` | Backend + cloudflared quick-tunnel/publisher sidecar. | ✓ |
| `market/backend/Dockerfile` | Backend image (`COPY app` + `COPY ui`; serves API + Torque). | ✓ |
| `market/backend/.dockerignore` | Keeps secrets/tests out of the build context. | ✓ |
| `tools/db_push.py` | Applies `supabase/migrations/*.sql` (schema) via the Management API. | ✓ |
| `tools/supabase_admin.py` | Standalone no-SQL data admin CLI: list tables + CRUD. | ✓ |
| `deploy/.env.example` | Template: Supabase keys, image, Vercel project, optional tunnel token/API base. | ✓ |
| `deploy/.env` | Real creds (Supabase service key, etc.). | ✗ (gitignored) |
| `vercel.json` | Static deploy config (`outputDirectory: dist`). | ✓ |
| `.vercelignore` | Hides backend/secrets/legacy from the Vercel upload. | ✓ |
| `dist/` | Staged frontend output (built by `deploy.sh`). | ✗ (gitignored) |

---

## Prerequisites

| What | Why | Notes |
|---|---|---|
| WSL2 with Ubuntu (or any Linux) | Backend + tools run there | Windows users: use WSL, not Git Bash |
| Python 3.10+ | Backend + all CLI tools | the CLI tools are stdlib-only; the backend has its own venv (`make setup`) |
| Bash | `deploy.sh` + make targets | comes with WSL/Linux |
| A browser | Open Matrix / `/torque` | Chromium-family preferred for DevTools depth |
| Node.js | Simmer build, parity tests, Vercel deploy | `vercel` CLI; `node` runs the parity harness |
| Docker | Production backend (container + tunnel) | only needed to `deploy-be` |
| `curl` | Health checks (`make status`, `make check-tunnel`) | universally available |

---

## Initial setup (one-time)

```bash
# 1. Enter the repo
cd /mnt/c/soljet_dev/EdgeLane

# 2. Create your config from the example
cp edgelane_market.config.example edgelane_market.config

# 3. Open it and fill in your keys — see the next section for what each does
$EDITOR edgelane_market.config

# 4. One-time backend venv + deps
cd market/backend && make setup && cd ../..

# 5. Boot the backend (sandbox Tradier, polls regardless of market hours)
cd market/backend && make run-dev

# 6. Open a UI against it
make ui                        # Matrix  → market/ui/index.html
# Torque → http://127.0.0.1:8789/torque
```

That's the whole loop. Everything below is detail and tooling around those commands.

---

## Configuration reference

`edgelane_market.config` is the single source of secrets + environment flags for
the backend, Torque, and the Python CLI tools. KEY=VALUE, `#` comments. Gitignored.

Deploy-time values live separately in `deploy/.env` (`CF_TUNNEL_TOKEN`,
`EDGELANE_API_BASE`, Vercel/Supabase provisioning) — read by `deploy.sh` and
docker compose, never by the running app.

### Required keys

| Key | What it's for |
|---|---|
| `TRADIER_TOKEN` | Operator-level Tradier token (chain + GEX + quote) |
| `TRADIER_TOKEN_SANDBOX` | Sandbox token (used when `DEVMODE=true`) |
| `SYMBOLS` | Which underlyings the poller tracks, comma-separated |

### Environment toggles

| Key | Values | Effect |
|---|---|---|
| `DEVMODE` | `true` / `false` | `true` → sandbox token + `sandbox.tradier.com` + sandbox DB, and the poller runs regardless of market hours. `false` → production token, market-hours gated. Also selects which token the Python CLI tools (`tp`, `tradier_smoke.py`) use. |
| `AUTH_ENABLED` | `true` / `false` | Supabase JWT enforcement + rate limiting. `false` for local dev. |
| `EMAIL_PROVIDER` | `auto` / `smtp` / `brevo` | Contact-form transport. See [contact tickets](./docs/contact_tickets.md). |
| `FORCE_POLL_WHEN_CLOSED` | `true` / `false` | Override the off-hours polling policy. |

The `make` targets manage `DEVMODE` for you: `make run-dev` rewrites it to `true`,
`make run-prod` to `false`, and plain `make run` leaves whatever is in the file.

> ⚠️ Inline `#` comments inside an **unquoted** value get parsed as part of the
> value by `tests/utils.py:load_config`. Write `TRADIER_TOKEN="abc"  # prod`, or
> drop the comment.

---

## Tools: the `tp` shell alias

`tp` is long-named. Add an alias to your shell to save typing:

```bash
# Append to ~/.bashrc
cat >> ~/.bashrc <<'EOF'
alias tp='python3 /mnt/c/soljet_dev/EdgeLane/tools/tradier_positions.py'
EOF
source ~/.bashrc
```

### `tp` — Tradier position & order manager

Full reference: `tools/tp_operating_manual.html`. Quick highlights:

```bash
tp                                           # list open positions (with P&L)
tp 3 -L 1.20                                 # close position #3 at LIMIT $1.20
tp --open '<copy-button ticket>' --execute   # open new position from ticket string
tp -O                                        # list working orders
tp -O 2 -L 1.35 --execute                    # modify order #2 limit to $1.35
tp -C 2 --execute                            # cancel order #2
```

All destructive actions default to PREVIEW (no `--execute` = dry run).

## Test scripts — what to run before committing

These integration scripts are stdlib-only, load keys from `edgelane_market.config`, and run from the repo root. Run them in this order when you've touched anything that touches the data path:

### 1. Tradier API sanity (provider-side)

```bash
python3 tests/tradier_smoke.py SPY
python3 tests/tradier_smoke.py NDX 2026-06-20    # specific underlying + expiration
```

Pings `markets/quotes`, `markets/options/expirations`, `markets/options/chains`. Strict pass criteria. Doesn't depend on the proxy.

### 2. Chain pipeline (matches JSX filter logic)

```bash
python3 tests/tradier_chain_pipeline_probe.py NVDA 2026-06-20
```

Mirrors EdgeLane's client-side filter step (±30% band, bid>0 selection). Useful when the bottom panel goes empty for a specific ticker.

### 3. Ticket parser round-trip

```bash
python3 tests/tradier_execute_ticket.py \
  'Sell Aggressive · SPX · 2026-05-21 · Short 7385P / Long 7365P · LIMIT credit $6.00 GTC · 1 contract'
```

Parses the ticket, resolves canonical OCC root, posts a preview-only order. No `--execute` = dry run. Tells you if EdgeLane's copy-button output is valid for Tradier.

### 4. Market backend tests (when you touch `market/backend`)

```bash
cd market/backend && make test    # parity (JSX↔Python) + torque + guardrails
```

Must be green before commit. See [Market backend & Torque](#market-backend--torque-local-run).

---

## Daily workflow

```bash
# ── Start of day ──
cd market/backend && make run-dev      # or make run-prod
make status                            # poller alive? market open?

# ── While working ──
make test                              # 56 parity tests — must stay green
make ui                                # open Matrix against the local backend

# ── Before committing ──
make test
python3 tests/tradier_smoke.py SPY     # provider-side sanity (live token)
cd simmer/ui && npm run check          # only if you touched Simmer
```

Touching `bias_engine`, `strategy_engine`, or `walls` means the parity tests are
the gate — they pin the Python math to the original JSX engine's output.

---

## Switching between sandbox and production

One switch, `DEVMODE` in `edgelane_market.config`:

| `DEVMODE` | Backend + Torque | Python CLI tools |
|---|---|---|
| `true` | sandbox token, `sandbox.tradier.com`, sandbox DB, polls regardless of market hours | sandbox token + base URL |
| `false` | production token, `api.tradier.com`, market-hours gated | production token + base URL |

`make run-dev` / `make run-prod` rewrite the flag before booting; bare `make run`
respects whatever is already in the file.

**Order submission is separate.** Each user attaches their own Tradier connection
in Settings, and that connection carries its own env field. So a user's orders can
route to sandbox while the house token renders a production chain — if those
mismatch, the strikes on screen may not exist on their account.

---

## Market backend & Torque (local run)

FastAPI service in `market/backend/` that polls Tradier, runs the bias/strategy
math (parity-tested vs the JSX), and serves **Torque** (`/torque`), the standalone
multi-leg order builder. Technical detail: `docs/torque.md`; what every control and
reading on screen means: `docs/torque_operating_manual.html`.

```bash
cd market/backend
make setup            # one-time: venv + deps
make run-dev          # sandbox Tradier (paper) — forces DEVMODE=true, boots :8789
make run-prod         # production Tradier (real money) — forces DEVMODE=false
make run              # boot with whatever DEVMODE is in the config
make test             # parity + torque + guardrails tests (must be green before commit)
# Torque page:  http://127.0.0.1:8789/torque
```

- **Config**: `edgelane_market.config` (KEY=VALUE). `DEVMODE=true` → sandbox token
  + `sandbox.tradier.com` + sandbox DB; `false` → production, market-hours gated.
- **`run-dev`/`run-prod` rewrite `DEVMODE`** before booting; bare `run` respects it.
- **Spot** = Yahoo live quote → Tradier put-call parity fallback. **Chain** keeps
  only the live root (NDX→NDXP, SPX→SPXW, RUT→RUTW, DJX→DJXW); UI shows the base ticker.
- **Auto-close** is backgrounded: entry placed → watched until filled → close
  placed then. Orders panel (`/torque/orders`) shows working orders + armed closes.

### How the Torque page is served (and what a change needs)

`GET /torque` returns `market/backend/ui/torque.html` via `FileResponse` with
`Cache-Control: no-store` (`routes/torque.py`). What that means for picking up edits:

| Where you're running | `torque.html` edit | `app/*.py` (Python) edit |
|---|---|---|
| **Local** (`make run` / `run-dev`) | read **live from disk** per request → just **reload the browser** (no restart) | **restart** the process — the `make` targets run uvicorn **without `--reload`** |
| **Docker** (`deploy-be`) | `COPY`-baked into the image → needs an **image rebuild** (`deploy-be-restart`) | same — rebuild the image |

So locally a Torque UI tweak is a browser reload; in the container it rides the
backend image rebuild. Either way there is **no separate frontend deploy** for
Torque — it is not on Vercel.

---

## Troubleshooting

### "Undefined symbol" on order POST
v4.7.56+ handles the NDX/NDXP/SPX/SPXW family. If you see this on a non-index ticker, the chain may have returned a ghost listing (rare but possible). Open the console, look for `[Tradier root probe]` and `[Tradier submit] live response:` lines — they tell you exactly what Tradier rejected.

### `tp` errors with "config file has problem"
99% of the time it's an inline `#` comment inside an unquoted string. `tests/utils.py:load_config` strips comments only outside quoted values — if your config has `TRADIER_TOKEN=abc # prod`, the `# prod` makes it through. Quote the value or remove the comment.

### Tradier rate-limit errors
The browser auto-retries with exponential backoff. If you hit the limit repeatedly, you're probably running tests + UI clicks against the same token in parallel — Tradier sandbox is especially strict. Switch to production or space the calls out.

### Torque UI change didn't show up after deploy
You almost certainly ran `deploy-fe` (Vercel) instead of `deploy-be-restart`. Torque
is served by the **backend** image — rebuild it: `make deploy-be-restart`. In the
container the HTML is baked at build time, so a plain browser reload won't pull it
until the image is rebuilt.

---

## Production deployment

The market service ships two deployables (plus the optional legacy single-file page):

- **Backend** (`market/backend` — FastAPI API **and** the Torque order builder at
  `/torque`) runs as a **Docker container on a local PC**, exposed over HTTPS by a
  **Cloudflare *named* tunnel** sidecar on the permanent hostname
  **`edge.facades.trade`**. The hostname belongs to the tunnel, not the container, so
  restarts and host moves keep the same URL — both frontends simply bake it at deploy
  time from `EDGELANE_API_BASE`.
- **Frontends** — Matrix (`market/ui/index.html`) and Simmer (`simmer/ui/`) deploy
  to **Vercel**.

**Torque is part of the backend container, not the Vercel deploy.** `torque.html`
is `COPY`-baked into the image (`Dockerfile`: `COPY app ./app` + `COPY ui ./ui`) and
served by FastAPI at `/torque`. Compose bind-mounts only the config + data volume,
**not** the source — so a code change is picked up by an **image rebuild**, which is
exactly what `deploy-be-restart` does.

### What ships each change — quick map

| You changed | Deploy with | Why |
|---|---|---|
| `market/backend/ui/torque.html` (Torque UI) | **`make deploy-be-restart`** | HTML is baked into the backend image |
| `market/backend/app/**` (API, guardrails, engine) | **`make deploy-be-restart`** | Python is baked into the backend image |
| `market/ui/index.html` (Matrix) | **`make deploy-fe`** | that file goes to Vercel |
| `simmer/ui/**` (Simmer) | **`make deploy-simmer`** | separate Vercel project |
| both backend + dashboard | `make deploy-prod` | backend first, then Vercel |

> `make deploy-be-restart` builds from the **working tree** (Docker's build context is
> the filesystem, not git) — so **uncommitted** changes are baked in; you don't have to
> commit first. It also **does not flip `DEVMODE`** (unlike `run-dev`/`run-prod`): the
> container runs whatever mode/token is in `edgelane_market.config` at boot. Set that
> file to the intended environment **before** restarting.

### Targets (from repo root)

```bash
make doctor        # list ONLY missing build/deploy prerequisites (run this first)
make vercel-setup  # install Vercel CLI on Ubuntu (+ Node) and log in (browser)
make deploy-be     # backend → Docker container + Cloudflare tunnel (DEPLOY=local_container)
make deploy-fe     # market UI → Vercel
make deploy-prod   # both, backend first (+ db-push first)
make deploy-dry    # dry-run everything (prints commands, runs nothing)

# lifecycle / teardown (Docker on this host only — never touches Vercel)
make deploy-be-restart  # rebuild + recreate ONLY the backend (latest code), leaves cloudflared up
make deploy-be-down     # remove ONLY the backend container (tunnel stays; public URL 502s until back)
make deploy-down        # stop+remove the whole stack; KEEPS the DuckDB volume (ARGS=-v drops it)
make deploy-prune       # delete dangling images from rebuilds (ARGS=-a = all unused; machine-wide!)

# data migration (DuckDB volume → gitignored tarball)
make deploy-data-dump     # tar the volume → deploy/edgelane-data.tar.gz
make deploy-data-restore  # restore that tarball into the volume on a new host

# overrides
make deploy-be DEPLOY=cloud      # reserved future target (errors for now)
make deploy-prod ARGS=-n         # dry-run via ARGS passthrough
make deploy-prod ARGS=-y         # skip confirmation prompts
```

All targets delegate to `./deploy.sh` (`-b` / `-f` / `--target` / `-n` / `-y`).

> ⚠️ **Production flags — set these in `edgelane_market.config` before a public deploy:**
> - **`AUTH_ENABLED=true`** — if left `false`, **every gate is off**: no Supabase-JWT
>   enforcement, no anonymous-teaser redaction, no rate limiting → the API is wide
>   open to the internet. Must be `true` for any public deploy. (`make doctor` warns
>   if it isn't.)
> - **`DEVMODE=false`** — production Tradier + **market-hours-only polling** (09:30–16:00
>   ET). Off-hours you'll see no fresh snapshots; add `FORCE_POLL_WHEN_CLOSED=true`
>   temporarily only if you need data outside market hours for testing.

### Access to Torque in production (auth)

Torque trades the **server's single broker account**, so it's an admin/owner tool.
With `AUTH_ENABLED=true`, every data/action endpoint requires the **admin token**;
open the page with the token in the URL:

```
https://<backend-host>/torque?token=<ADMIN_API_TOKEN>
```

The page stashes it (per-tab `sessionStorage`) and sends `X-Admin-Token` on every
fetch. `ADMIN_API_TOKEN` lives in `edgelane_market.config`. With `AUTH_ENABLED=false`
(dev) the gate is a no-op. Being logged into the dashboard in another tab does **not**
carry over — Torque is a different origin (backend, not Vercel).

### Deploying from a new machine

```bash
make doctor          # lists what's missing (docker, node, vercel login, deploy/.env keys, …)
make vercel-setup    # if doctor flags Vercel: installs the CLI on Ubuntu + browser login
# copy the git-ignored secrets onto the machine: deploy/.env + edgelane_market.config
make deploy-dry      # final sanity (prints every command, runs nothing)
make deploy-prod     # go
```

`make doctor` prints only the **missing** items (silent on what's fine) and exits
non-zero if any required dependency is absent — so it's safe to gate CI/scripts on
it. Advisories like the `AUTH_ENABLED`/`DEVMODE` warnings don't fail it.

### One-time setup (production)

1. **Backend config** — `edgelane_market.config` with the **production** Tradier
   token and `DEVMODE=false` (deploy.sh warns if it's `true`). Bind-mounted
   read-only into the container at `/config/`; DuckDB persists in the
   `edgelane-data` volume.
2. **Cloudflare named tunnel** — create it once in the dashboard (Zero Trust →
   Networks → Tunnels → Create tunnel), add public hostname `edge.facades.trade`
   → `http://edgelane-backend:8789`, and put the connector token in `deploy/.env`
   as `CF_TUNNEL_TOKEN`. The DNS record is created for you.
3. **Vercel** — `npm i -g vercel && vercel login`. First `deploy-fe` links the
   repo to the `edgelane-matrix` Vercel project. Set
   `EDGELANE_API_BASE=https://edge.facades.trade` in `deploy/.env`.

**Backend URL wiring:** there is none at runtime. `EDGELANE_API_BASE` is baked into
both SPAs at deploy time as `window.__EDGELANE_API_BASE__`, and `deploy.sh` refuses
to deploy a frontend when it's blank. Explicit `?api=` / `localStorage` overrides
still win on Matrix (dev); local dev falls back to `127.0.0.1:8789`. On Simmer a
baked base is authoritative and cannot be overridden — see the SECURITY note in
`simmer/ui/src/lib/api.ts`.

### Migrating the backend to another machine

The tunnel hostname is permanent and belongs to the Cloudflare tunnel, not to the
host running it. So moving the backend needs **no Vercel redeploy** — bring the new
host up with the same `CF_TUNNEL_TOKEN` and `edge.facades.trade` follows it.

`make setup` is **not** needed on the new host — that builds a native Python venv
for `make run`. The container deploy builds its own deps inside the image, so the
new host needs only Docker + the two git-ignored config files.

```bash
# ── on the OLD machine ──
make deploy-data-dump                      # → deploy/edgelane-data.tar.gz (DuckDB history)
scp deploy/edgelane-data.tar.gz \
    deploy/.env edgelane_market.config \
    user@newhost:/path/to/EdgeLane/<dest>  # carry the gitignored secrets + data

# ── on the NEW machine (repo cloned, Docker installed) ──
# place deploy/.env + edgelane_market.config + deploy/edgelane-data.tar.gz
make deploy-data-restore   # load history into the volume BEFORE first boot
make deploy-be             # build + start; the tunnel reattaches on the same hostname

# ── back on the OLD machine, once the new one is serving ──
make deploy-down           # stop backend + cloudflared here
```

> ⚠️ **Don't leave both machines' cloudflared running.** Two connectors on the same
> tunnel token both register as healthy origins, and Cloudflare will load-balance
> between them — so requests land on whichever host answers, including the one you're
> abandoning (stale DuckDB, possibly stale code). Exactly **one** stack should be up.
> `make deploy-down` only touches Docker on that host — the Vercel site stays live.

> **DuckDB history doesn't move on its own.** It lives in the `edgelane_edgelane-data`
> named volume, local to each host; `make deploy-down` keeps it *on that host*. Skip
> `deploy-data-dump`/`restore` and the new machine simply starts with a fresh DB
> (the app works; only bias-accuracy history resets). The tarball is git-ignored
> (`deploy/*.tar.gz`).

### Database admin (data, not schema)

`db-push` applies **schema** migrations. To inspect or edit **data**, use the
standalone `tools/supabase_admin.py` utility — a no-SQL CRUD CLI over the same
Supabase Management API query endpoint (no psql, stdlib only, reads `deploy/.env`).
It runs with elevated rights (RLS bypassed) — operator-only.

```bash
python3 tools/supabase_admin.py tables                          # every table: row count + last-updated
python3 tools/supabase_admin.py describe broker_configs         # columns, types, PK
python3 tools/supabase_admin.py list profiles --where plan=pro --order created_at:desc
python3 tools/supabase_admin.py update profiles --set plan=pro --where email~gmail -y
python3 tools/supabase_admin.py delete user_settings --where user_id=<uuid> -y
python3 tools/supabase_admin.py sql "select count(*) from auth.users"   # escape hatch
python3 tools/supabase_admin.py --help                          # full help
```

Output: a bordered table that **wraps** long cell values to fit the terminal (never
overflows). Tables with too many columns to fit a grid (e.g. `list auth.users`)
auto-switch to a record view (psql `\x` style, one field per line); force it with
`-x/--expanded`. `--wide` keeps full untrimmed widths; `--json` emits raw JSON.

Safety: `--dry-run/-n` prints the SQL and runs nothing; `update`/`delete` **refuse**
without a `--where` (override with `--all`); writes prompt unless `-y` (non-TTY
shells must pass `-y`). Global flags work before or after the subcommand. Value
syntax: `col=val` auto-types (numbers/bool/null/jsonb), `col=s:val` forces text,
`col=r:expr` is raw SQL (e.g. `updated_at=r:now()`); `--where` ops are
`= != > < >= <= ~` (`~` = ILIKE substring).

**Cascade-impact preview.** Referential integrity is enforced by the DB, not the
tool — every FK to `auth.users` is `ON DELETE CASCADE`, and nothing references the
`public` tables (cascade depth 1). Before any `delete`, the tool prints a preview of
exactly what cascades — recursively — e.g.:

```
$ python3 tools/supabase_admin.py --dry-run delete auth.users --where id=<uuid>
cascade — will ALSO delete:
  └─ auth.identities: delete 1
  └─ auth.sessions: delete 2
    └─ auth.refresh_tokens: delete 2
  └─ public.profiles: delete 1
  └─ public.user_settings: delete 1
```

It also flags `SET NULL`/`SET DEFAULT` refs (row kept, column nulled) and any
`RESTRICT`/`NO ACTION` refs that would **block** the delete. `--no-cascade` skips
the preview. To delete a user, delete the **parent** (`auth.users` — cascades
clean), not the child rows piecemeal.

### The named tunnel (done — was a quick tunnel)

The backend used to sit behind a free Cloudflare **quick** tunnel, whose
`*.trycloudflare.com` hostname changed on every container restart. That forced a
whole runtime-discovery layer: a publisher sidecar wrote the current URL to Supabase
`app_config.api_base` and Vercel Edge Config, and both SPAs fetched a pointer at
boot (and re-read it mid-session on a failed call).

That is all gone. The backend now runs behind a **named tunnel** on the permanent
hostname `edge.facades.trade`, and the URL is baked at deploy time.

| | Before (quick tunnel) | Now (named tunnel) |
|---|---|---|
| Hostname | `*.trycloudflare.com`, rotates each restart | `edge.facades.trade`, permanent |
| cloudflared image | custom build, `deploy/cloudflared/` | stock `cloudflare/cloudflared` |
| Frontend URL | fetched at boot from a pointer | baked `window.__EDGELANE_API_BASE__` |
| Matrix pointer | Supabase `app_config.api_base` | *removed* |
| Simmer pointer | `/api/config` → Vercel Edge Config | *removed* |
| On restart | publish new URL, frontends self-heal | nothing — same URL |

**What this means day to day:** restarting or rebuilding the backend no longer
touches the frontends, and moving the backend to another machine needs no Vercel
redeploy — just bring the new host up with the same `CF_TUNNEL_TOKEN`.

**Leftovers, inert and safe to drop whenever:** the Supabase `app_config` table
(migration `0005`) and the Vercel Edge Config store `edgelane-api-base` are no
longer read or written by anything. Drop them only *after* both frontends are
redeployed with a baked base — an older deployed build still reads its pointer.

**If you ever change the hostname:** update the tunnel's public hostname in
Cloudflare, set the new `EDGELANE_API_BASE` in `deploy/.env`, then redeploy **both**
frontends (`make deploy-fe` + `make deploy-simmer`). There is no runtime fallback to
catch a mismatch — `deploy.sh` fails fast on a blank base, but it cannot know a
non-blank one is wrong.

## Quick reference card

```bash
# ─── everyday loop ───
cd market/backend && make run-dev          # backend on :8789 (sandbox)
make ui                                    # open Matrix against it
# Torque → http://127.0.0.1:8789/torque

# ─── before commit ───
cd market/backend && make test             # 56 parity tests
python3 tests/tradier_smoke.py SPY         # provider-side sanity (live token)
cd simmer/ui && npm run check && npx vitest run   # if you touched Simmer

# ─── market backend + Torque (local) ───
cd market/backend
make run-dev          # sandbox (paper) on :8789  — open /torque
make run-prod         # production (real money)
# torque.html edit → just reload the tab; python edit → restart the process

# ─── ship to production (from repo root) ───
make doctor                                # prerequisites check
make deploy-be-restart                     # backend + Torque UI (image rebuild)
make deploy-fe                             # Matrix → Vercel
make deploy-simmer                         # Simmer → Vercel
make deploy-prod                           # backend + Matrix, backend first
make check-tunnel                          # end-to-end health via edge.facades.trade

# ─── tools ───
tp                                         # list positions
tp -O                                      # list working orders
tp --open '<ticket>' --execute             # place order from a copy-button ticket
python3 tools/supabase_admin.py tables     # prod data admin
```
