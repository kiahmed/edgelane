# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Response style (ALWAYS)

Answer short, high-level, executive takeaways. Lead with the outcome or decision;
state what changed / what's needed in a sentence or a few bullets. No technical
ranting, no walls of detail, no step-by-step narration of the work. If deeper
detail exists, offer it ("want the details?") instead of dumping it. Skip preamble
and restating the question. This governs the final answer to the user — internal
tool use and verification stay as thorough as the task needs.

## What This Is

EdgeLane is a hybrid (single-file frontend + optional FastAPI backend) options spread optimizer. It finds and ranks multi-leg options spreads by a composite tradeability score (EV, structural health, liquidity, limit-order feasibility, probability of profit). Data comes from Tradier, with Gemini Flash providing prose narrative synthesis.

## Build & Run Commands

### Frontend

```bash
# First-time setup
cp edge_lane_config.config.example edge_lane_config.config  # fill in API keys

# Build (produces edge_lane.html from JSX + template + config)
./edge_lane_build.sh
./edge_lane_build.sh --dry-run     # preview substitutions without writing
./edge_lane_build.sh --new-output  # write to edge_lane_v{VERSION}.html

# Serve locally
python tools/cors_proxy.py &       # CORS proxy on port 8787 (needed for dev)
python -m http.server 8080         # open http://localhost:8080/edge_lane.html
```

### Backend (market service)

```bash
cd market/backend
make setup       # one-time: creates venv + installs deps
make run         # boot uvicorn on port 8789 (current DEVMODE)
make run-dev     # flip DEVMODE=true (sandbox Tradier, polls anytime)
make run-prod    # flip DEVMODE=false (production Tradier, market hours only)
make diag        # curl /diag/tradier (auth + latency check)
make status      # curl /status
make snapshot    # curl /snapshot/SPX
make accuracy    # curl /accuracy/SPX
```

Or directly: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8788 --reload`

### Tests

```bash
# Backend parity tests (56 tests — math layer must match JSX engine exactly)
cd market/backend
make test
# or: python -m pytest tests/parity/ -q --no-header

