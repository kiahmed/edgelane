# level_engine

The Level Engine container: a deterministic, level-based decision engine for
SPX that runs beside Matrix's existing engine. Design:
[`docs/level_engine.md`](../../../docs/level_engine.md).

Config: `facades_levels_engine.config` (gitignored; template `facades_levels_engine.config.example`;
excluded from the image by `market/backend/.dockerignore`).

## Run / deploy

Root-Makefile targets (`make help` → *Level Engine*). Every container target
passes `--profile levels` and touches only `facades-levels-engine`; the backend
and cloudflared are left alone.

```bash
make db-setup              # once: creates the config, role passwords, shared INTERNAL_FEED_TOKEN
make deploy-levels         # preflight → build + (re)create ONLY the engine container
make levels-status         # container state, admin + read /healthz, feed/runner status (read-only)
make levels-logs [N=500]   # follow its logs
make levels-restart        # restart without rebuilding (picks up config edits)
make levels-down           # stop + remove ONLY the engine
make deploy-down           # whole stack incl. the engine (plain `compose down` would leave it running)

# host dev run (default config level_engine/facades_levels_engine.config; MEMORY=1 = no DB)
make levels-run-local [CONFIG=path] [MEMORY=1]
# replay a recorded day (facades_levels_engine_replay role; writes lvl_replay.* only)
make levels-replay DATE=2026-11-20 [CONFIG=a.config] [CONFIG_B=b.config]
```

`deploy-levels` preflight (`ops/levels_preflight.sh`, never prints secrets)
fails if the config is missing, `LVL_DB_URL` / `INTERNAL_FEED_TOKEN` are empty,
or `edgelane-backend` isn't running; it warns if `LEVEL_FEED_ENABLED` isn't true
in `edgelane_market.config` or the two tokens differ. It builds the image tag
the backend shares — the running backend keeps its container until recreated.

`main` refuses to start with an empty `LVL_DB_URL` unless `LEVEL_ENGINE_MEMORY_STORE=1`.

## DB roles & passwords

Migration 0035 creates the login roles `facades_levels_engine_rw` and
`facades_levels_engine_replay` (only if missing) with **no password**. `make db-setup` /
`make db-push` (and deploy-prod) then run `tools/levels_provision.py`:

* `LVL_DB_URL` / `LVL_REPLAY_DB_URL` empty in `facades_levels_engine.config` → a random
  password is set on the role (sent as a SCRAM verifier, never in plaintext) and the
  DSN is written: Supavisor pooler, **session mode, port 5432**, user `<role>.<project_ref>`
  (falls back to `db.<ref>.supabase.co:5432` if the pooler lookup fails).
* DSN already filled → untouched; re-runs are no-ops. Postgres keeps only a hash, so
  the config is the only copy — **to rotate, blank the key and run `make db-setup`**.
* `INTERNAL_FEED_TOKEN` is generated if empty and kept identical in
  `edgelane_market.config` (no other key there is touched).
* The config is created from the `.example` if missing. An existing config (and
  `edgelane_market.config`) is rewritten **in place** — same inode — because the
  containers bind-mount each as a single file; so after `make db-setup` a plain
  `make levels-restart` / backend restart picks the new values up. The previous
  contents are kept as `<file>.bak` (gitignored, 0600) and restored if the
  read-back check fails. Mode 0600
  (best-effort on WSL `/mnt/c`, where chmod is usually ignored; the files are
  gitignored either way).
  Secrets are never printed. `ARGS=--dry-run` shows the plan with no network or writes.
For `levels-run-local`, point `BACKEND_FEED_URL` at `http://127.0.0.1:8789/internal/feed`
(the container value resolves only on the compose network) and stop the container
first if it holds admin port 8790.

## Modules

| Module | Role |
|---|---|
| `main.py` | entrypoint: read listener (`READ_PORT`, compose network only) + admin listener (`ADMIN_PORT`, host loopback) + tick loop in one asyncio loop; SIGTERM → graceful stop |
| `feed.py` | `FeedClient` — `GET /internal/feed/{symbol}` with `X-Internal-Token`; new-ts dedupe; status ok / stale (> 3×TICK_SEC without a new ts) / down; latest SPY / SPX trade (`prior_close`) — the admin page's reference and its typo-check price |
| `tick.py` | `TickRunner` — one deterministic tick: session gate → ratio → levels/clusters → state machine → scenario → `lvl_ticks` row (filtered chain) → brain → persist. Session-close tick, `maybe_close` fallback, `warm_start` (restart safety) |
| `read_api.py` | `GET /levels/decision/{symbol}`, `/levels/day/{date}`, `/levels/trace/{symbol}` (+ `/healthz`); everything else 404 |
| `replay.py` | `run_replay(source, cfg, date)` + CLI; levels as of each tick from `lvl_level_audit`; `--config-b` diff; `--memory` dry run |
| `admin.py` / `levels_cli.py` | Level Engine Management page + `make levels-show / levels-import`. Soft typo check (§17): a new / edited level > ±`TYPO_CHECK_PCT` % (default 30, 0 = off) from the latest trade isn't saved until confirmed — page **Save anyway**, JSON 409 `needs_confirm` → resend `"confirm": true`, `levels_cli import --force` |
| `config.py`, `store.py`, `clock.py`, `levels.py`, `ratio.py`, `state_machine.py`, `scenario.py`, `context.py` | config, storage contract (MemoryStore / PgStore), SPY→SPX conversion + clusters, ratio lock, §7.1 state machine, §7.2 scenario, brain seam |

## Determinism notes

* The brain sees the **recorded** view of each tick (`feed_from_tick_row` of the
  row written to `lvl_ticks`), so a replay gives it identical contexts.
* Feed metadata without its own column (expected_move, dte, prior closes,
  market_open, `levels_as_of`) lives in `lvl_ticks.zone.meta`.
* Warm start replays the brain too (outputs discarded) — brain ids (leg ids)
  must be derived deterministically from its inputs.
