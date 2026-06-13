# EdgeLane MARKET backend — convenience targets.
#
# Usage:
#   make            → same as `make help`
#   make setup      → one-time venv + pip install (only re-run if pyproject.toml changes)
#   make clean      → clear __pycache__ + stale .pyc files
#   make run        → boot dev server on port 8789 (sandbox mode if DEVMODE=true)
#   make run-prod   → flip config to DEVMODE=false then boot (production Tradier)
#   make run-dev    → flip config to DEVMODE=true then boot (sandbox Tradier)
#   make diag       → curl /diag/tradier (verifies auth + latency)
#   make status     → curl /status (pretty-printed)
#   make snapshot   → curl /snapshot/SPX (latest cached engine output)
#   make accuracy   → curl /accuracy/SPX (rolling win rate + recent outcomes)
#   make test       → run the 56 parity tests
#   make test-e2e   → run the 6 extension webhook e2e tests
#   make test-all   → parity + e2e
#   make webhook-debug → curl /webhook/debug (latest payload + history)
#   make webhook-post  → POST a synthetic GEX payload
#   make ext-version   → curl /extension/version
#   make ext-policy    → curl /extension/policy.json
#   make ui         → open market/ui/index.html in default browser
#   make stop       → kill any uvicorn on PORT
#   make logs       → tail server log (if you used `make run-bg`)
#   make run-bg     → run server in background, log to /tmp/edgelane-market.log
#
# Defaults
PORT          ?= 8789
HOST          ?= 127.0.0.1
APP            = app.main:app
CONFIG         = ../../edgelane_market.config
PY             = python3
VENV           = .venv
VENV_PY        = $(VENV)/bin/python
UI_PATH        = ../ui/index.html
LOG_FILE       = /tmp/edgelane-market.log

# --- targets ---

.PHONY: help setup clean run run-prod run-dev run-bg stop logs \
        diag status snapshot accuracy test test-e2e test-all \
        webhook-debug webhook-post ext-version ext-policy \
        ui _check_config _flip_dev _flip_prod

help:
	@echo "EdgeLane MARKET — available targets:"
	@echo ""
	@echo "  Setup:"
	@echo "    make setup        first-time install (creates venv, installs deps)"
	@echo "    make clean        clear __pycache__"
	@echo ""
	@echo "  Run:"
	@echo "    make run          boot dev server on port $(PORT) (uses current DEVMODE)"
	@echo "    make run-dev      flip DEVMODE=true then boot (sandbox)"
	@echo "    make run-prod     flip DEVMODE=false then boot (production)"
	@echo "    make run-bg       run in background, log to $(LOG_FILE)"
	@echo "    make stop         kill any uvicorn on port $(PORT)"
	@echo "    make logs         tail the background log"
	@echo ""
	@echo "  Inspect (server must be running):"
	@echo "    make diag         /diag/tradier — auth + latency check"
	@echo "    make status       /status — service health + poller state"
	@echo "    make snapshot     /snapshot/SPX — latest engine output"
	@echo "    make accuracy     /accuracy/SPX — rolling win rate"
	@echo ""
	@echo "  Tests:"
	@echo "    make test         56 parity tests (math layer == JSX engine)"
	@echo "    make test-e2e     6 extension webhook e2e tests (spins up ephemeral backend)"
	@echo "    make test-all     parity + e2e"
	@echo ""
	@echo "  Extension / Webhook (server must be running):"
	@echo "    make webhook-debug   /webhook/debug — latest payload + history"
	@echo "    make webhook-post    POST a synthetic GEX payload to /webhook/edgelane_provider_gex"
	@echo "    make ext-version     /extension/version — policy version"
	@echo "    make ext-policy      /extension/policy.json — hot-reload config"
	@echo ""
	@echo "  UI:"
	@echo "    make ui           open market/ui/index.html in browser"
	@echo ""
	@echo "Override PORT with e.g.  make run PORT=8788"

setup:
	@if [ ! -d "$(VENV)" ]; then \
	  echo ">>> creating venv at $(VENV)"; \
	  $(PY) -m venv $(VENV); \
	fi
	@echo ">>> installing in editable mode"
	@$(VENV_PY) -m pip install -e . --quiet
	@echo ">>> done. activate with:  source $(VENV)/bin/activate  (optional — make run handles this)"

