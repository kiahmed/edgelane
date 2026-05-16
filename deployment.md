# EdgeLane — Deployment & Test Guide

Single-file HTML deploy of the spread optimizer. Bias is synthesized hybrid (deterministic JS engine + Gemini Flash for prose only); chain fetch and quote pulls go straight to Atlas REST. No build pipeline, no Node runtime — Babel-standalone compiles the JSX in-browser.

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
| `tests/utils.py` | Shared test helpers (config loader, Atlas/Gemini wrappers, schema). | ✓ |
| `tests/atlas_rest_smoke.py` | Verifies Atlas REST endpoints respond with expected shapes. | ✓ |
| `tests/gemini_bias_bench.py` | Flash vs Pro on real data, side-by-side comparison. | ✓ |
| `tests/e2e_pipeline.py` | Full pipeline simulation with stage-by-stage timings. | ✓ |
| `operating_manual.md` | Badge / verdict / score / strategy reference. | ✓ |
| `deployment.md` | This file. | ✓ |
| `.gitignore` | Build artifacts + secrets + caches. | ✓ |

---

## Architecture (v4.7+)

```
[detectBias click]
    ├── Atlas /get_stock_quote          ─┐
    │                                     ├── parallel ──→ JS engine ──→ structured bias fields
    └── Atlas /analyze_greek_exposures  ─┘                              ──→ Gemini Flash (prose only)
                                                                         ──→ bias card

[fetchChain click]
    └── Atlas /get_options_chain ──→ flatten + ±30% strike filter (client-side) ──→ ready

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
ATLAS_KEY="<your atlas key>"
ANTHROPIC_KEY="sk-ant-..."   # only needed if you ever build with the legacy LLM/MCP path
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
# → /atlas/<tool>   forwards to https://atlasmcp.finmanagerai.com/api/v1/tools/<tool>
# → /gemini/<rest>  forwards to https://generativelanguage.googleapis.com/v1beta/<rest>
```

Leave this running. It adds CORS headers so the browser can hit Atlas/Gemini from `file://` or `localhost`.

### Terminal 2 — enable the proxy URLs in config

Uncomment the two URL lines in `edge_lane_config.config`:

```
ATLAS_BASE_URL="http://localhost:8787/atlas"
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

### Atlas REST smoke

```bash
python tests/atlas_rest_smoke.py SPY
```

Hits `get_stock_quote`, `analyze_greek_exposures`, `get_options_chain` directly (no proxy). Strict pass criteria — fails if any required field is missing. Shape dumps inline so you can see exactly what Atlas is returning today.

### Gemini bias bench (Flash vs Pro)

```bash
python tests/gemini_bias_bench.py SPY 2026-05-15
```

Pulls real Atlas data, runs the bias prompt through both `gemini-2.5-flash` and `gemini-2.5-pro`, prints latency, token usage, and a side-by-side field-by-field comparison. Use this to decide if Pro is worth the extra cost on your tickers. (For most options-bias work, Flash agrees on the structured fields and runs ~3× faster.)

### End-to-end pipeline timing

```bash
python tests/e2e_pipeline.py SPY 2026-05-15
```

Simulates a single user click: parallel Atlas pull → Gemini synthesis → chain fetch → local filter. Prints per-stage and total wall-clock latency. Target total: **< 4 seconds**.

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
// Cloudflare Pages Function — proxies /api/atlas/*  and /api/gemini/*  through
// to the upstreams, attaching keys from environment variables. The browser
// never sees the keys.
export async function onRequest(context) {
  const { request, env, params } = context;
  const path = params.path.join('/');

  let upstream, headers;
  if (path.startsWith('atlas/')) {
    upstream = 'https://atlasmcp.finmanagerai.com/api/v1/tools/' + path.slice(6);
    headers = { 'Authorization': `Bearer ${env.ATLAS_KEY}`, 'Content-Type': 'application/json' };
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
- `ATLAS_KEY` = your Atlas key (production)
- `GEMINI_API_KEY` = your Gemini key (production)

**Step 3.** Build with proxy URLs pointing at the function, and EMPTY browser-side keys:

```bash
# In edge_lane_config.config (production build only — keep separate file):
ATLAS_KEY=""
GEMINI_API_KEY=""
ANTHROPIC_KEY=""
ATLAS_BASE_URL="/api/atlas"
GEMINI_BASE_URL="/api/gemini"
EDGE_LANE_VERSION="4.7"
```

```bash
./edge_lane_build.sh --config edge_lane_config.production.config
wrangler pages deploy . --project-name=edgelane
```

The browser now hits `/api/atlas/*` (same origin, no CORS), the Pages Function attaches the keys server-side, response gets returned. Keys never leave Cloudflare's servers.

---

## Troubleshooting

### "Atlas …: blocked by CORS or network"

Atlas doesn't return CORS headers for browser origins. Run the smoke test to confirm REST + auth work outside the browser:

```bash
python tests/atlas_rest_smoke.py SPY
```

If that passes but the browser fails:
- **Quick fix (dev)**: make sure `tools/cors_proxy.py` is running and the URLs in config are uncommented and pointing at it
- **Real fix (production)**: use the Pages Function in Path B above

### "Atlas …: rate_limit: free tier limit reached"

You've hit the 10/month free-tier cap on mind-vest.io. Upgrade or connect a broker at https://mind-vest.io/dashboard. Each Detect Bias click consumes 2 calls (quote + greeks); each Run Optimizer adds 1 (chain).

### "Gemini …: HTTP 503"

Gemini overloaded. The browser-side `_callGemini` retries with exponential backoff automatically (1s → 2s → 4s, ±20% jitter, max 3 retries). If it still fails after retries, wait or set `GEMINI_MODEL=gemini-2.5-flash-lite` for higher quota.

### "GEMINI_API_KEY missing" / "ATLAS_KEY missing"

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
python tests/atlas_rest_smoke.py SPY
python tests/e2e_pipeline.py SPY 2026-05-15
./edge_lane_build.sh --dry-run             # confirm version + substitutions clean

# ---- ship ----
./edge_lane_build.sh
wrangler pages deploy . --project-name=edgelane
```
