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
8. [CORS proxy — what it does and how to manage it](#cors-proxy--what-it-does-and-how-to-manage-it)
9. [Build — `edge_lane_build.sh`](#build--edge_lane_buildsh)
10. [Local run + browser](#local-run--browser)
11. [Tools: `tp` and `coproxy` shell aliases](#tools-tp-and-coproxy-shell-aliases)
12. [Test scripts — what to run before committing](#test-scripts--what-to-run-before-committing)
13. [Daily workflow](#daily-workflow)
14. [Switching between sandbox and production](#switching-between-sandbox-and-production)
15. [Market backend & Torque (local run)](#market-backend--torque-local-run)
16. [Troubleshooting](#troubleshooting)
17. [Production deployment](#production-deployment)
18. [Quick reference card](#quick-reference-card)

---

## What EdgeLane is, briefly

A hybrid options-spread optimizer:

- Pulls live options chain + Greeks from Tradier (EdgeLane originally used Atlas's API (mind-vest.io) as its data provider; Atlas was fully retired ~May 2026 and Tradier is now the sole provider)
- Computes a deterministic market-bias signal in JavaScript (Gemini Flash only writes the prose summary, not the bias decision)
- Scores candidate spreads (verticals, condors, butterflies, iron flies)
- Lets the user sign in, attach a Tradier brokerage connection, copy a ticket to clipboard, or push an order straight to the broker
- A separate Python toolchain (`tp` and friends) covers everything the UI doesn't: listing / closing / modifying / cancelling live positions and working orders.

The legacy front-end ships as one `edge_lane.html` produced by `edge_lane_build.sh` —
no bundler, no Node runtime, no install step; Babel-standalone compiles the JSX in
the browser. The current product surface is the **market backend** (FastAPI) plus its
two UIs: the **market dashboard** (Vercel) and **Torque** (served by the backend).

---

## The three deployables

EdgeLane has **three** shippable pieces. Knowing which serves what is the difference
between a one-command deploy and chasing a change that never went live:

| Deployable | Source | Served from | Shipped by |
|---|---|---|---|
| **Market backend API + Torque page** | `market/backend/` (`app/` + `ui/torque.html`) | **Docker container** (FastAPI, behind a Cloudflare tunnel) | `make deploy-be` / `make deploy-be-restart` |
| **Market dashboard UI** | `market/ui/index.html` | **Vercel** | `make deploy-fe` |
| **Legacy single-file optimizer** | `edge_lane.html` (built from JSX) | *not currently deployed* (optional Cloudflare Pages) | manual `wrangler pages deploy` |

> ⚠️ **Torque is part of the backend, not Vercel.** `market/backend/ui/torque.html`
> is `COPY`-baked into the backend Docker image (`Dockerfile`: `COPY ui ./ui`) and
> served by FastAPI at `GET /torque`. So a Torque UI change ships with
> **`make deploy-be-restart`** (which rebuilds the image), **not** `make deploy-fe`.
> `deploy-fe` only pushes `market/ui/index.html` to Vercel. See
> [Production deployment](#production-deployment) for the full serving model.

---

## Architecture in one diagram

```
                 ┌─────────────────────────────┐
                 │  edge_lane.html  (browser)  │   ← legacy single-file optimizer
                 └────┬───────────────────┬────┘
                      │                   │
        market data ──┤                   ├── per-user broker orders
        (operator)    │                   │   (user's Tradier connection)
                      ▼                   ▼
        ┌─────────────────────┐   ┌─────────────────────┐
        │ CORS proxy :8787    │   │ Tradier api endpoint│
        │ tools/cors_proxy.py │   │ (sandbox or prod)   │
        └────┬──────────┬─────┘   └─────────────────────┘
             │          │
             │          └──→ Gemini  (prose only)
             └────────────→ Tradier  (chain, GEX, quote — operator-level)

Market service (current product):
        ┌──────────────────────────────┐        ┌─────────────────────┐
        │ market dashboard index.html  │───────▶│ market backend      │
        │ (Vercel)                     │  api   │ FastAPI, Docker      │
        └──────────────────────────────┘        │  + Torque /torque    │
                                                 │  behind CF tunnel    │
        ┌──────────────────────────────┐  same  └─────────────────────┘
        │ Torque /torque (backend-served) │◀── origin (COPY-baked into image)

Python side (no browser involved):
    tradier_smoke.py            chain/quote sanity check
    tradier_execute_ticket.py   parse EdgeLane copy-button string + submit
    tools/tradier_positions.py  list / sell / open / modify / cancel positions
```

Bias decision is **always deterministic** — Gemini only writes the human-readable
summary. Same chain inputs always produce the same trade pick. Torque itself does
**no** forecasting (no bias engine) — just positioning/flow reads + order building.

---

## File layout

### Frontend / local-dev

| Path | Role | Commit? |
|---|---|---|
| `spread_optimizer_v4_7_html.jsx` | Source of truth. All optimizer logic. Edit this. | ✓ |
| `edge_lane.template.html` | HTML shell — CDN imports, theme CSS, placeholders. | ✓ |
| `edge_lane_build.sh` | Build script. Auto-bumps version. | ✓ |
| `edge_lane_config.config.example` | Template config with placeholders. | ✓ |
| `edge_lane_config.config` | Your real keys + DEVMODE. | ✗ (gitignored) |
| `edge_lane.html` | Build artifact. Overwritten every build. | ✗ |
| `.edge_lane_buildstate` | Version-bump state. | ✗ |
| `tools/cors_proxy.py` | Local CORS proxy (browser → Tradier + Gemini). | ✓ |
| `tools/cors_proxy_service.sh` | Service manager: start/stop/status/install. | ✓ |
| `tools/tradier_positions.py` | `tp` — Tradier position + order manager. | ✓ |
| `tools/tp_operating_manual.html` | Operating manual for `tp` (HTML). | ✓ |
| `tests/tradier_smoke.py` | Tradier API sanity check. | ✓ |
| `tests/tradier_execute_ticket.py` | Parses EdgeLane copy-button ticket, posts via Tradier. | ✓ |
| `tests/utils.py` | Shared test helpers (config loader, etc.). | ✓ |
| `archive/` | Snapshots of prior JSX versions. | ✓ |
| `docs/` | Implementation notes (`torque.md`, `torque_operating_manual.html`, `tradier_implementation.md`, etc.). | ✓ |
| `operating_manual.md` | Badge / verdict / score / strategy reference for the UI. | ✓ |
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
| WSL2 with Ubuntu (or any Linux) | Build script + proxy + tests run there | Windows users: use WSL, not Git Bash — the proxy is long-lived |
| Python 3.10+ | Build script + proxy + all CLI tools | stdlib only; **no `pip install` required** for the frontend/tools |
| Bash | Build script + service manager | comes with WSL/Linux |
| A browser | Open `edge_lane.html` / `/torque` | Chromium-family preferred for DevTools depth |
| Node.js | Optional (JSX validation); required for Vercel deploy | `@babel/parser` syntax-check; `vercel` CLI |
| Docker | Production backend (container + tunnel) | only needed to `deploy-be` |
| `curl`, `ss` | Optional, for proxy health checks | universally available |

---

## Initial setup (one-time)

```bash
# 1. Enter the repo
cd /mnt/c/soljet_dev/EdgeLane

# 2. Create your config from the example
cp edge_lane_config.config.example edge_lane_config.config

# 3. Open it and fill in your keys — see the next section for what each does
$EDITOR edge_lane_config.config

# 4. Start the CORS proxy as a background service (see CORS proxy section)
./tools/cors_proxy_service.sh start

# 5. Build the HTML
./edge_lane_build.sh

# 6. Serve it
python3 -m http.server 8080
# open http://localhost:8080/edge_lane.html
```

That's the whole loop. Everything below is detail and tooling around those six commands.

---

## Configuration reference

`edge_lane_config.config` is the single source of secrets + environment flags for the
**frontend/tools**. The build script `source`s it as bash, so values must use bash
quoting rules. (The **market backend / Torque** use a *separate* file,
`edgelane_market.config` — see [Market backend & Torque](#market-backend--torque-local-run).)

### Required keys

| Key | What it's for |
|---|---|
| `GEMINI_API_KEY` | Bias prose generation (deterministic JS does the math; Gemini just narrates) |
| `TRADIER_TOKEN` | Operator-level Tradier token (chain + GEX + quote) |
| `TRADIER_TOKEN_SANDBOX` | Sandbox-environment Tradier token (used when `DEVMODE=true`) |

### Environment toggles

| Key | Values | Effect |
|---|---|---|
| `DEVMODE` | `true` / `false` | When `true`, Python CLI tools (`tp`, `tradier_smoke.py`, etc.) target Tradier sandbox; when `false`, they target production. The browser's data provider env is set by `TRADIER_ENV` below, independently. |
| `DATA_PROVIDER` | `tradier` | Data provider EdgeLane uses for chain + GEX in the browser (Tradier is the only supported value) |
| `TRADIER_ENV` | `production` / `sandbox` | Browser-side env when `DATA_PROVIDER=tradier` |
| `EDGE_LANE_VERSION` | e.g. `4.7` | Major.minor base — build script auto-appends `.<patch>` per content change |

### Local-dev URLs

When the CORS proxy is running locally, point both Tradier and Gemini at it so the browser doesn't hit CORS walls:

```
TRADIER_BASE_URL="http://localhost:8787/tradier"
GEMINI_BASE_URL="http://localhost:8787/gemini"
```

In production these point at server-side proxies/functions instead (see [Production deployment](#production-deployment)).

### Example

```bash
# edge_lane_config.config
GEMINI_API_KEY="AIzaXXXX"
GEMINI_MODEL=gemini-2.5-flash

TRADIER_TOKEN="prod-token-xxxx"
TRADIER_TOKEN_SANDBOX="sb-token-xxxx"
DEVMODE=false                     # python CLI tools → production
DATA_PROVIDER=tradier             # browser-side data provider
TRADIER_ENV=production            # browser-side env

TRADIER_BASE_URL="http://localhost:8787/tradier"
GEMINI_BASE_URL="http://localhost:8787/gemini"

EDGE_LANE_VERSION="4.7"
```

> ⚠️ Don't edit-paste broken quotes into this file. Bash refuses to source any file with an unclosed `"`, and the build will fail with `GEMINI_API_KEY missing` or similar — even though the key is right there. If the build complains a key is missing, re-paste with proper closing quotes.

---

## CORS proxy — what it does and how to manage it

Tradier and Gemini don't return CORS headers, so the browser can't call them from a `file://` or `localhost` page directly. The proxy at `tools/cors_proxy.py` listens on `127.0.0.1:8787` and adds those headers while forwarding requests upstream. Keys are passed through from the browser's headers; the proxy never stores them.

### Routes

```
/tradier/<rest>  →  https://api.tradier.com/<rest>
/gemini/<rest>   →  https://generativelanguage.googleapis.com/v1beta/<rest>
```

### Service manager — `tools/cors_proxy_service.sh`

Don't run `cors_proxy.py` by hand. Use the manager — it handles PID tracking, untracked-process detection, and integrates with systemd for auto-start.

```bash
./tools/cors_proxy_service.sh start      # bring it up
./tools/cors_proxy_service.sh status     # show running state + health probe
./tools/cors_proxy_service.sh restart    # stop + start
./tools/cors_proxy_service.sh stop       # shut down
./tools/cors_proxy_service.sh logs       # tail -f the log file
./tools/cors_proxy_service.sh install    # auto-start on WSL boot
./tools/cors_proxy_service.sh uninstall  # remove auto-start hook
./tools/cors_proxy_service.sh help
```

State lives in `~/.edgelane/` (PID file + log). Port + bind are configurable:

```bash
EDGELANE_CORS_PORT=9000 EDGELANE_CORS_BIND=0.0.0.0 ./tools/cors_proxy_service.sh start
```

The script path is long — add the `coproxy` alias so you can type `coproxy start/status/install/...` instead. See [Tools: `tp` and `coproxy` shell aliases](#tools-tp-and-coproxy-shell-aliases).

### Auto-start on WSL boot

```bash
./tools/cors_proxy_service.sh install
```

- If your WSL has systemd enabled (`systemd=true` in `/etc/wsl.conf` — the modern Windows 11 default), this writes a user-level systemd unit at `~/.config/systemd/user/edgelane-cors-proxy.service`, enables it, and starts it.
- To keep it running without an active login, run once: `sudo loginctl enable-linger $USER`
- If systemd isn't available, the script falls back to adding a `boot.command` line to `/etc/wsl.conf`.

After install you can use **either** `coproxy start/stop/...` (the script delegates to systemd if the unit is present) or `systemctl --user {start,stop,restart,status} edgelane-cors-proxy` directly.

### Health check

```bash
./tools/cors_proxy_service.sh status
# ● running (systemd)
#   unit    : edgelane-cors-proxy.service
#   pid     : 24
#   listen  : 127.0.0.1:8787
#   manage  : systemctl --user {status,stop,restart} edgelane-cors-proxy
#   logs    : journalctl --user -u edgelane-cors-proxy -f
```

Or with curl:

```bash
curl -i -X OPTIONS http://localhost:8787/gemini/models \
  -H 'Access-Control-Request-Method: POST'
# expect: HTTP/1.0 204 No Content + Access-Control-Allow-Origin: *
```

---

## Build — `edge_lane_build.sh`

The build script does four things:

1. Sources `edge_lane_config.config` for keys + URLs + version
2. Reads `spread_optimizer_v4_7_html.jsx` and transforms it for browser use (strips ESM imports, drops `export default`)
3. Wraps the transformed JSX inside `edge_lane.template.html`, substituting `__GEMINI_API_KEY__`, `__TRADIER_TOKEN__`, `__DATA_PROVIDER__`, `__VERSION__`, base URLs, etc.
4. Writes the final `edge_lane.html` and updates `.edge_lane_buildstate` so the patch number auto-bumps on content change.

### Basic usage

```bash
./edge_lane_build.sh
# ✓ wrote edge_lane.html  (5290 lines, 273275 bytes)
#   version: v4.7.56  (content change (hash X → Y))
#   provider: tradier (production - https://api.tradier.com)
```

The version line tells you exactly why the patch did (or didn't) bump:

| Reason | Means |
|---|---|
| `first build` | No `.edge_lane_buildstate` yet — patch starts at 1 |
| `no content change` | JSX + template hashes unchanged — patch stays |
| `content change (hash A → B)` | Hashes differ — patch +1 |
| `base change (4.7 → 4.8)` | You edited `EDGE_LANE_VERSION` in config — patch resets to 1 |

Force a reset by deleting `.edge_lane_buildstate`.

### Flags

```bash
./edge_lane_build.sh --help                       # full flag list
./edge_lane_build.sh --dry-run                    # checks + planned substitutions, write nothing
./edge_lane_build.sh -n                           # write to edge_lane_v{VERSION}.html instead of overwriting
./edge_lane_build.sh --config edge_lane_config.production.config
./edge_lane_build.sh --jsx spread_optimizer_v4_5.jsx --out edge_lane_v4_5.html
```

### Optional pre-flight: JSX parse check

Before building, you can syntax-check the JSX with Babel:

```bash
node -e "
  const fs = require('fs'); const p = require('@babel/parser');
  try { p.parse(fs.readFileSync('spread_optimizer_v4_7_html.jsx','utf8'),
                { sourceType:'module', plugins:['jsx'] });
        console.log('OK'); }
  catch(e){ console.error('PARSE ERROR line', e.loc?.line, e.message); process.exit(1); }
"
```

(Install once with `cd /tmp && npm install @babel/parser`.)

---

## Local run + browser

```bash
python3 -m http.server 8080
# then open http://localhost:8080/edge_lane.html
```

Hard-reload (Ctrl+Shift+R) after every rebuild — browsers cache aggressively. Open DevTools (F12) on first run so you can see Console errors immediately.

For local development you can also just open `file:///C:/soljet_dev/EdgeLane/edge_lane.html` directly — works for most things, but a few features (clipboard, certain fetch behaviors) require a real HTTP origin.

---

## Tools: `tp` and `coproxy` shell aliases

The two CLI tools are long-named. Add aliases to your shell to save typing:

```bash
# Append to ~/.bashrc
cat >> ~/.bashrc <<'EOF'
alias tp='python3 /mnt/c/soljet_dev/EdgeLane/tools/tradier_positions.py'
alias coproxy='/mnt/c/soljet_dev/EdgeLane/tools/cors_proxy_service.sh'
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

### `coproxy` — CORS proxy service

See [CORS proxy](#cors-proxy--what-it-does-and-how-to-manage-it) above.

```bash
coproxy start | stop | restart | status | logs | install | uninstall
```

---

## Test scripts — what to run before committing

All frontend tests are stdlib-only, load keys from `edge_lane_config.config`, and run from the repo root. Run them in this order when you've touched anything that touches the data path:

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
coproxy status                        # confirm proxy is up; if not: coproxy start

# ── After every JSX edit ──
./edge_lane_build.sh                  # patch auto-bumps if content changed
# (browser tab: Ctrl+Shift+R to hard-refresh)

# ── Before committing ──
python3 tests/tradier_smoke.py SPY
./edge_lane_build.sh --dry-run        # confirm substitutions are clean
# (also run any of the test scripts touching the path you changed)
```

If you broke the parse (truncated JSX, unbalanced braces, etc.), the build script will refuse and tell you which line. Restore from `archive/` if needed — every shipped version is snapshotted there.

---

## Switching between sandbox and production

For the **frontend / CLI tools**, two independent env switches:

| Switch | Affects |
|---|---|
| `DEVMODE` in `edge_lane_config.config` | Python CLI tools (`tp`, `tradier_smoke.py`, `tradier_execute_ticket.py`). Selects which token (`TRADIER_TOKEN` vs `TRADIER_TOKEN_SANDBOX`) and which base URL. |
| `TRADIER_ENV` in `edge_lane_config.config` | The browser's chain/GEX/quote data provider. Set at build time. Rebuild after changing. |
| Per-user broker connection (Settings UI) | The browser's **order submission** path. Each user adds their own Tradier connection, which carries its own env field. |

**Common gotcha**: the operator-level browser token can be production while a user's broker connection is sandbox. The canonical-root probe in v4.7.54+ uses the broker connection's env when present, so order POSTs always go to the same env as the chain lookup. But chain rendering on screen still comes from the operator-level token. If that mismatches, the candidate strikes you see may not match the strikes available on your account.

For the **market backend / Torque**, the switch is `DEVMODE` in the *separate*
`edgelane_market.config` — see the next section.

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
  This flag is **separate** from the frontend's `edge_lane_config.config`.
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

### Bias detection fails / "Failed to fetch" in console
Almost always the CORS proxy isn't running. Run:
```bash
coproxy status
coproxy start   # if it says stopped
```

### Bias detection fails with a specific HTTP error in console
Read the `[label]: HTTP <code> — <body>` message. Most common:
- `GEMINI_API_KEY missing` → config got truncated; re-paste keys with closing quotes
- `HTTP 429` → Gemini quota; wait or switch to `gemini-2.5-flash-lite`
- `HTTP 400 — invalid response_schema` → Google tightened schema validation; check `_BIAS_PROSE_SCHEMA` in JSX

### "Undefined symbol" on order POST
v4.7.56+ handles the NDX/NDXP/SPX/SPXW family. If you see this on a non-index ticker, the chain may have returned a ghost listing (rare but possible). Open the console, look for `[Tradier root probe]` and `[Tradier submit] live response:` lines — they tell you exactly what Tradier rejected.

### "Tradier …: blocked by CORS"
Same root cause as bias detection: proxy down. `coproxy start`.

### Babel error in browser console
JSX got truncated mid-token (file tool truncation, bad merge, etc.).
```bash
wc -l spread_optimizer_v4_7_html.jsx
# should be ~5000 lines for v4.7.x
```
If short, restore from `archive/` and reapply your changes.

### Version doesn't bump on rebuild
- `no content change` reason → expected, JSX + template hashes are identical
- Force reset: `rm .edge_lane_buildstate && ./edge_lane_build.sh`

### `tp` errors with "config file has problem"
99% of the time it's an inline `#` comment inside an unquoted string. `tests/utils.py:load_config` strips comments only outside quoted values — if your config has `TRADIER_TOKEN=abc # prod`, the `# prod` makes it through. Quote the value or remove the comment.

### Tradier rate-limit errors
The browser auto-retries with exponential backoff. If you hit the limit repeatedly, you're probably running tests + UI clicks against the same token in parallel — Tradier sandbox is especially strict. Switch to production or space the calls out.

### Proxy says "● running (UNTRACKED)"
Something else started `cors_proxy.py` outside the service manager. `coproxy stop` will find it and kill it; then `coproxy start` to relaunch cleanly.

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
- **Frontend** (`market/ui/index.html` — the market dashboard, *not* the legacy
  `edge_lane.html`) deploys to **Vercel**.

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
| `market/ui/index.html` (dashboard) | **`make deploy-fe`** | that file goes to Vercel |
| both backend + dashboard | `make deploy-prod` | backend first, then Vercel |
| `edge_lane.html` (legacy) | manual `wrangler pages deploy` | not part of the market deploy |

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

### Legacy: shipping `edge_lane.html` on Cloudflare Pages

The single-file optimizer is **not** part of the market deploy and is not currently
hosted. If you ever need to ship it standalone, it's one static file — two paths:

- **Path A — private deploy (keys baked in).** For an access-gated page (Cloudflare
  Access, basic auth, IP allowlist):
  ```bash
  npm install -g wrangler && wrangler login
  ./edge_lane_build.sh && wrangler pages deploy . --project-name=edgelane
  ```
  Keys live in the deployed HTML — fine when access is controlled.
- **Path B — public deploy (keys server-side).** Add a Cloudflare Pages Function
  `functions/api/[[path]].js` that proxies `/api/tradier/*` and `/api/gemini/*` to the
  upstreams, attaching `TRADIER_TOKEN` / `GEMINI_API_KEY` from Pages **environment
  variables**; then build with **empty** browser-side keys and
  `TRADIER_BASE_URL="/api/tradier"` / `GEMINI_BASE_URL="/api/gemini"` so the browser
  hits the same origin and the keys never leave Cloudflare. (Historical recipe — key
  names in the build config may differ from the current `edge_lane_config.config`.)

---

## Quick reference card

```bash
# ─── everyday loop (frontend) ───
coproxy start                              # CORS proxy
./edge_lane_build.sh                       # rebuild after JSX edits
python3 -m http.server 8080                # serve
# open http://localhost:8080/edge_lane.html  (Ctrl+Shift+R after rebuild)

# ─── before commit ───
python3 tests/tradier_smoke.py SPY
./edge_lane_build.sh --dry-run
cd market/backend && make test             # if you touched the backend

# ─── market backend + Torque (local) ───
cd market/backend
make run-dev          # sandbox (paper) on :8789  — open /torque
make run-prod         # production (real money)
# torque.html edit → just reload the tab; python edit → restart the process

# ─── ship to production (from repo root) ───
make doctor                                # prerequisites check
make deploy-be-restart                     # backend + Torque UI (image rebuild)
make deploy-fe                             # market dashboard → Vercel
make deploy-prod                           # both, backend first

# ─── tools ───
tp                                         # list positions
tp -O                                      # list working orders
tp --open '<ticket>' --execute             # place order from copy-button ticket
coproxy status | install                   # proxy health / autostart
python3 tools/supabase_admin.py tables     # prod data admin
```
