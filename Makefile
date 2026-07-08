# EdgeLane — top-level convenience targets.
#
# Groups:
#   Frontend    build + serve the single-file HTML app
#   Backend     FastAPI market service (poller, bias engine, webhook)
#   Extension   Chrome extension webhook + policy endpoints
#   Tests       parity, e2e, all
#   Inspect     curl live endpoints on a running backend
#
# Defaults
BACKEND      = market/backend
PY           = python3
PORT         ?= 8789
HOST         ?= 127.0.0.1
CORS_PORT    ?= 8787
COPROXY      = ./tools/cors_proxy_service.sh
DEPLOY       ?= local_container     # backend target: local_container | cloud
DEPLOY_SH    = ./deploy.sh
ARGS         ?=                     # extra flags, e.g. make deploy-prod ARGS=-n
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
        setup run run-dev run-prod run-bg stop logs \
        diag status snapshot accuracy \
        test test-e2e test-all \
        webhook-debug webhook-post ext-version ext-policy \
        ui clean \
        deploy-be deploy-fe deploy-prod deploy-dry db-push db-push-dry \
        deploy-down deploy-be-down deploy-be-restart deploy-prune deploy-builder \
        deploy-data-dump deploy-data-restore \
        doctor vercel-setup check-tunnel

help:
	@echo "EdgeLane — all targets"
	@echo ""
	@echo "  Frontend (build + serve the HTML app):"
	@echo "    make build         run edge_lane_build.sh → produces edge_lane.html"
	@echo "    make build-dry     dry-run build (preview substitutions, no write)"
	@echo "    make cors          start CORS proxy on port $(CORS_PORT) (background)"
	@echo "    make cors-start    start CORS proxy via service manager (coproxy)"
	@echo "    make cors-stop     stop the CORS proxy service"
	@echo "    make cors-restart  restart the CORS proxy service"
	@echo "    make cors-delete   uninstall the CORS proxy auto-start hook"
	@echo ""
	@echo "  Backend — setup:"
	@echo "    make setup         one-time venv + pip install"
	@echo "    make clean         clear __pycache__"
	@echo ""
	@echo "  Backend — run:"
	@echo "    make run           boot uvicorn on port $(PORT) (uses current DEVMODE)"
	@echo "    make run-dev       flip DEVMODE=true then boot (sandbox Tradier, polls anytime)"
	@echo "    make run-prod      flip DEVMODE=false then boot (production Tradier)"
	@echo "    make run-bg        run in background, log to /tmp/edgelane-market.log"
	@echo "    make stop          kill any uvicorn on port $(PORT)"
	@echo "    make logs          tail the background log"
	@echo ""
	@echo "  Backend — inspect (server must be running):"
	@echo "    make diag          /diag/tradier — auth + latency check"
	@echo "    make status        /status — service health + poller state"
	@echo "    make snapshot      /snapshot/SPX — latest engine output"
	@echo "    make accuracy      /accuracy/SPX — rolling win rate"
	@echo ""
	@echo "  Extension / Webhook (server must be running):"
	@echo "    make webhook-debug /webhook/debug — latest payload + history"
	@echo "    make webhook-post  POST a synthetic GEX payload"
	@echo "    make ext-version   /extension/version — policy version"
	@echo "    make ext-policy    /extension/policy.json — hot-reload config"
	@echo ""
	@echo "  Tests:"
	@echo "    make test          56 parity tests (math layer == JSX engine)"
	@echo "    make test-e2e      6 extension webhook e2e tests"
	@echo "    make test-all      parity + e2e (62 tests)"
	@echo ""
	@echo "  UI:"
	@echo "    make ui            open market/ui/index.html in browser"
	@echo ""
	@echo "  Deploy (production):"
	@echo "    make deploy-be     backend → Docker container + Cloudflare tunnel (DEPLOY=$(DEPLOY))"
	@echo "    make deploy-fe     market UI → Vercel"
	@echo "    make deploy-prod   both backend + frontend together (+ db-push first)"
	@echo "    make deploy-dry    dry-run the full deploy (prints commands, runs nothing)"
	@echo "    make deploy-down   stop+remove the stack (keeps DB volume; ARGS=-v drops it)"
	@echo "    make deploy-be-down    remove ONLY the backend (cloudflared keeps running)"
	@echo "    make deploy-be-restart rebuild+recreate ONLY the backend (latest code)"
	@echo "    make deploy-prune  reclaim dangling images + THIS repo's build cache ($(BUILDER)); ARGS=-a = all unused images"
	@echo "    make deploy-builder    create the dedicated buildx builder if missing (auto-run by deploy targets)"
	@echo "    make deploy-data-dump     tar the DuckDB volume -> deploy/edgelane-data.tar.gz (migration)"
	@echo "    make deploy-data-restore  restore that tarball into the volume on a new host"
	@echo "    make db-push       apply Supabase migrations (idempotent)"
	@echo "    make db-push-dry   list migrations that would be applied"
	@echo "    make doctor        list missing build/deploy prerequisites (run on a new machine)"
	@echo "    make vercel-setup  install Vercel CLI on Ubuntu (+ Node) and log in"
	@echo "    make check-tunnel  verify the prod backend is reachable end-to-end (tunnel+CORS+auth)"
	@echo ""
	@echo "Override defaults:  make run PORT=8788  |  make deploy-be DEPLOY=cloud  |  make deploy-prod ARGS=-n"

