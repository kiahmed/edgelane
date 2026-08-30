# EdgeLane — Options Spread Optimizer

Finds and ranks multi-leg options spreads by a **composite tradeability score**
(EV + structural health + liquidity + limit-order feasibility + POP), reading
dealer-positioning bias from Tradier — and grades its own picks afterwards, so the
published win rate is earned rather than asserted. (EdgeLane originally sourced
bias from Atlas's API (mind-vest.io); that provider was fully retired ~May 2026.)

A FastAPI + DuckDB backend holds all the math and runs behind a Cloudflare named
tunnel at `edge.facades.trade`, serving all three products below.

## The three products

This repo ships as three products under the **Facades** brand
([facades.trade](https://facades.trade)):

| Product | What it is here | Served at |
|---|---|---|
| **Matrix** | this optimizer — `market/ui/` | `matrix.facades.trade` |
| **Simmer** | earnings / premium-watch engine — `simmer/`, `market/backend/app/simmer_*.py` | `simmer.facades.trade` |
| **Torque** | standalone order builder — `docs/torque.md` | *not public yet — admin-only, no multi-user login* |

The old internal names stay as they are in the code; the table above is the
whole mapping. The parent portal — the landing page plus the DNS and hosting
plumbing for those subdomains — is a separate repo, **facades-portal**, deployed
to Cloudflare Pages; see its `docs/hosting.md` before touching the domains.

## Where to start

| If you want to… | Read this |
|---|---|
| **Set it up, run + deploy the stack** | [deployment.md](./deployment.md) |
| **Sign up, connect a broker, use Matrix** | [docs/matrix_operating_manual.html](./docs/matrix_operating_manual.html) |
| **Run the Python tests** | `tests/` — `tradier_smoke.py`, `tradier_execute_ticket.py` |
| **Understand Simmer's readiness scoring** | [simmer/simmer_operating_manual.html](./simmer/simmer_operating_manual.html) |
| **Place multi-leg orders fast (Torque)** | [docs/torque.md](./docs/torque.md) — `cd market/backend && make run-dev`, open `:8789/torque` |
| **Understand Torque's controls, readings, and labels** | [docs/torque_operating_manual.html](./docs/torque_operating_manual.html) |
| **Positions & realized P&L CLI (`tp`)** | [tools/tp_operating_manual.html](./tools/tp_operating_manual.html) |

## TL;DR

```bash
cp edgelane_market.config.example edgelane_market.config   # paste real keys
cd market/backend && make setup && make run-dev            # backend on :8789
make ui                                                    # open Matrix against it
```

## Source of truth

The backend (`market/backend/app/`) owns every number: bias, walls, candidate
generation, composite scoring, and the self-evaluation. The UIs are readouts —
`market/ui/index.html` (Matrix, static, no build step), `simmer/ui/` (SvelteKit),
and `market/backend/ui/torque.html` (served by the backend at `/torque`).

The 56 parity tests in `market/backend/tests/parity/` pin the Python math to the
original JSX engine's output; they must stay green when touching `bias_engine`,
`strategy_engine`, or `walls`.

> **Removed 2026-08:** the original single-file frontend
> (`spread_optimizer_v4_7_html.jsx` → `edge_lane.html`, built by
> `edge_lane_build.sh`) and its dev-only Gemini CORS proxy. Matrix superseded it;
> recover from git history if you ever need it.