clean:
	@echo ">>> clearing __pycache__"
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@echo ">>> done"

_check_config:
	@if [ ! -f "$(CONFIG)" ]; then \
	  echo "ERROR: $(CONFIG) not found. Copy edgelane_market.config.example and fill in."; \
	  exit 1; \
	fi

_flip_dev: _check_config
	@sed -i 's/^DEVMODE=.*/DEVMODE=true/' $(CONFIG)
	@echo ">>> DEVMODE=true  (sandbox Tradier, polls anytime)"

_flip_prod: _check_config
	@sed -i 's/^DEVMODE=.*/DEVMODE=false/' $(CONFIG)
	@echo ">>> DEVMODE=false (production Tradier, market hours only)"

run: _check_config clean
	@echo ">>> booting uvicorn on http://$(HOST):$(PORT)"
	@EDGELANE_MARKET_CONFIG=$(CONFIG) $(PY) -m uvicorn $(APP) --host $(HOST) --port $(PORT)

run-dev: _flip_dev run

run-prod: _flip_prod run

run-bg: _check_config clean
	@echo ">>> booting uvicorn in background (log: $(LOG_FILE))"
	@EDGELANE_MARKET_CONFIG=$(CONFIG) nohup $(PY) -m uvicorn $(APP) \
	  --host $(HOST) --port $(PORT) > $(LOG_FILE) 2>&1 &
	@sleep 2
	@echo ">>> server PID: $$(lsof -t -i:$(PORT) 2>/dev/null || ss -lntp 2>/dev/null | grep :$(PORT) | head -1)"
	@echo ">>> tail with:  make logs"

stop:
	@PID=$$(lsof -t -i:$(PORT) 2>/dev/null); \
	if [ -n "$$PID" ]; then \
	  echo ">>> killing pid $$PID"; \
	  kill -9 $$PID; \
	else \
	  echo ">>> nothing running on port $(PORT)"; \
	fi

logs:
	@tail -f $(LOG_FILE)

diag:
	@curl -s http://$(HOST):$(PORT)/diag/tradier | $(PY) -m json.tool

status:
	@curl -s http://$(HOST):$(PORT)/status | $(PY) -m json.tool

snapshot:
	@curl -s http://$(HOST):$(PORT)/snapshot/SPX | $(PY) -m json.tool

accuracy:
	@curl -s http://$(HOST):$(PORT)/accuracy/SPX | $(PY) -m json.tool

test:
	@$(PY) -m pytest tests/parity/ -q --no-header

test-e2e:
	@$(PY) -m pytest ../extension/tests/test_e2e.py -v

test-all:
	@$(PY) -m pytest tests/parity/ ../extension/tests/test_e2e.py -q --no-header

webhook-debug:
	@curl -s http://$(HOST):$(PORT)/webhook/debug | $(PY) -m json.tool

webhook-post:
	@curl -s -X POST http://$(HOST):$(PORT)/webhook/edgelane_provider_gex \
	  -H 'Content-Type: application/json' \
	  -d '{"symbol":"SPX","spot":7475.0,"put_wall_strike":7500,"put_wall_strength":"high","put_wall_net_gex":150000000000,"call_wall_strike":7530,"call_wall_strength":"medium","call_wall_net_gex":-80000000000,"gex_wall_strike":7500,"extension_version":"manual","extracted_at_ms":0}' \
	  | $(PY) -m json.tool

ext-version:
	@curl -s http://$(HOST):$(PORT)/extension/version | $(PY) -m json.tool

ext-policy:
	@curl -s http://$(HOST):$(PORT)/extension/policy.json | $(PY) -m json.tool

ui:
	@if command -v xdg-open >/dev/null 2>&1; then \
	  xdg-open "$(UI_PATH)"; \
	elif command -v cmd.exe >/dev/null 2>&1; then \
	  cmd.exe /c start "" "$$(wslpath -w $(UI_PATH))"; \
	elif command -v open >/dev/null 2>&1; then \
	  open "$(UI_PATH)"; \
	else \
	  echo "no opener found; manually open: $(UI_PATH)"; \
	fi
