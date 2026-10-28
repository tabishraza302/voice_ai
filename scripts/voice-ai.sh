#!/usr/bin/env bash
# voice-ai — the one command for this project.
#
# Everything the project does is a subcommand of this script: starting
# the stack, stopping it, pulling the models, running the measurements,
# running the tests. There is no second way in, so there is no second
# implementation to drift.
#
#   scripts/voice-ai.sh start        # everything, in order, with readiness checks
#   scripts/voice-ai.sh stop         # everything, in reverse
#   scripts/voice-ai.sh status        # what is running, and where
#   scripts/voice-ai.sh help         # the full list
#
# The order of `start` matters: Phoenix on the host, then the container stack,
# then the STT server, then the agent (which needs the STT server).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- configuration ---------------------------------------------------------

STT_PORT="${STT_PORT:-8020}"
STT_MODEL="${STT_MODEL:-mlx-community/nemotron-3.5-asr-streaming-0.6b}"
AGENT_PORT="${AGENT_PORT:-7860}"
PHOENIX_PORT="${PHOENIX_PORT:-6006}"
PHOENIX_GRPC_PORT="${PHOENIX_GRPC_PORT:-6007}"
PROMETHEUS_PORT="${PROMETHEUS_PORT:-9091}"
GRAFANA_PORT="${GRAFANA_PORT:-3003}"
COLLECTOR_PORT="${COLLECTOR_PORT:-9464}"
POSTGRES_PORT="${POSTGRES_PORT:-5439}"
CONFIGURED_WEB_PORT="$(grep -E '^[[:space:]]*(web_port|WEB_PORT)=' "$REPO_ROOT/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '"\047[:space:]' || true)"
WEB_PORT="${WEB_PORT:-${CONFIGURED_WEB_PORT:-3001}}"
# The STT lookahead belongs to the model deployment, not the agent. A blank
# value keeps the checkpoint's default; scripts/stt_server.py validates it.
CONFIGURED_STT_LOOKAHEAD="$(grep -E '^[[:space:]]*(stt_lookahead_frames|STT_LOOKAHEAD_FRAMES)=' "$REPO_ROOT/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '"\047[:space:]' || true)"
STT_LOOKAHEAD_FRAMES="${STT_LOOKAHEAD_FRAMES:-$CONFIGURED_STT_LOOKAHEAD}"
export WEB_PORT
export STT_LOOKAHEAD_FRAMES
export POSTGRES_PORT

RUN_DIR="$REPO_ROOT/data/run"
LOG_DIR="$REPO_ROOT/logs"
PHOENIX_DIR="$REPO_ROOT/data/phoenix"
PHOENIX_PID="$PHOENIX_DIR/phoenix.pid"
PHOENIX_LOG="$LOG_DIR/phoenix.log"
STT_LOG="$LOG_DIR/stt.log"
AGENT_LOG="$LOG_DIR/agent.log"
WEB_LOG="$LOG_DIR/web.log"
WEB_PID="$RUN_DIR/web.pid"
WEB_BIN="$REPO_ROOT/web/node_modules/next/dist/bin/next"
OTHER_PROJECTS=(${OTHER_DOCKER_PROJECTS:-})
PHOENIX_BIN="${PHOENIX_BIN:-$(command -v phoenix 2>/dev/null || echo "$HOME/.local/bin/phoenix")}"

# --- output ----------------------------------------------------------------

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  --\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  !!\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$WEB_PORT" =~ ^[0-9]+$ ]] && (( WEB_PORT >= 1 && WEB_PORT <= 65535 )) \
  || die "WEB_PORT must be an integer between 1 and 65535"

web_pid_is_owned() {
  [ -f "$WEB_PID" ] || return 1
  local pid started command recorded_started
  IFS='|' read -r pid recorded_started _ < "$WEB_PID" || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  started="$(ps -p "$pid" -o lstart= 2>/dev/null | sed 's/^ *//')"
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [ -n "$started" ] && [ "$started" = "$recorded_started" ] || return 1
  case "$command" in *"$WEB_BIN"*) return 0 ;; *) return 1 ;; esac
}

