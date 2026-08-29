# EdgeLane — Options Spread Optimizer

A single-file web app for finding and ranking multi-leg options spreads with the highest **composite tradeability score** (EV + structural health + liquidity + limit-order feasibility + POP). Pulls dealer-positioning bias from Tradier, synthesizes a narrative with Gemini Flash, then ranks candidates client-side. (EdgeLane originally sourced bias from Atlas's API (mind-vest.io); that provider was fully retired ~May 2026.)

Built for personal/local use. The deployed HTML embeds your API keys at build time, so the workflow is private-first — gate the deploy with auth or a tiny key-proxying Pages Function before exposing it publicly.

## The three products

This repo ships as three products under the **Facades** brand
([facades.trade](https://facades.trade)):

| Product | What it is here | Served at |
|---|---|---|
| **Matrix** | this optimizer — `spread_optimizer_v4_7_html.jsx`, `market/ui/` | `matrix.facades.trade` |
| **Simmer** | earnings / premium-watch engine — `simmer/`, `market/backend/app/simmer_*.py` | `simmer.facades.trade` |
| **Torque** | standalone order builder — `docs/torque.md` | *not public yet — admin-only, no multi-user login* |

The old internal names stay as they are in the code; the table above is the
whole mapping. The parent portal — the landing page plus the DNS and hosting
plumbing for those subdomains — is a separate repo, **facades-portal**, deployed
to Cloudflare Pages; see its `docs/hosting.md` before touching the domains.

## Where to start

| If you want to… | Read this |
|---|---|
| **Set it up + run locally** | [deployment.md](./deployment.md) |
| **Understand the badges, scores, and decision rules** | [operating_manual.md](./operating_manual.md) |
| **Run the Python tests** | `tests/` — `tradier_smoke.py`, `tradier_execute_ticket.py` |
| **Run the local CORS proxy during dev** | `python tools/cors_proxy.py` (see deployment.md) |
| **Place multi-leg orders fast (Torque)** | [docs/torque.md](./docs/torque.md) — `cd market/backend && make run-dev`, open `:8789/torque` |
| **Understand Torque's controls, readings, and labels** | [docs/torque_operating_manual.html](./docs/torque_operating_manual.html) |
| **Positions & realized P&L CLI (`tp`)** | [tools/tp_operating_manual.html](./tools/tp_operating_manual.html) |

## TL;DR

```bash
cp edge_lane_config.config.example edge_lane_config.config  # paste real keys
python tools/cors_proxy.py &                                 # background terminal
./edge_lane_build.sh                                         # produces edge_lane.html
python -m http.server 8080                                   # → http://localhost:8080/edge_lane.html
```

## Source of truth

`spread_optimizer_v4_7_html.jsx` is the only source file. The build script:
1. strips ESM imports + `export default`
2. inlines the JSX into `edge_lane.template.html`
3. substitutes API keys, model name, version, proxy URLs from the config
4. emits `edge_lane.html`

Auto-versioning: the build hashes JSX + template; patch number bumps on real changes, stays put on no-op rebuilds, resets when you edit `EDGE_LANE_VERSION` in config.
