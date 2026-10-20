# voice-ai — a fully local, air-gapped English + Hindi voice agent.
#
# The common case is `make up`: the observability stack, the STT server,
# and the agent. Then open http://localhost:7860/client.
#
# There is no `make client`: the browser client is Pipecat's own, served
# by the dev runner at /client. There is no npm step either, for the same
# reason.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PY := uv run
UV_CACHE := $(CURDIR)/.cache/uv
export UV_CACHE_DIR := $(UV_CACHE)

# The local mlx-audio server: STT and TTS from one process.
STT_PORT ?= 8020
STT_MODEL ?= mlx-community/nemotron-3.5-asr-streaming-0.6b
STT_URL := http://localhost:$(STT_PORT)

COMPOSE := docker compose

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- the other stacks on this box -----------------------------------------
#
# This machine is shared: the hear_help and newotnite stacks live here
# too. voice-ai needs the memory and the ports they hold, so they are
# stopped before voice-ai runs. **Stop only** — no rm, no prune, no
# volume changes, no reconfiguration. `make start-others` brings them
# back exactly as they were.

OTHER_PROJECTS := hear_help newotnite

.PHONY: stop-others
stop-others: ## Stop the other Docker stacks on this box (stop only, reversible)
	@for project in $(OTHER_PROJECTS); do \
		ids=$$(docker ps -q --filter "label=com.docker.compose.project=$$project"); \
		if [ -n "$$ids" ]; then \
			echo "stopping $$project ($$(echo $$ids | wc -w | tr -d ' ') containers)"; \
			docker stop $$ids >/dev/null; \
		else \
			echo "$$project: nothing running"; \
		fi; \
	done

.PHONY: start-others
start-others: ## Bring the other Docker stacks back up
	@for project in $(OTHER_PROJECTS); do \
		ids=$$(docker ps -a -q --filter "label=com.docker.compose.project=$$project"); \
		if [ -n "$$ids" ]; then \
			echo "starting $$project ($$(echo $$ids | wc -w | tr -d ' ') containers)"; \
			docker start $$ids >/dev/null; \
		else \
			echo "$$project: nothing to start"; \
		fi; \
	done

# --- setup ---------------------------------------------------------------

.PHONY: install
install: ## Create the venv and install dependencies
	uv sync --extra dev
	@# Phoenix runs on the host, in its own tool environment, so the
	@# collector can reach it. Skip it if it is already there.
	@if command -v phoenix >/dev/null 2>&1 || [ -x "$(HOME)/.local/bin/phoenix" ]; then \
		echo "arize-phoenix already installed"; \
	else \
		uv tool install "arize-phoenix==20.23.0"; \
	fi

.PHONY: models
models: ## Download every model (STT, TTS, LLM)
	bash scripts/pull_models.sh all

.PHONY: models-stt
models-stt: ## Download the STT models only
	bash scripts/pull_models.sh stt

.PHONY: models-llm
models-llm: ## Download the LLM models only
	bash scripts/pull_models.sh llm

# --- run -----------------------------------------------------------------

.PHONY: stt
stt: ## Start the local mlx-audio server (STT + TTS)
	@echo "STT server on $(STT_URL) — Ctrl-C to stop"
	$(PY) mlx_audio.server --host 127.0.0.1 --port $(STT_PORT)

.PHONY: stt-model
stt-model: ## Pre-load a model into the running STT server: make stt-model MODEL=<repo>
	@curl -sf -X POST "$(STT_URL)/v1/models?model_name=$(MODEL)" \
		&& echo "loaded $(MODEL)"

.PHONY: stt-unload
stt-unload: ## Free a resident model: make stt-unload MODEL=<repo>
	@curl -sf -X DELETE "$(STT_URL)/v1/models?model_name=$(MODEL)" \
		&& echo "released $(MODEL)"

.PHONY: run
run: stop-others ## Run the agent (browser client at http://localhost:7860/client)
	$(PY) python -m bot.bot -t webrtc

.PHONY: run-eval
run-eval: ## Run the agent for the eval harness (connects to ws://localhost:7860/ws)
	$(PY) python -m bot.bot -t websocket

.PHONY: up
up: obs-up ## Start the observability stack, the STT server and the agent
	@echo
	@echo "  Agent:    http://localhost:7860/client"
	@echo "  Phoenix:  http://localhost:6006"
	@echo "  Grafana:  http://localhost:3003"
	@echo
	@$(MAKE) stt & \
	 sleep 8 && $(MAKE) run

# --- observability --------------------------------------------------------
#
# Phoenix runs on the *host*, not in Docker. Docker Desktop's VM is
# capped at 1.93 GB on this machine and the other projects' stacks
# already hold most of it, so a Phoenix container is OOM-killed the
# moment it starts — at 768m, at 1g, and in Phoenix's own lite mode.
# The collector is small enough to stay in Docker; Phoenix is not.
#
# PHOENIX_ENABLE_PROMETHEUS is deliberately left off. Phoenix starts
# that endpoint on port 9090 with a hardcoded value and no env var to
# move it, and the host's 9090 belongs to the other stack on this
# machine. The compose Prometheus publishes 9091 instead. See
# observability/prometheus.yml.