# Integration/smoke tests (from repo root, require live API keys)
python tests/tradier_smoke.py
```

## Architecture

### Frontend: `spread_optimizer_v4_7_html.jsx`

This single JSX file (~5,500 lines) is the **source of truth** for the frontend. The build script (`edge_lane_build.sh`) strips ESM syntax, inlines it into `edge_lane.template.html`, substitutes config values (`__TRADIER_TOKEN__`, `__GEMINI_API_KEY__`, `__EDGE_LANE_VERSION__`, etc.), and emits `edge_lane.html`. No Node.js build step — Babel-standalone transpiles in-browser.

Key subsystems within the JSX:
- **Bias engine** — deterministic dealer GEX wall detection + confidence scoring (JS math, not LLM)
- **Strategy engine** — candidate generation, width selection, composite scoring, health badges
- **Wall finder** — bilateral put/call GEX walls with strength tiers and penalties
- **Gemini integration** — structured output mode for prose narrative only; all math stays in JS
- **Chain fetching** — filters ±30% around spot, normalizes strikes, computes mids

### Backend: `market/backend/app/`

FastAPI + DuckDB service that continuously polls Tradier, runs the same math as the JSX (ported to Python), persists results, and evaluates bias accuracy over time.

| Module | Role |
|---|---|
| `main.py` | FastAPI app, lifespan, CORS, client wiring |
| `config.py` | Pydantic Settings from KEY=VALUE config file |
| `db.py` | DuckDB schema + connection (gex_snapshots, bias_decisions, outcomes) |
| `poller.py` | Background loop: fetch quote → chain → compute → persist → cache |
| `evaluator.py` | Background loop: evaluate bias predictions after configurable window |
| `engine.py` | Main orchestrator (calls bias_engine + strategy_engine) |
| `bias_engine.py` | Dealer GEX aggregation, wall detection, confidence (ported from JSX) |
| `strategy_engine.py` | Candidate generation + composite scoring (ported from JSX) |
| `strike_profiles.py` | Per-ticker OTM-OTM debit strike picker + DB-backed profiles (backend-only; see `docs/strike_profiles.md`) |
| `walls.py` | Bilateral wall finder + strength + penalty |
| `tradier_client.py` | Async httpx Tradier client (structured errors, rate-limit capture) |
| `mock_tradier.py` | Synthetic SPX chain for dev/demo (no credentials needed) |
| `routes/` | HTTP endpoints: /status, /snapshot/{symbol}, /accuracy/{symbol}, /orders, /diag/tradier, /webhook/edgelane_provider_gex, /strike-profiles, /torque/* |
| `torque_engine.py` / `torque_config.py` | Torque order-builder: pure strike auto-fill + pricing + lean math; per-ticker offset config |
| `ui/torque.html` | Torque standalone page (served at `GET /torque`) |

### Torque (standalone order builder)

A separate page (`/torque`) served from this same backend — the place-order
dialog as its own fast, auto-filling advanced order menu (ticker + one of 10
strategies, no bias logic). Auto-close is hybrid: single-leg limit entries use a
native Tradier OTO bracket, spreads/market entries place→confirm-fill→close (so a
rejected/unfilled entry never leaves a naked close). Strike offsets are
backend-configured (`torque_config.py` / `torque_tickers.json`), never hardcoded
in the page. Run via `make run-dev` (sandbox) and open `:8789/torque`. Full doc:
`docs/torque.md`.

### Data Providers

Tradier is the sole live data provider (EdgeLane was originally built on Atlas (mind-vest.io), which was fully retired ~May 2026):
- **Tradier** — raw chains + local GEX aggregation, for both frontend and backend
- **Mock** — deterministic synthetic chain when no Tradier token configured

## Configuration

Two config files (both KEY=VALUE format, `#` comments):
- `edge_lane_config.config` — frontend: API keys, model, data provider, proxy URLs, version
- `edgelane_market.config` — backend (FastAPI service + **Torque**): Tradier tokens, symbols, poll interval, scoring params, DB path, CORS origins

**DEVMODE** controls sandbox vs production Tradier and market-hours gating. Each
config file has its **own** `DEVMODE`, read by a **separate** code path — the
frontend file's flag is consumed only by the browser JSX build; the backend file's
flag is consumed only by the market service / Torque. They are independent; setting
one does not affect the other.

For the backend (`edgelane_market.config`), the `make` targets manage `DEVMODE` for you:
- `make run-dev` **rewrites** `DEVMODE=true` (sandbox token + `sandbox.tradier.com` + sandbox DB), then boots.
- `make run-prod` **rewrites** `DEVMODE=false` (production token + `api.tradier.com`, market-hours gated), then boots.
- `make run` does **not** flip it — it boots with whatever `DEVMODE` is currently in the file.

So `run-dev`/`run-prod` force the environment regardless of the file's prior value;
only bare `make run` respects the current setting. `DEVMODE=true` flips token,
account id, base URL, and DB path to sandbox in one switch (derived properties in
`config.py`).

## Key Conventions

- **Parity tests are critical**: the 56 tests in `market/backend/tests/parity/` verify the Python math layer produces identical output to the JSX engine. These must stay green when modifying bias_engine, strategy_engine, or walls.
- **Prose-only LLM usage**: Gemini Flash generates human-readable narrative; all scoring, wall detection, and candidate ranking is deterministic math — never delegate math to the LLM.
- **Auto-versioning**: the build script hashes JSX + template content; patch version bumps only on real changes.
- **pytest-asyncio**: backend tests use `asyncio_mode = "auto"` — async test functions are detected automatically.
- **Branches**: `main` is the PR target; current working branch is `master`.
