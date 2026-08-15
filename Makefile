# EdgeLane — top-level convenience targets.
#
# Run `make` (or `make help`) for the categorised target list. Section headers
# below (`# ---- Name ----`) are what the help target groups on, so keep new
# targets under a heading and give each one a `## description`.
.DEFAULT_GOAL := help

# Defaults
BACKEND      = market/backend
PY           = python3
PORT         ?= 8789
HOST         ?= 127.0.0.1
CORS_PORT    ?= 8787
COPROXY      = ./tools/cors_proxy_service.sh
DEPLOY_SH    = ./deploy.sh
# backend deploy target: local_container | cloud
DEPLOY       ?= local_container
# extra flags passed through to the underlying script, e.g. make deploy-prod ARGS=-n
ARGS         ?=
COMPOSE      = docker compose -f deploy/docker-compose.yml --env-file deploy/.env
# Dedicated buildx builder for THIS repo. The docker-container driver gives it a
# private cache pool, so `make deploy-prune` reclaims only EdgeLane's build cache
# and never touches other projects' layers on the shared `default` builder.
BUILDER      = edgelane-builder
# DuckDB store (compose-prefixed volume name) + gitignored migration tarball
DATA_VOLUME  = edgelane_edgelane-data
DATA_DUMP    = deploy/edgelane-data.tar.gz