# ---- Frontend ----------------------------------------------------------------

build:
	@./edge_lane_build.sh

build-dry:
	@./edge_lane_build.sh --dry-run

cors:
	@echo ">>> CORS proxy on port $(CORS_PORT) (background)"
	@$(PY) tools/cors_proxy.py &

cors-start:
	@EDGELANE_CORS_PORT=$(CORS_PORT) $(COPROXY) start

cors-stop:
	@$(COPROXY) stop

cors-restart:
	@EDGELANE_CORS_PORT=$(CORS_PORT) $(COPROXY) restart

cors-delete:
	@$(COPROXY) uninstall

# ---- Backend (delegate to market/backend/Makefile) ---------------------------

setup:
	@$(MAKE) -C $(BACKEND) setup

clean:
	@$(MAKE) -C $(BACKEND) clean

run:
	@$(MAKE) -C $(BACKEND) run PORT=$(PORT) HOST=$(HOST)

run-dev:
	@$(MAKE) -C $(BACKEND) run-dev PORT=$(PORT) HOST=$(HOST)

run-prod:
	@$(MAKE) -C $(BACKEND) run-prod PORT=$(PORT) HOST=$(HOST)

run-bg:
	@$(MAKE) -C $(BACKEND) run-bg PORT=$(PORT) HOST=$(HOST)

stop:
	@$(MAKE) -C $(BACKEND) stop PORT=$(PORT)

logs:
	@$(MAKE) -C $(BACKEND) logs

# ---- Inspect -----------------------------------------------------------------

diag:
	@$(MAKE) -C $(BACKEND) diag PORT=$(PORT) HOST=$(HOST)

status:
	@$(MAKE) -C $(BACKEND) status PORT=$(PORT) HOST=$(HOST)

snapshot:
	@$(MAKE) -C $(BACKEND) snapshot PORT=$(PORT) HOST=$(HOST)

accuracy:
	@$(MAKE) -C $(BACKEND) accuracy PORT=$(PORT) HOST=$(HOST)

# ---- Extension / Webhook -----------------------------------------------------

webhook-debug:
	@$(MAKE) -C $(BACKEND) webhook-debug PORT=$(PORT) HOST=$(HOST)

webhook-post:
	@$(MAKE) -C $(BACKEND) webhook-post PORT=$(PORT) HOST=$(HOST)

ext-version:
	@$(MAKE) -C $(BACKEND) ext-version PORT=$(PORT) HOST=$(HOST)

ext-policy:
	@$(MAKE) -C $(BACKEND) ext-policy PORT=$(PORT) HOST=$(HOST)

# ---- Tests -------------------------------------------------------------------

test:
	@$(MAKE) -C $(BACKEND) test

test-e2e:
	@$(MAKE) -C $(BACKEND) test-e2e

test-all:
	@$(MAKE) -C $(BACKEND) test-all

# ---- UI ----------------------------------------------------------------------

ui:
	@$(MAKE) -C $(BACKEND) ui

# ---- Deploy ------------------------------------------------------------------
# Frontend (market UI) → Vercel; backend (FastAPI + Torque) → Docker container
# behind a Cloudflare tunnel. See deploy.sh + deploy/ for config.

