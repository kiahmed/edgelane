# EdgeLane — Deployment & Operations Manual

This is the consolidated guide for getting EdgeLane running on a new workstation, building it after code changes, and validating it works end-to-end. Production deployment is the last section and currently a placeholder while we finalize hosting.

> Supersedes the older `deployment.md` (which predates Tradier provider, auth module, broker connections, CORS proxy service, and the `tp` / `coproxy` CLI tools). The old file is kept for historical reference; new readers should use this manual.

---

## Contents

1. [What EdgeLane is, briefly](#what-edgelane-is-briefly)
2. [Architecture in one diagram](#architecture-in-one-diagram)
3. [File layout](#file-layout)
4. [Prerequisites](#prerequisites)
5. [Initial setup (one-time)](#initial-setup-one-time)
6. [Configuration reference](#configuration-reference)
7. [CORS proxy — what it does and how to manage it](#cors-proxy--what-it-does-and-how-to-manage-it)
8. [Build — `edge_lane_build.sh`](#build--edge_lane_buildsh)
9. [Local run + browser](#local-run--browser)
10. [Tools: `tp` and `coproxy` shell aliases](#tools-tp-and-coproxy-shell-aliases)
11. [Test scripts — what to run before committing](#test-scripts--what-to-run-before-committing)
12. [Daily workflow](#daily-workflow)
13. [Switching between sandbox and production](#switching-between-sandbox-and-production)
14. [Troubleshooting](#troubleshooting)
15. [Production deployment](#production-deployment) *(placeholder)*

---

## What EdgeLane is, briefly

A single-file HTML web app that:

- Pulls live options chain + Greeks from a data provider (Atlas or Tradier)
- Computes a deterministic market-bias signal in JavaScript (Gemini Flash only writes the prose summary, not the bias decision)
- Scores candidate spreads (verticals, condors, butterflies, iron flies)
- Lets the user sign in, attach a Tradier brokerage connection, copy a ticket to clipboard, or push an order straight to the broker
- A separate Python toolchain (`tp` and friends) covers everything the UI doesn't: listing / closing / modifying / cancelling live positions and working orders.

The whole front-end ships as one `edge_lane.html` produced by `edge_lane_build.sh`. No bundler, no Node runtime, no install step — Babel-standalone compiles the JSX in the browser.

---

## Architecture in one diagram

```
                 ┌─────────────────────────────┐
                 │  edge_lane.html  (browser)  │
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
             └────────────→ Atlas    (chain, GEX, quote)  ── or ──
                            Tradier   (chain, GEX, quote — operator-level)

Python side (no browser involved):
    tradier_smoke.py            chain/quote sanity check
    tradier_execute_ticket.py   parse EdgeLane copy-button string + submit
    tools/tradier_positions.py  list / sell / open / modify / cancel positions
```

Bias decision is **always deterministic** — Gemini only writes the human-readable summary. Same chain inputs always produce the same trade pick.

---

## File layout

| Path | Role | Commit? |
|---|---|---|
| `spread_optimizer_v4_7_html.jsx` | Source of truth. All optimizer logic. Edit this. | ✓ |
| `edge_lane.template.html` | HTML shell — CDN imports, theme CSS, placeholders. | ✓ |
| `edge_lane_build.sh` | Build script. Auto-bumps version. | ✓ |
| `edge_lane_config.config.example` | Template config with placeholders. | ✓ |
| `edge_lane_config.config` | Your real keys + DEVMODE. | ✗ (gitignored) |
| `edge_lane.html` | Build artifact. Overwritten every build. | ✗ |
| `.edge_lane_buildstate` | Version-bump state. | ✗ |
| `tools/cors_proxy.py` | Local CORS proxy (browser → Atlas + Gemini). | ✓ |
| `tools/cors_proxy_service.sh` | Service manager: start/stop/status/install. | ✓ |
| `tools/tradier_positions.py` | `tp` — Tradier position + order manager. | ✓ |
| `tools/tp_operating_manual.html` | Operating manual for `tp` (HTML). | ✓ |
| `tests/tradier_smoke.py` | Tradier API sanity check. | ✓ |
| `tests/tradier_execute_ticket.py` | Parses EdgeLane copy-button ticket, posts via Tradier. | ✓ |
| `tests/atlas_rest_smoke.py` | Atlas REST sanity check. | ✓ |
| `tests/utils.py` | Shared test helpers (config loader, etc.). | ✓ |
| `archive/` | Snapshots of prior JSX versions. | ✓ |
| `docs/` | Implementation notes (tradier_implementation.md, etc.). | ✓ |
| `operating_manual.md` | Badge / verdict / score / strategy reference for the UI. | ✓ |
| `deployment_manual.md` | This file. | ✓ |

---

## Prerequisites

| What | Why | Notes |
|---|---|---|
| WSL2 with Ubuntu (or any Linux) | Build script + proxy + tests run there | Windows users: use WSL, not Git Bash — the proxy is long-lived |
| Python 3.10+ | Build script + proxy + all CLI tools | stdlib only; **no `pip install` required** |
| Bash | Build script + service manager | comes with WSL/Linux |
| A browser | Open `edge_lane.html` | Chromium-family preferred for DevTools depth |
| Node.js | Optional, for JSX parse validation | only used by `@babel/parser` to syntax-check JSX before building |
| `curl`, `ss` | Optional, for proxy health checks | universally available; manager script degrades gracefully without |

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

`edge_lane_config.config` is the single source of secrets + environment flags. The build script `source`s it as bash, so values must use bash quoting rules.

### Required keys

| Key | What it's for |
|---|---|
| `ATLAS_KEY` | Operator-level Atlas account (chain + GEX provider when `DATA_PROVIDER=atlas`) |
| `GEMINI_API_KEY` | Bias prose generation (deterministic JS does the math; Gemini just narrates) |
| `TRADIER_TOKEN` | Operator-level Tradier token (chain + GEX + quote when `DATA_PROVIDER=tradier`) |
| `TRADIER_TOKEN_SANDBOX` | Sandbox-environment Tradier token (used when `DEVMODE=true`) |

### Environment toggles

| Key | Values | Effect |
|---|---|---|
| `DEVMODE` | `true` / `false` | When `true`, Python CLI tools (`tp`, `tradier_smoke.py`, etc.) target Tradier sandbox; when `false`, they target production. The browser's data provider env is set by `TRADIER_ENV` below, independently. |
| `DATA_PROVIDER` | `atlas` / `tradier` | Which provider EdgeLane uses for chain + GEX in the browser |
| `TRADIER_ENV` | `production` / `sandbox` | Browser-side env when `DATA_PROVIDER=tradier` |
| `EDGE_LANE_VERSION` | e.g. `4.7` | Major.minor base — build script auto-appends `.<patch>` per content change |

### Local-dev URLs

When the CORS proxy is running locally, point both Atlas and Gemini at it so the browser doesn't hit CORS walls:

```
ATLAS_BASE_URL="http://localhost:8787/atlas"
GEMINI_BASE_URL="http://localhost:8787/gemini"
```

In production these point at server-side proxies/functions instead (see [Production deployment](#production-deployment)).

### Example

```bash
# edge_lane_config.config
ATLAS_KEY="atlas_xxxx"
GEMINI_API_KEY="AIzaXXXX"
GEMINI_MODEL=gemini-2.5-flash

TRADIER_TOKEN="prod-token-xxxx"
TRADIER_TOKEN_SANDBOX="sb-token-xxxx"
DEVMODE=false                     # python CLI tools → production
DATA_PROVIDER=tradier             # browser-side data provider
TRADIER_ENV=production            # browser-side env

ATLAS_BASE_URL="http://localhost:8787/atlas"
GEMINI_BASE_URL="http://localhost:8787/gemini"

EDGE_LANE_VERSION="4.7"
```

> ⚠️ Don't edit-paste broken quotes into this file. Bash refuses to source any file with an unclosed `"`, and the build will fail with `GEMINI_API_KEY missing` or similar — even though the key is right there. If the build complains a key is missing, re-paste with proper closing quotes.

---

## CORS proxy — what it does and how to manage it

Atlas and Gemini don't return CORS headers, so the browser can't call them from a `file://` or `localhost` page directly. The proxy at `tools/cors_proxy.py` listens on `127.0.0.1:8787` and adds those headers while forwarding requests upstream. Keys are passed through from the browser's headers; the proxy never stores them.

### Routes

```
/atlas/<tool>    →  https://atlasmcp.finmanagerai.com/api/v1/tools/<tool>
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
3. Wraps the transformed JSX inside `edge_lane.template.html`, substituting `__ATLAS_KEY__`, `__GEMINI_API_KEY__`, `__TRADIER_TOKEN__`, `__DATA_PROVIDER__`, `__VERSION__`, base URLs, etc.
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

All tests are stdlib-only, load keys from `edge_lane_config.config`, and run from the repo root. Run them in this order when you've touched anything that touches the data path:

### 1. Tradier API sanity (provider-side)

```bash
python3 tests/tradier_smoke.py SPY
python3 tests/tradier_smoke.py NDX 2026-06-20    # specific underlying + expiration
```

Pings `markets/quotes`, `markets/options/expirations`, `markets/options/chains`. Strict pass criteria. Doesn't depend on the proxy.

### 2. Atlas REST sanity (if you use the Atlas provider)

```bash
python3 tests/atlas_rest_smoke.py SPY
```

### 3. Chain pipeline (matches JSX filter logic)

```bash
python3 tests/tradier_chain_pipeline_probe.py NVDA 2026-06-20
```

Mirrors EdgeLane's client-side filter step (±30% band, bid>0 selection). Useful when the bottom panel goes empty for a specific ticker.

### 4. Ticket parser round-trip

```bash
python3 tests/tradier_execute_ticket.py \
  'Sell Aggressive · SPX · 2026-05-21 · Short 7385P / Long 7365P · LIMIT credit $6.00 GTC · 1 contract'
```

Parses the ticket, resolves canonical OCC root, posts a preview-only order. No `--execute` = dry run. Tells you if EdgeLane's copy-button output is valid for Tradier.

### 5. End-to-end pipeline timing (Atlas path only, legacy)

```bash
python3 tests/e2e_pipeline.py SPY 2026-05-15
```

Target wall-clock total: **< 4 seconds**.

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

There are two independent env switches:

| Switch | Affects |
|---|---|
| `DEVMODE` in `edge_lane_config.config` | Python CLI tools (`tp`, `tradier_smoke.py`, `tradier_execute_ticket.py`). Selects which token (`TRADIER_TOKEN` vs `TRADIER_TOKEN_SANDBOX`) and which base URL. |
| `TRADIER_ENV` in `edge_lane_config.config` | The browser's chain/GEX/quote data provider. Set at build time. Rebuild after changing. |
| Per-user broker connection (Settings UI) | The browser's **order submission** path. Each user adds their own Tradier connection, which carries its own env field. |

**Common gotcha**: the operator-level browser token can be production while a user's broker connection is sandbox. The canonical-root probe in v4.7.54+ uses the broker connection's env when present, so order POSTs always go to the same env as the chain lookup. But chain rendering on screen still comes from the operator-level token. If that mismatches, the candidate strikes you see may not match the strikes available on your account.

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

### "Atlas …: blocked by CORS"
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
Something else started `cors_proxy.py` outside the service manager (maybe an old terminal, or the systemd unit when you also manually started it). `coproxy stop` will find it and kill it; then `coproxy start` to relaunch cleanly. v4.7+ of the service manager handles this automatically.

---

## Production deployment

*Placeholder — to be filled in once we lock the hosting target.*

Open questions to resolve before shipping prod:

- Do we want public access (with server-side key proxying) or gated access behind auth (keys can live in the bundle)?
- Cloudflare Pages + Pages Functions, or something else?
- Per-user broker connections imply per-user persistence beyond `sessionStorage` — what's the backend story (Cloudflare D1, Supabase, hosted Postgres)?
- Auth provider for real (vs the current simulated OAuth) — Clerk, Auth0, custom, or stay simulated for a beta?

The older `deployment.md` has a working Cloudflare Pages recipe (path A: keys baked in for private deploys, path B: Pages Function proxies keys server-side). That's the starting point we'll formalize here once decisions are made.

---

## Quick reference card

```bash
# ─── everyday loop ───
coproxy start                              # CORS proxy
./edge_lane_build.sh                       # rebuild after JSX edits
python3 -m http.server 8080                # serve
# open http://localhost:8080/edge_lane.html  (Ctrl+Shift+R after rebuild)

# ─── before commit ───
python3 tests/tradier_smoke.py SPY
./edge_lane_build.sh --dry-run
# (run other test scripts that match what you changed)

# ─── tools ───
tp                                         # list positions
tp -O                                      # list working orders
tp --open '<ticket>' --execute             # place order from copy-button ticket
coproxy status                             # check proxy health
coproxy install                            # auto-start on WSL boot

# ─── once per machine ───
./tools/cors_proxy_service.sh install      # systemd-auto-start
sudo loginctl enable-linger $USER          # keep service running without login
```