web_port_is_listening() {
  lsof -nP -iTCP:"$WEB_PORT" -sTCP:LISTEN -t >/dev/null 2>&1
}

start_web_background() {
  if web_pid_is_owned; then
    ok "the web studio is already running (pid $(cut -d'|' -f1 "$WEB_PID"))"
    return 0
  fi
  rm -f "$WEB_PID"
  web_port_is_listening && die "port $WEB_PORT is already in use; refusing to start the web studio"
  [ -x "$WEB_BIN" ] || die "Next.js is not installed in web/node_modules; run the web setup first"
  log "starting the Next.js web studio on $WEB_PORT"
  ( cd "$REPO_ROOT/web" && exec node "$WEB_BIN" dev --hostname 127.0.0.1 -p "$WEB_PORT" ) > "$WEB_LOG" 2>&1 &
  local pid="$!" started attempt
  for attempt in $(seq 1 10); do
    started="$(ps -p "$pid" -o lstart= 2>/dev/null | sed 's/^ *//')"
    [ -n "$started" ] && break
    sleep 0.1
  done
  [ -n "$started" ] || die "could not record the web studio process"
  printf '%s|%s|%s\n' "$pid" "$started" "$WEB_BIN" > "$WEB_PID"
}

stop_web() {
  if ! web_pid_is_owned; then
    rm -f "$WEB_PID"
    warn "no project-owned web studio process is running"
    return 0
  fi
  local pid
  pid="$(cut -d'|' -f1 "$WEB_PID")"
  kill "$pid" 2>/dev/null || true
  rm -f "$WEB_PID"
  ok "the project-owned web studio stopped"
}

wait_for_postgres() {
  local attempt
  for attempt in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U voice_ai -d voice_ai >/dev/null 2>&1; then
      ok "PostgreSQL is ready"
      return 0
    fi
    sleep 1
  done
  return 1
}

# Wait for an HTTP endpoint to answer. Readiness, not a sleep: the whole
# point is that the agent never starts against a server that is not there.
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

# The LLM the agent is configured to use: .env first, then the measured
# .env.example, which `apply-tier` writes.
configured_llm_model() {
  { cat .env .env.example 2>/dev/null || true; } \
    | grep -E '^llm_model=' | head -1 | cut -d= -f2
}

# --- setup -----------------------------------------------------------------

