# EdgeLane — Deployment & Test Guide

Single-file HTML deploy of the spread optimizer. Bias is synthesized hybrid (deterministic JS engine + Gemini Flash for prose only); chain fetch and quote pulls go straight to the Tradier REST API. No build pipeline, no Node runtime — Babel-standalone compiles the JSX in-browser. (EdgeLane originally pulled data from Atlas's API (mind-vest.io); that provider was fully retired ~May 2026 and Tradier is now the sole data backbone.)

---

## File layout

| File | Role | Commit? |
|---|---|---|
| `spread_optimizer_v4_7_html.jsx` | Source of truth. All optimizer logic. Edit this for changes. | ✓ |
| `edge_lane.template.html` | HTML shell — CDN imports, theme CSS, placeholders. | ✓ |
| `edge_lane_build.sh` | Build script. Sources config, transforms JSX, substitutes placeholders, auto-bumps version. | ✓ |
| `edge_lane_config.config.example` | Template config with placeholders. | ✓ |
| `edge_lane_config.config` | Your real keys. | ✗ (gitignored) |
| `edge_lane.html` | Build artifact. Overwritten on every `./edge_lane_build.sh`. | ✗ (gitignored) |
| `.edge_lane_buildstate` | Auto-managed version state (last hash, last patch). | ✗ (gitignored) |
| `tools/cors_proxy.py` | Local-dev CORS proxy. Runs on `localhost:8787`. | ✓ |
| `tests/utils.py` | Shared test helpers (config loader, Tradier/Gemini wrappers, schema). | ✓ |
| `tests/tradier_smoke.py` | Verifies Tradier REST endpoints respond with expected shapes. | ✓ |
| `tests/gemini_bias_bench.py` | Flash vs Pro on real data, side-by-side comparison. | ✓ |
| `tests/e2e_pipeline.py` | Full pipeline simulation with stage-by-stage timings. | ✓ |
| `operating_manual.md` | Badge / verdict / score / strategy reference. | ✓ |
| `deployment.md` | This file. | ✓ |
| `.gitignore` | Build artifacts + secrets + caches. | ✓ |

---

## Architecture (v4.7+)

```
[detectBias click]
    ├── Tradier /v1/markets/quotes          ─┐
    │                                          ├── parallel ──→ JS engine (local GEX) ──→ structured bias fields
    └── Tradier /v1/markets/options/chains  ─┘                              ──→ Gemini Flash (prose only)
                                                                            ──→ bias card

[fetchChain click]
    └── Tradier /v1/markets/options/chains ──→ flatten + ±30% strike filter (client-side) ──→ ready

[strategy / slider changes]
    └── pure JS over chainCache.contracts ──→ instant
```

Wall, score, confidence, strategy recommendation are computed deterministically in JS. Gemini only writes the human-readable summary + 4 short signal sentences. Same inputs always produce the same trade pick.

---

## One-time setup

### 1. Configure keys

```bash
cd C:\soljet_dev\EdgeLane
cp edge_lane_config.config.example edge_lane_config.config
```

Edit `edge_lane_config.config`:

```
DATA_PROVIDER=tradier
TRADIER_SANDBOX_TOKEN="<your sandbox token>"   # paper/dev
TRADIER_PROD_TOKEN="<your production token>"   # live, only used when DEVMODE=false
GEMINI_API_KEY="AIza..."
GEMINI_MODEL=gemini-2.5-flash
EDGE_LANE_VERSION="4.7"        # major.minor base — auto-patches via build script
```

### 2. Install nothing

The whole stack uses only Python stdlib (tests + CORS proxy) and bash (build script). No `pip install`, no `npm install`. Just Python 3.10+ and bash.

---

## Local development workflow

### Terminal 1 — start the CORS proxy

```bash
cd C:\soljet_dev\EdgeLane
python tools/cors_proxy.py
# → listens on http://127.0.0.1:8787
# → /tradier/<rest> forwards to the Tradier REST API
# → /gemini/<rest>  forwards to https://generativelanguage.googleapis.com/v1beta/<rest>
```

Leave this running. It adds CORS headers so the browser can hit Tradier/Gemini from `file://` or `localhost`.

### Terminal 2 — enable the proxy URLs in config

Uncomment the two URL lines in `edge_lane_config.config`:

```
TRADIER_BASE_URL="http://localhost:8787/tradier"
GEMINI_BASE_URL="http://localhost:8787/gemini"
```

### Terminal 2 — build

```bash
./edge_lane_build.sh
# ✓ wrote edge_lane.html  (1783 lines, ~93 KB)
#   version: v4.7.1  (first build|content change ...)
```

The patch number auto-bumps when the JSX or template content changes. Editing the config base (`EDGE_LANE_VERSION="4.7"` → `"4.8"`) resets it to `.1`.

```bash
./edge_lane_build.sh --dry-run   # show what would happen + planned version, write nothing
./edge_lane_build.sh --help      # full flag list
```

### Terminal 2 — serve and open

```bash
python -m http.server 8080
# → open http://localhost:8080/edge_lane.html
```

Hard reload (Ctrl+F5) when CSS or JSX changes — browsers cache HTML aggressively.

---

## Tests (Python stdlib only — no deps)

All tests load keys from `edge_lane_config.config`. Run from the project root.

### Tradier REST smoke

```bash
python tests/tradier_smoke.py SPY
```

Hits the quote, options expirations, and options chain endpoints directly (no proxy). Strict pass criteria — fails if any required field is missing. Shape dumps inline so you can see exactly what Tradier is returning today.

### Gemini bias bench (Flash vs Pro)

```bash
python tests/gemini_bias_bench.py SPY 2026-05-15
```

Pulls real Tradier data, runs the bias prompt through both `gemini-2.5-flash` and `gemini-2.5-pro`, prints latency, token usage, and a side-by-side field-by-field comparison. Use this to decide if Pro is worth the extra cost on your tickers. (For most options-bias work, Flash agrees on the structured fields and runs ~3× faster.)

### End-to-end pipeline timing

```bash
python tests/e2e_pipeline.py SPY 2026-05-15
```

Simulates a single user click: parallel Tradier pull → local GEX + Gemini synthesis → chain fetch → local filter. Prints per-stage and total wall-clock latency. Target total: **< 4 seconds**.

---

## Production deployment (Cloudflare Pages)

`edge_lane.html` is a single static file. Two paths to ship it:

### Path A — Private deploy (simplest, keys baked in)

Use this if your page is gated behind authentication (Cloudflare Access, Workers Auth, basic auth, IP allowlist) or only used internally.

```bash
npm install -g wrangler
wrangler login
wrangler pages deploy . --project-name=edgelane
```

You get a `*.pages.dev` URL. Each rerun of `./edge_lane_build.sh && wrangler pages deploy .` ships the latest version. The keys live in the deployed HTML — fine if access is controlled.

### Path B — Public deploy (Pages Function proxies + server-side keys)

Use this if anyone with the URL can reach the page. Keys must NOT be in the browser bundle.

**Step 1.** Create `functions/api/[[path]].js` in the repo root:

```javascript
// functions/api/[[path]].js
// Cloudflare Pages Function — proxies /api/tradier/*  and /api/gemini/*  through
// to the upstreams, attaching keys from environment variables. The browser
// never sees the keys.
export async function onRequest(context) {
  const { request, env, params } = context;
  const path = params.path.join('/');

  let upstream, headers;
  if (path.startsWith('tradier/')) {
    upstream = 'https://api.tradier.com/' + path.slice(8);
    headers = { 'Authorization': `Bearer ${env.TRADIER_TOKEN}`, 'Accept': 'application/json' };
  } else if (path.startsWith('gemini/')) {
    upstream = 'https://generativelanguage.googleapis.com/v1beta/' + path.slice(7);
    headers = { 'x-goog-api-key': env.GEMINI_API_KEY, 'Content-Type': 'application/json' };
  } else {
    return new Response('not found', { status: 404 });
  }

  const resp = await fetch(upstream, {
    method: request.method,
    headers,
    body: request.method === 'POST' ? await request.text() : undefined,
  });
  return new Response(await resp.text(), {
    status: resp.status,
    headers: { 'Content-Type': resp.headers.get('Content-Type') || 'application/json' },
  });
}
```

**Step 2.** In Cloudflare Pages dashboard → your project → Settings → Environment variables, set:
- `TRADIER_TOKEN` = your Tradier production access token
- `GEMINI_API_KEY` = your Gemini key (production)

**Step 3.** Build with proxy URLs pointing at the function, and EMPTY browser-side keys:

```bash
# In edge_lane_config.config (production build only — keep separate file):
DATA_PROVIDER=tradier
TRADIER_PROD_TOKEN=""
GEMINI_API_KEY=""
TRADIER_BASE_URL="/api/tradier"
GEMINI_BASE_URL="/api/gemini"
EDGE_LANE_VERSION="4.7"
```

```bash
./edge_lane_build.sh --config edge_lane_config.production.config
wrangler pages deploy . --project-name=edgelane
```

The browser now hits `/api/tradier/*` (same origin, no CORS), the Pages Function attaches the keys server-side, response gets returned. Keys never leave Cloudflare's servers.

---

## Troubleshooting

### "Tradier …: blocked by CORS or network"

Tradier doesn't return CORS headers for browser origins. Run the smoke test to confirm REST + auth work outside the browser:

```bash
python tests/tradier_smoke.py SPY
```

If that passes but the browser fails:
- **Quick fix (dev)**: make sure `tools/cors_proxy.py` is running and the URLs in config are uncommented and pointing at it
- **Real fix (production)**: use the Pages Function in Path B above

### "Tradier …: rate limit reached"

You've hit Tradier's per-minute API rate limit. Back off and retry; the response headers report the remaining quota and reset window. Detect Bias and Run Optimizer each consume one chain call (quote is bundled).

### "Gemini …: HTTP 503"

Gemini overloaded. The browser-side `_callGemini` retries with exponential backoff automatically (1s → 2s → 4s, ±20% jitter, max 3 retries). If it still fails after retries, wait or set `GEMINI_MODEL=gemini-2.5-flash-lite` for higher quota.

### "GEMINI_API_KEY missing" / "TRADIER token missing"

Build script's source step rejected the config. Most common cause: the config got truncated mid-string (any unmatched `"` makes bash refuse to source). Re-paste the keys with proper closing quotes.

### Babel error in browser console

Open DevTools → Console. If you see "Unterminated JSX" or similar, the JSX got truncated during a write. Compare:

```bash
wc -l spread_optimizer_v4_7_html.jsx
# expected: ~1634 lines (varies by version)
```

If short, restore from git or your last working backup.

### Version doesn't bump

Run `./edge_lane_build.sh` and look at the printed reason:

| Reason | Means |
|---|---|
| `first build` | No `.edge_lane_buildstate` yet — patch starts at 1 |
| `no content change` | JSX + template hash unchanged from last build — patch stays |
| `content change (hash A → B)` | Hash differs, patch bumps |
| `base change (4.7 → 4.8)` | You edited `EDGE_LANE_VERSION` in config — patch resets to 1 |

Force a manual reset by deleting `.edge_lane_buildstate`.

---

## Roadmap (not yet shipped)

The Push-to-broker button (`↗` icon on each card) is a UI stub for v4.8+ where a per-user broker integration will live. Currently it's intentionally disabled — the Copy button (`⎘`) does the same job manually: copy the trade ticket, paste into your broker's order entry.

## Quick reference card

```bash
# ---- everyday loop ----
python tools/cors_proxy.py &              # terminal background
./edge_lane_build.sh                       # rebuild after edits
python -m http.server 8080                 # serve
# open http://localhost:8080/edge_lane.html, hard reload after CSS changes

# ---- before commit ----
python tests/tradier_smoke.py SPY
python tests/e2e_pipeline.py SPY 2026-05-15
./edge_lane_build.sh --dry-run             # confirm version + substitutions clean

# ---- ship ----
./edge_lane_build.sh
wrangler pages deploy . --project-name=edgelane

# ---- market backend + Torque order builder ----
cd market/backend
make run-dev          # sandbox (paper) on :8789  — open /torque
make run-prod         # production (real money)
make test             # parity + torque tests
# config: edgelane_market.config (DEVMODE); detail: docs/torque.md
```

---

## Production deployment (market backend + Vercel UI)

The market service ships two deployables:

- **Backend** (`market/backend` — FastAPI API + the Torque order builder at `/torque`)
  runs as a **Docker container on a local PC**, exposed over HTTPS by a
  **Cloudflare *quick* tunnel** sidecar — free, no domain, no token. The
  cloudflared container gets a fresh `*.trycloudflare.com` URL on each start and
  **publishes it to Supabase** (`app_config.api_base`); the frontend reads that
  at load, so a rotated URL needs **no redeploy**. (A stable named tunnel is a
  later option once you own a domain — see below.)
- **Frontend** (`market/ui/index.html` — the market dashboard, *not* the legacy
  `edge_lane.html`) deploys to **Vercel** (same pattern as the sibling `RelativeQs`).

Torque is part of the **backend** container, not the Vercel deploy.

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

### Migrating the backend to another machine

The Vercel frontend has **no baked-in backend URL** — the cloudflared container
publishes its `*.trycloudflare.com` URL to Supabase `app_config.api_base` on every
start, and the UI reads that pointer at runtime. So moving the backend host needs
**no Vercel redeploy**: the new tunnel publishes a new URL and the frontend follows.

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
make deploy-be             # build + start; tunnel publishes the new api_base

# ── back on the OLD machine, once the new one is serving ──
make deploy-down           # stop backend + cloudflared here
```

> ⚠️ **Don't leave both machines' cloudflared running.** Each tunnel writes
> `app_config.api_base`; two publishers race, and the old one (pointing at the host
> you're abandoning) can overwrite the pointer and send the frontend to a dead
> backend. Exactly **one** stack should be up. `make deploy-down` only touches Docker
> on that host — the Vercel site is untouched and stays live throughout.

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
`public` tables (cascade depth 1). So deleting a user atomically removes its
`profiles` / `broker_configs` / `user_settings` (plus Supabase's own `auth.*` rows)
with no orphans, and you can't insert a child row for a non-existent user (FK
rejects it). Before any `delete`, the tool prints a preview of exactly what
cascades — recursively — e.g.:

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

### Files

| File | Role | Commit? |
|---|---|---|
| `deploy.sh` | Orchestrator (frontend Vercel + backend Docker/tunnel). | ✓ |
| `deploy/docker-compose.yml` | Backend + cloudflared quick-tunnel/publisher sidecar. | ✓ |
| `market/backend/Dockerfile` | Backend image (editable install; serves API + Torque). | ✓ |
| `market/backend/.dockerignore` | Keeps secrets/tests out of the build context. | ✓ |
| `deploy/cloudflared/` | Quick-tunnel image: `Dockerfile` + `publish-url.sh` (publishes the rotating URL to Supabase). | ✓ |
| `tools/db_push.py` | Applies `supabase/migrations/*.sql` (schema) via the Management API. | ✓ |
| `tools/supabase_admin.py` | Standalone no-SQL data admin CLI: list tables + CRUD. | ✓ |
| `deploy/.env.example` | Template: Supabase keys, image, Vercel project, optional tunnel token/API base. | ✓ |
| `deploy/.env` | Real creds (Supabase service key, etc.). | ✗ (gitignored) |
| `vercel.json` | Static deploy config (`outputDirectory: dist`). | ✓ |
| `.vercelignore` | Hides backend/secrets/legacy from the Vercel upload. | ✓ |
| `dist/` | Staged frontend output (built by `deploy.sh`). | ✗ (gitignored) |

### One-time setup

1. **Backend config** — `edgelane_market.config` with the **production** Tradier
   token and `DEVMODE=false` (deploy.sh warns if it's `true`). Bind-mounted
   read-only into the container at `/config/`; DuckDB persists in the
   `edgelane-data` volume.
2. **Cloudflare quick tunnel** — nothing to create. The `edgelane-cloudflared`
   container (built from `deploy/cloudflared/`) starts a free quick tunnel and
   publishes its URL to Supabase. It only needs `SUPABASE_URL` +
   `SUPABASE_SERVICE_KEY` in `deploy/.env` (the service key writes
   `app_config.api_base`). `CF_TUNNEL_TOKEN`/`EDGELANE_API_BASE` stay blank.
3. **Vercel** — `npm i -g vercel && vercel login`. First `deploy-fe` links the
   repo to the `edgelane` Vercel project. Leave `EDGELANE_API_BASE` blank — the
   UI discovers the backend URL at runtime.

**Backend URL wiring (runtime service discovery):** the cloudflared container
publishes its current `*.trycloudflare.com` URL to Supabase `app_config.api_base`
on every start (migration `0005`, anon-readable / service_role-write). At load,
`market/ui/index.html` reads that pointer (`resolveApiBasePointer()`) and, on a
failed call, re-reads it and retries once — so a tunnel rotation is invisible to
users with **no Vercel redeploy**. Explicit `?api=` / `localStorage` overrides
still win (dev); local dev falls back to `127.0.0.1:8789`.

### Upgrading to a stable named tunnel (later, once you own a domain)

The quick tunnel is best-effort and rate-limited — fine for launch/traffic-building,
but when you outgrow it, move to a stable named tunnel on your own domain. This is
**not** a single env flip; it's a one-time setup:

1. **Register a domain** and add it to Cloudflare as a zone (Cloudflare Registrar
   is at-cost; any registrar works if you point its nameservers at Cloudflare).
2. **Create a named tunnel** (CF dashboard → Zero Trust → Networks → Tunnels, or
   `cloudflared tunnel create edgelane-backend`) and add a **public hostname**
   `api.<domain>` → service `http://edgelane-backend:8789`. Copy the connector token.
3. **`deploy/.env`** — set `CF_TUNNEL_TOKEN=<connector token>` and
   `EDGELANE_API_BASE=https://api.<domain>`.
4. **`deploy/docker-compose.yml`** — replace the `edgelane-cloudflared` service's
   `build:`/`image:`/`environment:` with the named-tunnel form (commented inline
   in that service):
   ```yaml
   image: cloudflare/cloudflared:latest
   command: tunnel --no-autoupdate run --token ${CF_TUNNEL_TOKEN}
   ```
5. `make deploy-prod` (or `deploy-be` + `deploy-fe`).

After the swap, the backend always answers at `https://api.<domain>` (no rotation),
so `EDGELANE_API_BASE` is baked into the frontend as the primary URL. The Supabase
`app_config.api_base` pointer is no longer written by the named-tunnel container —
leave the row as-is or drop it; the frontend prefers the baked URL when present.