deploy-be: deploy-builder
	@BUILDX_BUILDER=$(BUILDER) $(DEPLOY_SH) -b --target $(DEPLOY) $(ARGS)

deploy-fe:
	@$(DEPLOY_SH) -f $(ARGS)

deploy-prod: deploy-builder
	@BUILDX_BUILDER=$(BUILDER) $(DEPLOY_SH) --target $(DEPLOY) $(ARGS)

deploy-dry:
	@$(DEPLOY_SH) --target $(DEPLOY) -n $(ARGS)

# Teardown the whole stack (backend + cloudflared + network). The named
# edgelane-data volume (DuckDB) is KEPT; add ARGS=-v to also drop it.
deploy-down:
	@$(COMPOSE) down $(ARGS)

# Remove ONLY the backend container; leaves cloudflared (and the tunnel) running.
deploy-be-down:
	@$(COMPOSE) rm -sf edgelane-backend

# Rebuild + recreate ONLY the backend (latest code), without touching cloudflared.
deploy-be-restart: deploy-builder
	@BUILDX_BUILDER=$(BUILDER) $(COMPOSE) up -d --no-deps --build edgelane-backend

# Ensure the dedicated buildx builder exists AND boots (self-healing). Prereq of
# every backend build target, so you never run it by hand. On Docker Desktop/WSL2
# the builder's backing container gets a stale Docker-Desktop bind-mount across a
# reboot and won't boot (`Exited 127`); a plain `inspect` still succeeds, so we
# must actively `--bootstrap` and, if that fails, rm + recreate a fresh one.
# Removing it entirely (undo isolation): docker buildx rm $(BUILDER)
deploy-builder:
	@docker buildx inspect --bootstrap $(BUILDER) >/dev/null 2>&1 || { \
		docker buildx rm $(BUILDER) >/dev/null 2>&1 || true; \
		docker buildx create --name $(BUILDER) --driver docker-container --bootstrap >/dev/null; \
	}

# Reclaim disk. Two pools: (1) dangling (untagged) images left by rebuilds, and
# (2) THIS repo's BuildKit cache — scoped to the $(BUILDER) instance, so other
# projects' cache on the shared `default` builder is untouched. ARGS=-a prunes
# all unused images (more aggressive); it does not affect the builder-cache sweep.
deploy-prune:
	@docker image prune -f $(ARGS)
	@docker buildx prune --builder $(BUILDER) -f

# Machine migration — DuckDB volume in/out. Dump tars the volume into
# $(DATA_DUMP) (gitignored); copy that file to the new host and run restore
# there BEFORE `make deploy-be` so the fresh stack picks up the history.
deploy-data-dump:
	@docker run --rm -v $(DATA_VOLUME):/data:ro -v "$(CURDIR)/deploy":/backup \
		alpine tar czf /backup/$(notdir $(DATA_DUMP)) -C /data .
	@echo ">>> wrote $(DATA_DUMP) ($$(du -h $(DATA_DUMP) | cut -f1))"

deploy-data-restore:
	@test -f $(DATA_DUMP) || { echo "missing $(DATA_DUMP) — copy it here first"; exit 1; }
	@docker volume create $(DATA_VOLUME) >/dev/null
	@docker run --rm -v $(DATA_VOLUME):/data -v "$(CURDIR)/deploy":/backup \
		alpine sh -c "rm -rf /data/* && tar xzf /backup/$(notdir $(DATA_DUMP)) -C /data"
	@echo ">>> restored $(DATA_VOLUME) from $(DATA_DUMP)"

# Supabase schema — idempotent push of supabase/migrations/*.sql.
db-push:
	@$(PY) tools/db_push.py $(ARGS)

db-push-dry:
	@$(PY) tools/db_push.py --dry-run

# Preflight: list ONLY the missing build/deploy prerequisites (run on a new machine).
doctor:
	@./tools/doctor.sh

# One-shot: install the Vercel CLI on Ubuntu/Debian (+ Node if needed) and log in.
vercel-setup:
	@./tools/install_vercel.sh

# Health: probe the prod backend the way the deployed frontend does
# (Supabase api_base pointer -> tunnel /status -> CORS -> /session/anon).
check-tunnel:
	@./tools/check_tunnel.sh $(ARGS)