cmd_install() {
  log "installing the dependencies"
  uv sync --extra dev
  # Phoenix runs on the host, in its own tool environment, so the
  # collector can reach it. Skip it if it is already there.
  if command -v phoenix >/dev/null 2>&1 || [ -x "$HOME/.local/bin/phoenix" ]; then
    ok "arize-phoenix already installed"
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

# --- other local container stacks (optional) --------------------------------
#
# If OTHER_DOCKER_PROJECTS is defined in your environment, voice-ai can
# pause those container stacks during runs to free host ports and memory.

cmd_others_stop() {
  [ ${#OTHER_PROJECTS[@]} -gt 0 ] || return 0
  for project in "${OTHER_PROJECTS[@]}"; do
    ids=$(docker ps -q --filter "label=com.docker.compose.project=$project")
    if [ -n "$ids" ]; then
      count=$(echo "$ids" | wc -w | tr -d ' ')
      log "stopping $project ($count containers)"
      docker stop $ids >/dev/null
      ok "$project stopped"
    else
      warn "$project: nothing running"
    fi
  done
}

cmd_others_start() {
  [ ${#OTHER_PROJECTS[@]} -gt 0 ] || return 0
  for project in "${OTHER_PROJECTS[@]}"; do
    ids=$(docker ps -a -q --filter "label=com.docker.compose.project=$project")
    if [ -n "$ids" ]; then
      count=$(echo "$ids" | wc -w | tr -d ' ')
      log "starting $project ($count containers)"
      docker start $ids >/dev/null
      ok "$project started"
    else
      warn "$project: nothing to start"
    fi
  done
}

# --- observability ---------------------------------------------------------

cmd_phoenix() {
  mkdir -p "$PHOENIX_DIR" "$(dirname "$PHOENIX_LOG")"
  if [ -f "$PHOENIX_PID" ] && kill -0 "$(cat "$PHOENIX_PID")" 2>/dev/null; then
    ok "Phoenix already running (pid $(cat "$PHOENIX_PID"))"
  else
    log "starting Phoenix on port $PHOENIX_PORT"
    PHOENIX_WORKING_DIR="$PHOENIX_DIR" \
    PHOENIX_SQL_DATABASE_URL="sqlite:///$PHOENIX_DIR/phoenix.db" \
    PHOENIX_ALLOW_EXTERNAL_RESOURCES=false \
    PHOENIX_TELEMETRY_ENABLED=false \
    PHOENIX_HOST=0.0.0.0 \
    PHOENIX_PORT="$PHOENIX_PORT" \
    PHOENIX_GRPC_PORT="$PHOENIX_GRPC_PORT" \
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

cmd_obs_up() {
  cmd_phoenix
  log "starting the observability stack"
  docker compose up -d
  wait_for "http://localhost:$PROMETHEUS_PORT/-/ready" "Prometheus" 60 \
    || die "Prometheus did not become ready; docker compose logs prometheus"
  wait_for "http://localhost:$GRAFANA_PORT/api/health" "Grafana" 60 \
    || die "Grafana did not become healthy; docker compose logs grafana"
  wait_for "http://localhost:$COLLECTOR_PORT/metrics" "the collector's metrics exporter" 60 \
    || die "the collector did not come up; docker compose logs otel-collector"
  wait_for_postgres || die "PostgreSQL did not become ready; docker compose logs postgres"
}

cmd_obs_down() {
  docker compose down
  cmd_phoenix_down
}

cmd_obs_logs() {
  docker compose logs -f
}

# --- the STT server and the agent ------------------------------------------

cmd_stt() {
  log "the mlx-audio server on $STT_PORT — Ctrl-C to stop"
  exec uv run python scripts/stt_server.py --host 127.0.0.1 --port "$STT_PORT"
}

cmd_stt_model() {
  [ -n "${MODEL:-}" ] || die "set MODEL=<repo>, e.g. MODEL=$STT_MODEL"
  curl -sf -X POST "http://localhost:$STT_PORT/v1/models?model_name=$MODEL" \
    && echo "loaded $MODEL"
}

cmd_stt_unload() {
  [ -n "${MODEL:-}" ] || die "set MODEL=<repo>, e.g. MODEL=$STT_MODEL"
  curl -sf -X DELETE "http://localhost:$STT_PORT/v1/models?model_name=$MODEL" \
    && echo "released $MODEL"
}

cmd_run() {
  log "the agent on $AGENT_PORT — Ctrl-C to stop"
  exec uv run python -m bot.bot
}

# --- the whole stack -------------------------------------------------------

cmd_start() {
  local background=false with_agent=true keep_others=false with_web=false
  while [ $# -gt 0 ]; do
    case "$1" in
      --background) background=true; shift ;;
      --no-agent) with_agent=false; shift ;;
      --web) with_web=true; shift ;;
      --keep-others) keep_others=true; shift ;;
      *) die "unknown option '$1'" ;;
    esac
  done

  mkdir -p "$RUN_DIR" "$LOG_DIR"

  # 1. Other local stacks (if specified via OTHER_DOCKER_PROJECTS).
  if [ ${#OTHER_PROJECTS[@]} -gt 0 ]; then
    if [ "$keep_others" = true ]; then
      warn "leaving other project stacks alone (--keep-others)"
    else
      log "stopping other project stacks"
      cmd_others_stop
    fi
  fi
  # 2. Phoenix, on the host.
  log "starting Phoenix"
  cmd_phoenix

  # 3. The observability stack.
  log "starting the observability stack"
  docker compose up -d
  wait_for "http://localhost:$PROMETHEUS_PORT/-/ready" "Prometheus" 60 \
    || die "Prometheus did not become ready; docker compose logs prometheus"
  wait_for "http://localhost:$GRAFANA_PORT/api/health" "Grafana" 60 \
    || die "Grafana did not become healthy; docker compose logs grafana"
  wait_for "http://localhost:$COLLECTOR_PORT/metrics" "the collector's metrics exporter" 60 \
    || die "the collector did not come up; docker compose logs otel-collector"
  wait_for_postgres || die "PostgreSQL did not become ready; docker compose logs postgres"

  # Web studio (if requested via --web)
  if [ "$with_web" = true ]; then
    start_web_background
    wait_for "http://localhost:$WEB_PORT" "the web studio" 30 || warn "the web studio is still compiling; see $WEB_LOG"
  fi

  # 4. The STT and TTS server. The agent talks to it, so it comes first.
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

  # 5. The agent.
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


cmd_web() {
  log "starting the Next.js web studio on http://localhost:$WEB_PORT"
  [ -x "$WEB_BIN" ] || die "Next.js is not installed in web/node_modules; run the web setup first"
  web_port_is_listening && die "port $WEB_PORT is already in use; refusing to start the web studio"
  cd "$REPO_ROOT/web"
  exec node "$WEB_BIN" dev --hostname 127.0.0.1 -p "$WEB_PORT"
}
cmd_stop() {
  local start_others=false
  while [ $# -gt 0 ]; do
    case "$1" in
      --start-others) start_others=true; shift ;;
      *) die "unknown option '$1'" ;;
    esac
  done

  # The agent. Matched on the module name, so the pid file is only a
  # hint: a process started by hand is stopped too.
  if pkill -f "python -m bot.bot" 2>/dev/null; then
    ok "the agent stopped"
  else
    warn "the agent was not running"
  fi
  rm -f "$RUN_DIR/agent.pid"

  # The STT server, matched on the port it was given. Both launchers are
  # matched so a server started by hand before the wrapper existed still
  # stops; whoever owns the port is the process this script started.
  if pkill -f "scripts/stt_server.py --host 127.0.0.1 --port $STT_PORT" 2>/dev/null \
    || pkill -f "mlx_audio.server --host 127.0.0.1 --port $STT_PORT" 2>/dev/null; then
    ok "the mlx-audio server stopped"
  else
    warn "the mlx-audio server was not running"
  fi
  rm -f "$RUN_DIR/stt.pid"
  stop_web


  cmd_obs_down

  if [ "$start_others" = true ] && [ ${#OTHER_PROJECTS[@]} -gt 0 ]; then
    log "starting other project stacks"
    cmd_others_start
  fi

  ok "voice-ai is down"
}

cmd_summary() {
  cat <<EOF

  Studio:     http://localhost:$WEB_PORT
  Agent:      http://localhost:$AGENT_PORT/client
  Phoenix:    http://localhost:$PHOENIX_PORT
  Grafana:    http://localhost:$GRAFANA_PORT
  Prometheus: http://localhost:$PROMETHEUS_PORT

EOF
}

cmd_status() {
  printf 'agent      %s\n' "$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:$AGENT_PORT/metrics/" 2>/dev/null || echo down)"
  printf 'phoenix    %s\n' "$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:$PHOENIX_PORT/" 2>/dev/null || echo down)"
  printf 'grafana    %s\n' "$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:$GRAFANA_PORT/api/health" 2>/dev/null || echo down)"
  printf 'prometheus %s\n' "$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:$PROMETHEUS_PORT/-/ready" 2>/dev/null || echo down)"
  printf 'stt server %s\n' "$(curl -sf -o /dev/null -w '%{http_code}' "http://127.0.0.1:$STT_PORT/v1/models" 2>/dev/null || echo down)"
  printf 'ollama     %s\n' "$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:11434/api/tags" 2>/dev/null || echo down)"
  printf 'studio     %s\n' "$(curl -sf -o /dev/null -w '%{http_code}' "http://localhost:$WEB_PORT" 2>/dev/null || echo down)"
  printf 'postgres   %s\n' "$(docker compose exec -T postgres pg_isready -U voice_ai -d voice_ai >/dev/null 2>&1 && echo 200 || echo down)"
  echo
  echo "containers:"
  docker compose ps --format '  {{.Name}}  {{.State}}  {{.Status}}' 2>/dev/null || true
  if [ ${#OTHER_PROJECTS[@]} -gt 0 ]; then
    echo "other projects:"
    for project in "${OTHER_PROJECTS[@]}"; do
      count=$(docker ps -q --filter "label=com.docker.compose.project=$project" | wc -w | tr -d ' ')
      echo "  $project: $count running"
    done
  fi
}

# --- measurement and quality ------------------------------------------------

cmd_memory() { bash scripts/memory_baseline.sh; }
cmd_apply_tier() { uv run python scripts/apply_tier.py; }
cmd_bench() { uv run python scripts/bench_stt.py --languages en hi "$@"; }
cmd_lookahead_bench() { uv run python scripts/bench_stt_lookahead.py "$@"; }
cmd_tts_bench() { uv run python scripts/bench_tts.py --backend "${1:-both}"; }
cmd_turn_bench() { uv run python scripts/bench_turn.py "$@"; }
cmd_llm_bench() { uv run python scripts/bench_llm_quant.py "$@"; }
cmd_eval() {
  uv run python evals/wer_eval.py --languages en hi --models nemotron nemotron-8bit whisper
}

cmd_profile_bench() {
  uv run python scripts/bench_agent_profiles.py
}
cmd_eval_scenarios() { uv run python -m pipecat.evals run evals/scenarios/; }
cmd_smoke() { uv run python scripts/smoke_stt.py; }
cmd_test() { uv run pytest "$@"; }
cmd_verify() {
  exec uv run --frozen python "$REPO_ROOT/scripts/verify.py" "$@"
}
cmd_schema() {
  local sub="${1:-}"
  [ $# -gt 0 ] && shift
  case "$sub" in
    init)
      exec uv run python -m platform_api.cli init-db "$@"
      ;;
    *)
      die "usage: $0 schema init"
      ;;
  esac
}

cmd_warm() {
  local model
  model=$(configured_llm_model)
  [ -n "$model" ] || model="qwen3.5:4b"
  curl -sf http://localhost:11434/api/generate \
    -d "{\"model\":\"$model\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":\"30m\"}" >/dev/null \
    && ok "LLM warm ($model, resident for 30m)"
}

cmd_clean() {
  rm -rf data/ evals/fixtures/audio/ evals/fixtures/wer_report.json
  find . -name '__pycache__' -type d -prune -exec rm -rf {} +
  ok "cleaned (models and the venv are kept)"
}

# --- help ------------------------------------------------------------------

cmd_help() {
  cat <<EOF
voice-ai — one script for the whole project.

  scripts/voice-ai.sh <command> [options]

Running it:
  start              Start everything, in order, waiting until each piece is ready
    --background       the agent runs in the background instead of the foreground
    --no-agent         everything except the agent
    --web              start the Next.js web studio in the background too
    --keep-others      leave the other projects' stacks alone
  stop               Stop everything, in reverse
    --start-others     bring the other projects' stacks back up too
  status             What is running, and where
  run                The agent alone, in the foreground
  run-eval           The agent alone, for the eval harness
  stt                The mlx-audio server alone (STT + TTS)
  stt-model          Load a model into the running server (MODEL=<repo>)
  stt-unload         Release a model from the running server (MODEL=<repo>)
  phoenix            Phoenix alone, on the host
  phoenix-down       Stop Phoenix
  obs-up             Phoenix, the collector, Prometheus and Grafana
  obs-down           Stop the observability stack
  obs-logs           Follow the containers' logs

Setup:
  install            The venv, the dependencies and the host-side Phoenix
  models             Download the models
    stt | tts | llm | all
    llm:gemma | llm:gemma:1b | llm:gemma:4b | llm:gemma:12b
                       the Gemma fallback ladder, or one rung of it

Measuring:
  memory             The memory baseline, and the LLM tier it selects
  apply-tier         Write the measured tier and latency into .env.example
  bench              STT speed: real-time factor and time-to-final-transcript
  lookahead-bench    Streaming WER/CER per lookahead (uses the eval fixtures)
  tts-bench          TTS speed: time to first audio, per backend
    kokoro | mlx-audio | both
  turn-bench         One turn, stage by stage: VAD, transcript, LLM, TTS
    --audio FILE  --backend kokoro|mlx-audio  --runs N
    --deadline SECS (0 = off)  --out FILE
  llm-bench          One LLM across the quantizations Ollama publishes
    --tags TAG...  --runs N
  profile-bench      Six bounded local English/Hindi preset prompt checks
  eval               FLEURS WER/CER, per model and per normalization
  eval-scenarios     The behavioural evals (needs the agent running)
  warm               Pre-load the LLM into Ollama

Quality:
  smoke              Synthesise speech, transcribe it back
  test               The regression suite
  verify             Strict qualification gates (prepare|unit|postgres|browser|voice|release)
  schema             Schema management commands (schema init)
  clean              Remove the generated artifacts

Other local stacks (optional, via OTHER_DOCKER_PROJECTS env var):
  others stop        Stop specified container stacks
  others start       Start specified container stacks
  web                Start the Next.js Web Studio on port 3001
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
    web) cmd_web "$@" ;;
    run-eval) cmd_run "$@" ;;
    stt) cmd_stt "$@" ;;
    stt-model) cmd_stt_model "$@" ;;
    stt-unload) cmd_stt_unload "$@" ;;
    phoenix) cmd_phoenix "$@" ;;
    phoenix-down) cmd_phoenix_down "$@" ;;
    obs-up) cmd_obs_up "$@" ;;
    obs-down) cmd_obs_down "$@" ;;
    obs-logs) cmd_obs_logs "$@" ;;
    install) cmd_install "$@" ;;
    models) cmd_models "$@" ;;
    memory) cmd_memory "$@" ;;
    apply-tier) cmd_apply_tier "$@" ;;
    bench) cmd_bench "$@" ;;
    lookahead-bench) cmd_lookahead_bench "$@" ;;
    tts-bench) cmd_tts_bench "$@" ;;
    turn-bench) cmd_turn_bench "$@" ;;
    llm-bench) cmd_llm_bench "$@" ;;
    eval) cmd_eval "$@" ;;
    profile-bench) cmd_profile_bench "$@" ;;
    eval-scenarios) cmd_eval_scenarios "$@" ;;
    warm) cmd_warm "$@" ;;
    smoke) cmd_smoke "$@" ;;
    test) cmd_test "$@" ;;
    verify) cmd_verify "$@" ;;
    schema) cmd_schema "$@" ;;
    clean) cmd_clean "$@" ;;
    others)
      case "${1:-}" in
        stop) cmd_others_stop ;;
        start) cmd_others_start ;;
        *) die "usage: $0 others {stop|start}" ;;
      esac
      ;;
    help|-h|--help) cmd_help ;;
    *) die "unknown command '$command' (try: $0 help)" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
