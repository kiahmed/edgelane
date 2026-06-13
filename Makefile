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
SERVE_PORT   ?= 8080

.PHONY: help \
        build build-dry serve cors \
        setup run run-dev run-prod run-bg stop logs \
        diag status snapshot accuracy \
        test test-e2e test-all \
        webhook-debug webhook-post ext-version ext-policy \
        ui clean

help:
	@echo "EdgeLane — all targets"
	@echo ""
	@echo "  Frontend (build + serve the HTML app):"
	@echo "    make build         run edge_lane_build.sh → produces edge_lane.html"
	@echo "    make build-dry     dry-run build (preview substitutions, no write)"
	@echo "    make serve         python http.server on port $(SERVE_PORT)"
	@echo "    make cors          start CORS proxy on port $(CORS_PORT) (background)"
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
	@echo "Override defaults:  make run PORT=8788  |  make serve SERVE_PORT=3000"

# ---- Frontend ----------------------------------------------------------------

build:
	@./edge_lane_build.sh

build-dry:
	@./edge_lane_build.sh --dry-run

serve:
	@echo ">>> serving on http://localhost:$(SERVE_PORT)/edge_lane.html"
	@$(PY) -m http.server $(SERVE_PORT)

cors:
	@echo ">>> CORS proxy on port $(CORS_PORT) (background)"
	@$(PY) tools/cors_proxy.py &

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