.PHONY: help \
        build build-dry cors cors-start cors-stop cors-restart cors-delete \
        setup run run-dev run-prod run-bg stop logs clean \
        diag status snapshot accuracy \
        test test-e2e test-all \
        webhook-debug webhook-post ext-version ext-policy \
        ui \
        deploy-be deploy-fe deploy-prod deploy-dry \
        deploy-down deploy-be-down deploy-be-restart deploy-prune deploy-builder \
        db-push db-push-dry deploy-data-dump deploy-data-restore \
        doctor vercel-setup check-tunnel

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} \
	     /^# ----/ {sec = substr($$0, 8); sub(/ -+$$/, "", sec); pending = 1} \
	     /^[a-zA-Z0-9_-]+:.*## / {if (pending) {printf "\n\033[1m%s\033[0m\n", sec; pending = 0} \
	       desc = $$2; gsub(/[A-Za-z][A-Za-z0-9_]*=("[^"]*"|[^ ,)]+)/, "\033[38;5;208m&\033[0m", desc); \
	       printf "  \033[94m%-22s\033[0m %s\n", $$1, desc}' $(MAKEFILE_LIST)
	@printf "\n\033[1mOverrides\033[0m\n"
	@printf "  \033[38;5;208m%-14s\033[0m %s\n" "PORT=8788"    "backend port (default $(PORT))"
	@printf "  \033[38;5;208m%-14s\033[0m %s\n" "HOST=0.0.0.0" "backend bind address (default $(HOST))"
	@printf "  \033[38;5;208m%-14s\033[0m %s\n" "DEPLOY=cloud" "backend deploy target (default $(DEPLOY))"
	@printf "  \033[38;5;208m%-14s\033[0m %s\n" "ARGS=-n"      "extra flags passed through to the underlying script"
	@printf "\n  e.g. \033[94mmake run\033[0m \033[38;5;208mPORT=8788\033[0m   |   \033[94mmake deploy-prod\033[0m \033[38;5;208mARGS=-n\033[0m\n\n"

# ---- Frontend (legacy single-file app) ----

build: ## Run edge_lane_build.sh → produces edge_lane.html
	@./edge_lane_build.sh

build-dry: ## Dry-run the build (preview substitutions, write nothing)
	@./edge_lane_build.sh --dry-run

cors: ## Start the CORS proxy on CORS_PORT=8787 (background)
	@echo ">>> CORS proxy on port $(CORS_PORT) (background)"
	@$(PY) tools/cors_proxy.py &

cors-start: ## Start the CORS proxy via the service manager (auto-restart)
	@EDGELANE_CORS_PORT=$(CORS_PORT) $(COPROXY) start

cors-stop: ## Stop the CORS proxy service
	@$(COPROXY) stop

cors-restart: ## Restart the CORS proxy service
	@EDGELANE_CORS_PORT=$(CORS_PORT) $(COPROXY) restart

cors-delete: ## Uninstall the CORS proxy auto-start hook
	@$(COPROXY) uninstall

# ---- Backend — setup ----

setup: ## One-time venv + pip install (market/backend)
	@$(MAKE) -C $(BACKEND) setup

clean: ## Clear __pycache__ under the backend
	@$(MAKE) -C $(BACKEND) clean

# ---- Backend — run ----

run: ## Boot uvicorn on PORT=8789 using whatever DEVMODE is currently set
	@$(MAKE) -C $(BACKEND) run PORT=$(PORT) HOST=$(HOST)

run-dev: ## Flip DEVMODE true, then boot (sandbox Tradier, polls anytime)
	@$(MAKE) -C $(BACKEND) run-dev PORT=$(PORT) HOST=$(HOST)

run-prod: ## Flip DEVMODE false, then boot (production Tradier, market hours)
	@$(MAKE) -C $(BACKEND) run-prod PORT=$(PORT) HOST=$(HOST)

run-bg: ## Run in the background, logging to /tmp/edgelane-market.log
	@$(MAKE) -C $(BACKEND) run-bg PORT=$(PORT) HOST=$(HOST)

stop: ## Kill any uvicorn listening on PORT=8789
	@$(MAKE) -C $(BACKEND) stop PORT=$(PORT)

logs: ## Tail the background log
	@$(MAKE) -C $(BACKEND) logs

# ---- Backend — inspect (server must be running) ----

diag: ## /diag/tradier — auth + latency check
	@$(MAKE) -C $(BACKEND) diag PORT=$(PORT) HOST=$(HOST)

status: ## /status — service health + poller state
	@$(MAKE) -C $(BACKEND) status PORT=$(PORT) HOST=$(HOST)

snapshot: ## /snapshot/SPX — latest engine output
	@$(MAKE) -C $(BACKEND) snapshot PORT=$(PORT) HOST=$(HOST)

accuracy: ## /accuracy/SPX — rolling win rate
	@$(MAKE) -C $(BACKEND) accuracy PORT=$(PORT) HOST=$(HOST)

# ---- Extension / Webhook (server must be running) ----

webhook-debug: ## /webhook/debug — latest payload + history
	@$(MAKE) -C $(BACKEND) webhook-debug PORT=$(PORT) HOST=$(HOST)

webhook-post: ## POST a synthetic GEX payload to the webhook
	@$(MAKE) -C $(BACKEND) webhook-post PORT=$(PORT) HOST=$(HOST)

ext-version: ## /extension/version — policy version
	@$(MAKE) -C $(BACKEND) ext-version PORT=$(PORT) HOST=$(HOST)

ext-policy: ## /extension/policy.json — hot-reload config
	@$(MAKE) -C $(BACKEND) ext-policy PORT=$(PORT) HOST=$(HOST)

# ---- Tests ----

test: ## Parity tests — Python math layer must match the JSX engine
	@$(MAKE) -C $(BACKEND) test

test-e2e: ## Extension webhook end-to-end tests
	@$(MAKE) -C $(BACKEND) test-e2e

test-all: ## Parity + e2e together
	@$(MAKE) -C $(BACKEND) test-all

# ---- UI ----

ui: ## Open market/ui/index.html in a browser
	@$(MAKE) -C $(BACKEND) ui

# ---- Deploy ----
# Frontend (market UI) → Vercel; backend (FastAPI + Torque) → Docker container
# behind a Cloudflare tunnel. See deploy.sh + deploy/ for config.

deploy-be: deploy-builder ## Backend → Docker container + tunnel (DEPLOY=local_container)
	@BUILDX_BUILDER=$(BUILDER) $(DEPLOY_SH) -b --target $(DEPLOY) $(ARGS)

deploy-fe: ## Market UI → Vercel
	@$(DEPLOY_SH) -f $(ARGS)

deploy-prod: deploy-builder ## Backend + frontend together (pushes Supabase migrations first)
	@BUILDX_BUILDER=$(BUILDER) $(DEPLOY_SH) --target $(DEPLOY) $(ARGS)

deploy-dry: ## Dry-run the full deploy (prints commands, runs nothing)
	@$(DEPLOY_SH) --target $(DEPLOY) -n $(ARGS)

# Teardown the whole stack (backend + cloudflared + network). The named
# edgelane-data volume (DuckDB) is KEPT; add ARGS=-v to also drop it.
deploy-down: ## Stop + remove the stack, keeping the DB volume (ARGS=-v drops it)
	@$(COMPOSE) down $(ARGS)

# Remove ONLY the backend container; leaves cloudflared (and the tunnel) running.
deploy-be-down: ## Remove ONLY the backend (cloudflared keeps running)
	@$(COMPOSE) rm -sf edgelane-backend

# Rebuild + recreate ONLY the backend (latest code), without touching cloudflared.
deploy-be-restart: deploy-builder ## Rebuild + recreate ONLY the backend, latest code
	@BUILDX_BUILDER=$(BUILDER) $(COMPOSE) up -d --no-deps --build edgelane-backend

# Ensure the dedicated buildx builder exists AND boots (self-healing). Prereq of
# every backend build target, so you never run it by hand. On Docker Desktop/WSL2
# the builder's backing container gets a stale Docker-Desktop bind-mount across a
# reboot and won't boot (`Exited 127`); a plain `inspect` still succeeds, so we
# must actively `--bootstrap` and, if that fails, rm + recreate a fresh one.
# Removing it entirely (undo isolation): docker buildx rm $(BUILDER)
deploy-builder: ## Create the dedicated buildx builder if missing (auto-run by deploy targets)
	@docker buildx inspect --bootstrap $(BUILDER) >/dev/null 2>&1 || { \
		docker buildx rm $(BUILDER) >/dev/null 2>&1 || true; \
		docker buildx create --name $(BUILDER) --driver docker-container --bootstrap >/dev/null; \
	}

# Reclaim disk. Two pools: (1) dangling (untagged) images left by rebuilds, and
# (2) THIS repo's BuildKit cache — scoped to the $(BUILDER) instance, so other
# projects' cache on the shared `default` builder is untouched. ARGS=-a prunes
# all unused images (more aggressive); it does not affect the builder-cache sweep.
deploy-prune: ## Reclaim dangling images + this repo's build cache (ARGS=-a for all unused)
	@docker image prune -f $(ARGS)
	@docker buildx prune --builder $(BUILDER) -f

# ---- Data & schema ----

# Supabase schema — idempotent push of supabase/migrations/*.sql.
db-push: ## Apply Supabase migrations (idempotent)
	@$(PY) tools/db_push.py $(ARGS)

db-push-dry: ## List the migrations that would be applied
	@$(PY) tools/db_push.py --dry-run

# Machine migration — DuckDB volume in/out. Dump tars the volume into
# $(DATA_DUMP) (gitignored); copy that file to the new host and run restore
# there BEFORE `make deploy-be` so the fresh stack picks up the history.
deploy-data-dump: ## Tar the DuckDB volume → deploy/edgelane-data.tar.gz (migration)
	@docker run --rm -v $(DATA_VOLUME):/data:ro -v "$(CURDIR)/deploy":/backup \
		alpine tar czf /backup/$(notdir $(DATA_DUMP)) -C /data .
	@echo ">>> wrote $(DATA_DUMP) ($$(du -h $(DATA_DUMP) | cut -f1))"

deploy-data-restore: ## Restore that tarball into the volume on a new host
	@test -f $(DATA_DUMP) || { echo "missing $(DATA_DUMP) — copy it here first"; exit 1; }
	@docker volume create $(DATA_VOLUME) >/dev/null
	@docker run --rm -v $(DATA_VOLUME):/data -v "$(CURDIR)/deploy":/backup \
		alpine sh -c "rm -rf /data/* && tar xzf /backup/$(notdir $(DATA_DUMP)) -C /data"
	@echo ">>> restored $(DATA_VOLUME) from $(DATA_DUMP)"

# ---- Diagnostics ----

# Preflight: list ONLY the missing build/deploy prerequisites (run on a new machine).
doctor: ## List missing build/deploy prerequisites (run on a new machine)
	@./tools/doctor.sh

# One-shot: install the Vercel CLI on Ubuntu/Debian (+ Node if needed) and log in.
vercel-setup: ## Install the Vercel CLI on Ubuntu (+ Node) and log in
	@./tools/install_vercel.sh

# Health: probe the prod backend the way the deployed frontend does
# (Supabase api_base pointer -> tunnel /status -> CORS -> /session/anon).
check-tunnel: ## Verify the prod backend end-to-end (tunnel + CORS + auth)
	@./tools/check_tunnel.sh $(ARGS)

# ---- Simmer ----
# Simmer targets land here once the product is built — see docs/simmer.md.
# Planned: simmer-setup, simmer-dev, simmer-build, deploy-smr-fe, simmer-status.
# Keep them under this heading so `make help` groups them automatically.