PHOENIX_PORT ?= 6006
PHOENIX_GRPC_PORT ?= 6007
PHOENIX_DIR := $(CURDIR)/data/phoenix
PHOENIX_PID := $(PHOENIX_DIR)/phoenix.pid
PHOENIX_LOG := $(CURDIR)/logs/phoenix.log
# `uv tool install` puts the binary in ~/.local/bin, which is not always on
# PATH. Prefer the explicit path, fall back to whatever is on PATH.
PHOENIX_BIN ?= $(shell command -v phoenix 2>/dev/null || echo $(HOME)/.local/bin/phoenix)

.PHONY: phoenix
phoenix: ## Start Phoenix on the host (trace UI at http://localhost:$(PHOENIX_PORT))
	@mkdir -p $(PHOENIX_DIR) $(dir $(PHOENIX_LOG))
	@if [ -f $(PHOENIX_PID) ] && kill -0 "$$(cat $(PHOENIX_PID))" 2>/dev/null; then \
		echo "Phoenix already running (pid $$(cat $(PHOENIX_PID)))"; \
	else \
		echo "Starting Phoenix on port $(PHOENIX_PORT)..."; \
		PHOENIX_WORKING_DIR=$(PHOENIX_DIR) \
		PHOENIX_SQL_DATABASE_URL=sqlite:///$(PHOENIX_DIR)/phoenix.db \
		PHOENIX_ALLOW_EXTERNAL_RESOURCES=false \
		PHOENIX_TELEMETRY_ENABLED=false \
		PHOENIX_HOST=0.0.0.0 \
		PHOENIX_PORT=$(PHOENIX_PORT) \
		PHOENIX_GRPC_PORT=$(PHOENIX_GRPC_PORT) \
		nohup $(PHOENIX_BIN) serve > $(PHOENIX_LOG) 2>&1 & \
		echo $$! > $(PHOENIX_PID); \
	fi
	@for i in $$(seq 1 60); do \
		if curl -sf -o /dev/null http://localhost:$(PHOENIX_PORT)/; then \
			echo "Phoenix ready on http://localhost:$(PHOENIX_PORT)"; exit 0; \
		fi; \
		sleep 1; \
	done; \
	echo "Phoenix did not become ready; see $(PHOENIX_LOG)"; exit 1

.PHONY: phoenix-down
phoenix-down: ## Stop the host Phoenix
	@if [ -f $(PHOENIX_PID) ] && kill -0 "$$(cat $(PHOENIX_PID))" 2>/dev/null; then \
		kill "$$(cat $(PHOENIX_PID))" && rm -f $(PHOENIX_PID) && echo "Phoenix stopped"; \
	else \
		echo "Phoenix is not running"; \
	fi

.PHONY: obs-up
obs-up: stop-others phoenix ## Start Phoenix, the collector, Prometheus and Grafana
	$(COMPOSE) up -d

.PHONY: obs-down
obs-down: ## Stop the observability stack
	$(COMPOSE) down
	@$(MAKE) phoenix-down

.PHONY: obs-logs
obs-logs: ## Tail the observability stack's logs
	$(COMPOSE) logs -f

# --- measurement ----------------------------------------------------------

.PHONY: memory
memory: ## Measure the memory baseline and apply the tier rule
	bash scripts/memory_baseline.sh

.PHONY: apply-tier
apply-tier: ## Write the measured tier and latency into .env.example
	$(PY) python scripts/apply_tier.py

.PHONY: bench
bench: stop-others ## Benchmark STT speed and write data/stt_bench.json
	$(PY) python scripts/bench_stt.py --languages en hi

.PHONY: eval
eval: stop-others ## Run the WER/CER harness over FLEURS
	$(PY) python evals/wer_eval.py --languages en hi

.PHONY: eval-scenarios
eval-scenarios: stop-others ## Run the behavioural eval scenarios (needs the agent running)
	$(PY) python -m pipecat.evals run evals/scenarios/

# The LLM the agent will actually use: .env first, then the measured
# .env.example, which `make apply-tier` writes. Falling back to a hardcoded
# tier would warm a model the agent never loads.
LLM_MODEL ?= $(shell { cat .env .env.example 2>/dev/null; } | grep -E '^llm_model=' | head -1 | cut -d= -f2)
ifeq ($(strip $(LLM_MODEL)),)
LLM_MODEL := gemma3:4b
endif

.PHONY: warm
warm: ## Pre-load the LLM into Ollama so the first reply is not slow
	@curl -sf http://localhost:11434/api/generate \
		-d '{"model":"$${LLM_MODEL:-$(LLM_MODEL)}","prompt":"hi","stream":false}' >/dev/null \
		&& echo "LLM warm ($(LLM_MODEL))"

# --- quality -------------------------------------------------------------

.PHONY: smoke
smoke: ## Round-trip Hindi and English speech through the STT server
	$(PY) python scripts/smoke_stt.py

.PHONY: test
test: ## Run the test suite
	$(PY) pytest

# --- housekeeping ---------------------------------------------------------

.PHONY: clean
clean: ## Remove generated artifacts (models and fixtures are kept)
	rm -rf data/ evals/fixtures/audio/ evals/fixtures/wer_report.json
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	@echo "cleaned (models and the venv are kept)"
