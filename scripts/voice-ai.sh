#!/usr/bin/env bash
# voice-ai — the one command for this project (voice-only bare minimum).
#
# Everything the project does is a subcommand of this script: installing,
# pulling models, starting the stack, and stopping it. There is no second
# way in.
#
#   scripts/voice-ai.sh start        # Phoenix (host, 6006) -> STT server -> agent
#   scripts/voice-ai.sh stop         # everything, in reverse
#   scripts/voice-ai.sh status       # what is running, and where
#   scripts/voice-ai.sh help         # the full list
#
# The order of `start` matters: Phoenix on the host (SQLite backend, no
# PostgreSQL), then the STT server, then the agent (which needs the STT
# server). No containers, no collector, no database.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- configuration ---------------------------------------------------------

STT_PORT="${STT_PORT:-8020}"
AGENT_PORT="${AGENT_PORT:-7860}"
PHOENIX_PORT="${PHOENIX_PORT:-6006}"

RUN_DIR="$REPO_ROOT/data/run"
LOG_DIR="$REPO_ROOT/logs"
PHOENIX_DIR="$REPO_ROOT/data/phoenix"
PHOENIX_PID="$PHOENIX_DIR/phoenix.pid"
PHOENIX_LOG="$LOG_DIR/phoenix.log"
STT_LOG="$LOG_DIR/stt.log"
AGENT_LOG="$LOG_DIR/agent.log"
PHOENIX_BIN="${PHOENIX_BIN:-$(command -v phoenix 2>/dev/null || echo "$HOME/.local/bin/phoenix")}"

# --- output ----------------------------------------------------------------

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  --\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  !!\033[0m %s\n' "$*" >&2; exit 1; }

# Wait for an HTTP endpoint to answer.
wait_for() {
  local url="$1" what="$2" timeout="${3:-60}"
  local deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -sf -o /dev/null "$url"; then
      ok "$what is up"
      return 0
    fi
    sleep 1
  done
  return 1
}

# The LLM the agent is configured to use: .env first, then .env.example.
configured_llm_model() {
  { cat .env .env.example 2>/dev/null || true; } \
    | grep -E '^llm_model=' | head -1 | cut -d= -f2
}

# --- setup -----------------------------------------------------------------

cmd_install() {
  log "installing the dependencies"
  uv sync
  # Phoenix runs on the host, in its own tool environment, with its
  # default (SQLite) backend — no [pg] extra, no PostgreSQL.
  if [ -x "$HOME/.local/bin/phoenix" ] || command -v phoenix >/dev/null 2>&1; then
    local phoenix_version
    phoenix_version="$("$PHOENIX_BIN" --version 2>/dev/null || true)"
    if [ -n "$phoenix_version" ]; then
      ok "arize-phoenix already installed ($phoenix_version)"
    else
      uv tool install --force "arize-phoenix==20.23.0"
    fi
  else
    uv tool install "arize-phoenix==20.23.0"
  fi
}

cmd_models() {
  local target="${1:-all}"
  case "$target" in
    stt|tts|llm|all) bash scripts/pull_models.sh "$target" ;;
    llm:gemma|llm:gemma:1b|llm:gemma:4b|llm:gemma:12b|llm:1b|llm:4b|llm:12b)
      bash scripts/pull_models.sh "$target" ;;
    *) die "unknown target '$target' (use: stt | tts | llm | llm:gemma | llm:gemma:1b | llm:gemma:4b | llm:gemma:12b | all)" ;;
  esac
}

# --- Phoenix (host, default backend, no PostgreSQL) ------------------------

cmd_phoenix() {
  mkdir -p "$PHOENIX_DIR" "$(dirname "$PHOENIX_LOG")"
  if [ -f "$PHOENIX_PID" ] && kill -0 "$(cat "$PHOENIX_PID")" 2>/dev/null; then
    ok "Phoenix already running (pid $(cat "$PHOENIX_PID"))"
  else
    log "starting Phoenix on port $PHOENIX_PORT (host, default backend)"
    PHOENIX_WORKING_DIR="$PHOENIX_DIR" \
    PHOENIX_ALLOW_EXTERNAL_RESOURCES=false \
    PHOENIX_TELEMETRY_ENABLED=false \
    PHOENIX_HOST=0.0.0.0 \
    PHOENIX_PORT="$PHOENIX_PORT" \
      nohup "$PHOENIX_BIN" serve > "$PHOENIX_LOG" 2>&1 &
    echo $! > "$PHOENIX_PID"
  fi
  wait_for "http://localhost:$PHOENIX_PORT/" "Phoenix" 60 \
    || die "Phoenix did not become ready; see $PHOENIX_LOG"
}

cmd_phoenix_down() {
  if [ -f "$PHOENIX_PID" ] && kill -0 "$(cat "$PHOENIX_PID")" 2>/dev/null; then
    kill "$(cat "$PHOENIX_PID")" && rm -f "$PHOENIX_PID" && ok "Phoenix stopped"
  else
    warn "Phoenix is not running"
  fi
}

# --- the STT server and the agent ------------------------------------------

cmd_stt() {
  log "the mlx-audio server on $STT_PORT — Ctrl-C to stop"
  exec uv run python scripts/stt_server.py --host 127.0.0.1 --port "$STT_PORT"
}

cmd_run() {
  log "the agent on $AGENT_PORT — Ctrl-C to stop"
  exec uv run python -m bot.bot
}

# --- the whole stack -------------------------------------------------------

