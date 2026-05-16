# EdgeLane — Options Spread Optimizer

A single-file web app for finding and ranking multi-leg options spreads with the highest **composite tradeability score** (EV + structural health + liquidity + limit-order feasibility + POP). Pulls dealer-positioning bias from Atlas (mind-vest.io), synthesizes a narrative with Gemini Flash, then ranks candidates client-side.

Built for personal/local use. The deployed HTML embeds your API keys at build time, so the workflow is private-first — gate the deploy with auth or a tiny key-proxying Pages Function before exposing it publicly.

## Where to start

| If you want to… | Read this |
|---|---|
| **Set it up + run locally** | [deployment.md](./deployment.md) |
| **Understand the badges, scores, and decision rules** | [operating_manual.md](./operating_manual.md) |
| **Run the Python tests** | `tests/` — `atlas_rest_smoke.py`, `gemini_bias_bench.py`, `e2e_pipeline.py`, `atlas_subscription.py` |
| **Run the local CORS proxy during dev** | `python tools/cors_proxy.py` (see deployment.md) |

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