cmd_start() {
  local background=false with_agent=true
  while [ $# -gt 0 ]; do
    case "$1" in
      --background) background=true; shift ;;
      --no-agent) with_agent=false; shift ;;
      *) die "unknown option '$1'" ;;
    esac
  done

  mkdir -p "$RUN_DIR" "$LOG_DIR"

  # 1. Phoenix on the host (SQLite, no PostgreSQL).
  log "starting Phoenix"
  cmd_phoenix

  # 2. The STT and TTS server. The agent talks to it, so it comes first.
  log "starting the mlx-audio server on $STT_PORT"
  if curl -sf -o /dev/null "http://127.0.0.1:$STT_PORT/v1/models"; then
    ok "the mlx-audio server is already up"
  else
    ( exec uv run python scripts/stt_server.py --host 127.0.0.1 --port "$STT_PORT" ) \
      > "$STT_LOG" 2>&1 &
    echo $! > "$RUN_DIR/stt.pid"
    wait_for "http://127.0.0.1:$STT_PORT/v1/models" "the mlx-audio server" 180 \
      || die "the mlx-audio server did not come up; see $STT_LOG"
  fi

  # 3. The agent.
  if [ "$with_agent" != true ]; then
    warn "not starting the agent (--no-agent)"
    cmd_summary
    return 0
  fi

  log "starting the agent on $AGENT_PORT"
  if [ "$background" = true ]; then
    ( exec uv run python -m bot.bot ) > "$AGENT_LOG" 2>&1 &
    echo $! > "$RUN_DIR/agent.pid"
    wait_for "http://localhost:$AGENT_PORT/metrics/" "the agent" 120 \
      || die "the agent did not come up; see $AGENT_LOG"
    cmd_summary
    ok "the agent is running in the background (pid $(cat "$RUN_DIR/agent.pid"))"
    return 0
  fi

  # Foreground: the agent's own logs are the point, so it gets the terminal.
  cmd_summary
  exec uv run python -m bot.bot
}

cmd_stop() {
  # The agent. Matched on the module name, so the pid file is only a hint.
  if pkill -f "python -m bot.bot" 2>/dev/null; then
    ok "the agent stopped"
  else
    warn "the agent was not running"
  fi
  rm -f "$RUN_DIR/agent.pid"

  # The STT server, matched on the port it was given. Both launcher
  # spellings are matched so a server started by hand still stops.
  if pkill -f "scripts/stt_server.py --host 127.0.0.1 --port $STT_PORT" 2>/dev/null \
    || pkill -f "mlx_audio.server --host 127.0.0.1 --port $STT_PORT" 2>/dev/null; then
    ok "the mlx-audio server stopped"
  else
    warn "the mlx-audio server was not running"
  fi
  rm -f "$RUN_DIR/stt.pid"

  cmd_phoenix_down

  ok "voice-ai is down"
}

cmd_summary() {
  cat <<EOF

  Agent:   http://localhost:$AGENT_PORT/client
  Phoenix: http://localhost:$PHOENIX_PORT

EOF
}

cmd_status() {
  printf 'agent      %s\n' "$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:$AGENT_PORT/metrics/" 2>/dev/null || echo down)"
  printf 'phoenix    %s\n' "$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:$PHOENIX_PORT/" 2>/dev/null || echo down)"
  printf 'stt server %s\n' "$(curl -sf -o /dev/null -w '%{http_code}' "http://127.0.0.1:$STT_PORT/v1/models" 2>/dev/null || echo down)"
  printf 'ollama     %s\n' "$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:11434/api/tags" 2>/dev/null || echo down)"
}

# --- misc ------------------------------------------------------------------

cmd_warm() {
  local model
  model=$(configured_llm_model)
  [ -n "$model" ] || model="qwen3.5:4b"
  curl -sf http://localhost:11434/api/generate \
    -d "{\"model\":\"$model\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":\"30m\"}" >/dev/null \
    && ok "LLM warm ($model, resident for 30m)"
}

cmd_clean() {
  rm -rf data/
  find . -name '__pycache__' -type d -prune -exec rm -rf {} +
  ok "cleaned (models and the venv are kept)"
}

# --- help ------------------------------------------------------------------

cmd_help() {
  cat <<EOF
voice-ai — one script for the whole project (voice-only bare minimum).

  scripts/voice-ai.sh <command> [options]

Running it:
  start              Start everything, in order, waiting until each piece is ready
    --background       the agent runs in the background instead of the foreground
    --no-agent         everything except the agent
  stop               Stop everything, in reverse
  status             What is running, and where
  run                The agent alone, in the foreground
  stt                The mlx-audio server alone (STT + TTS)
  phoenix            Phoenix alone, on the host (port 6006, default backend)
  warm               Pre-load the LLM into Ollama

Setup:
  install            The venv, the dependencies and the host-side Phoenix
  models             Download the models
    stt | tts | llm | all
    llm:gemma | llm:gemma:1b | llm:gemma:4b | llm:gemma:12b
                       the Gemma fallback ladder, or one rung of it
  clean              Remove the generated artifacts
EOF
}

# --- dispatch --------------------------------------------------------------

main() {
  local command="${1:-help}"
  [ $# -gt 0 ] && shift

  case "$command" in
    start) cmd_start "$@" ;;
    stop) cmd_stop "$@" ;;
    status) cmd_status "$@" ;;
    run) cmd_run "$@" ;;
    stt) cmd_stt "$@" ;;
    phoenix) cmd_phoenix "$@" ;;
    phoenix-down) cmd_phoenix_down "$@" ;;
    install) cmd_install "$@" ;;
    models) cmd_models "$@" ;;
    warm) cmd_warm "$@" ;;
    clean) cmd_clean "$@" ;;
    help|-h|--help) cmd_help ;;
    *) die "unknown command '$command' (try: $0 help)" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
