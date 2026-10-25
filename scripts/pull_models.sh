#!/usr/bin/env bash
# Idempotently fetch every model the voice agent needs, and verify each one
# by size. Safe to re-run: existing artefacts are skipped, not re-downloaded.
#
# Usage:
#   scripts/pull_models.sh              # everything
#   scripts/pull_models.sh stt          # STT only
#   scripts/pull_models.sh tts          # TTS only
#   scripts/pull_models.sh llm          # LLM only
#   scripts/pull_models.sh llm:gemma    # the Gemma fallback ladder
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Project-local uv cache: the shared one at ~/.cache/uv has a root-owned
# subdirectory on this host that uv cannot write to.
export UV_CACHE_DIR="${UV_CACHE_DIR:-$REPO_ROOT/.cache/uv}"

# STT models. bf16 is the default (best WER); 8bit is the memory fallback.
STT_BF16="mlx-community/nemotron-3.5-asr-streaming-0.6b"
STT_8BIT="mlx-community/nemotron-3.5-asr-streaming-0.6b-8bit"
# The English tier + the A/B baseline for the WER harness.
STT_WHISPER="mlx-community/whisper-large-v3-turbo"

KOKORO_DIR="$HOME/.cache/pipecat/kokoro-onnx"
KOKORO_MODEL="$KOKORO_DIR/kokoro-v1.0.onnx"
KOKORO_VOICES="$KOKORO_DIR/voices-v1.0.bin"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  --\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  !!\033[0m %s\n' "$*" >&2; exit 1; }

# Human-readable size of a path, in MB. `-L` matters: the HuggingFace cache
# stores blobs once and symlinks to them, so a plain `du` reports ~1 MB for
# a 1.2 GB model.
size_mb() { du -smL "$1" 2>/dev/null | cut -f1; }

# --- HF models -------------------------------------------------------------
# snapshot_download is idempotent: it skips files already in the cache.
pull_hf() {
  local repo="$1" expect_mb="$2" dest
  dest="$HOME/.cache/huggingface/hub/models--${repo//\//--}"
  if [[ -d "$dest" ]] && [[ "$(size_mb "$dest")" -ge "$expect_mb" ]]; then
    ok "$repo already present ($(size_mb "$dest") MB)"
    return 0
  fi
  log "downloading $repo"
  uv run --no-project python - "$repo" <<'PY'
import sys
from huggingface_hub import snapshot_download
repo = sys.argv[1]
path = snapshot_download(repo)
print(f"  -> {path}")
PY
  local got
  got="$(size_mb "$dest")"
  [[ "$got" -ge "$expect_mb" ]] || die "$repo: expected >=${expect_mb} MB, got ${got} MB"
  ok "$repo (${got} MB)"
}

pull_stt() {
  log "STT models (MLX / Nemotron 3.5 ASR)"
  pull_hf "$STT_BF16" 1200
  pull_hf "$STT_8BIT" 700
  pull_hf "$STT_WHISPER" 1500
  pull_whisper_processor
}

# The MLX conversion of Whisper ships the weights and nothing else: the
# tokenizer and the feature extractor live in the original OpenAI repo.
# Both loaders read them from the model directory, so without them
# Whisper loads and then fails on the first transcription with
# "Processor not found".
pull_whisper_processor() {
  local repo="mlx-community/whisper-large-v3-turbo"
  local dest="$HOME/.cache/huggingface/hub/models--${repo//\//--}"
  local snapshot
  snapshot="$(ls -d "$dest"/snapshots/*/ 2>/dev/null | head -1)"
  [[ -n "$snapshot" ]] || die "$repo is not downloaded yet"
  if [[ -f "$snapshot/preprocessor_config.json" ]]; then
    ok "whisper tokenizer and feature extractor already present"
    return 0
  fi

  log "fetching the whisper tokenizer and feature extractor"
  uv run --no-project python - "$snapshot" <<'PY'
import sys
from huggingface_hub import snapshot_download

target = sys.argv[1]
snapshot_download(
    "openai/whisper-large-v3",
    local_dir=target,
    allow_patterns=[
        "preprocessor_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "added_tokens.json",
        "generation_config.json",
        "vocab.json",
        "merges.txt",
    ],
)
print(f"  -> {target}")
PY
  [[ -f "$snapshot/preprocessor_config.json" ]] \
    || die "the whisper tokenizer and feature extractor did not arrive"
  ok "whisper tokenizer and feature extractor ready"
}

# --- Kokoro (TTS) ----------------------------------------------------------
# Two builds of the same model: the MLX checkpoint the mlx-audio server
# serves (the default), and the ONNX model + voice pack Pipecat fetches
# from GitHub releases on first construction. Both are prefetched here so
# the air-gapped run never reaches the network.
TTS_MLX="mlx-community/Kokoro-82M-4bit"

pull_tts() {
  log "TTS (Kokoro MLX + ONNX + voice pack)"
  command -v espeak-ng >/dev/null 2>&1 \
    || warn "espeak-ng not on PATH — Hindi phonemization will fail. Install with: brew install espeak-ng"

  pull_hf "$TTS_MLX" 600

  # misaki's English G2P calls spacy.cli.download("en_core_web_sm") on
  # first use when the model is absent. Install it in the project venv
  # now, so the demo does not make that network call.
  log "checking the spacy English model"
  uv run python - <<'PY'
import spacy

try:
    spacy.load("en_core_web_sm")
except OSError:
    from spacy.cli import download

    download("en_core_web_sm")
    spacy.load("en_core_web_sm")
print("  -> en_core_web_sm ready")
PY

  mkdir -p "$KOKORO_DIR"
  if [[ -f "$KOKORO_MODEL" && -f "$KOKORO_VOICES" ]]; then
    ok "kokoro files already present ($(size_mb "$KOKORO_DIR") MB)"
    return 0
  fi
  log "pre-triggering kokoro model download"
  uv run python - <<'PY'
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.transcriptions.language import Language

# Constructing the service downloads the ONNX model + voice pack.
# Use `settings=` — the direct `voice=` kwarg is deprecated as of 0.0.105.
KokoroTTSService(settings=KokoroTTSService.Settings(voice="af_heart", language=Language.EN))
print("  -> kokoro files ready")
PY
  [[ -f "$KOKORO_MODEL" ]] || die "kokoro model file missing after trigger"
  [[ -f "$KOKORO_VOICES" ]] || die "kokoro voice pack missing after trigger"
  ok "kokoro ($(size_mb "$KOKORO_DIR") MB)"
}

# --- Ollama (LLM) ----------------------------------------------------------
# The default LLM: Qwen3.5 4B. The Gemma 3 ladder is the memory-tier
# fallback the rule in scripts/memory_baseline.sh can select; it is a
# separate, explicit download because most machines do not need it.
# Gemma's registry sizes are 3.35 / 8.16 / 0.81 GB.
pull_llm() {
  local models=("qwen3.5:4b")
  case "${1:-}" in
    llm:gemma) models=("gemma3:1b" "gemma3:4b" "gemma3:12b") ;;
    llm:gemma:1b) models=("gemma3:1b") ;;
    llm:gemma:4b) models=("gemma3:4b") ;;
    llm:gemma:12b) models=("gemma3:12b") ;;
    # The original names. Keep them working; they were the only rungs
    # before Qwen3.5 became the default.
    llm:1b) models=("gemma3:1b") ;;
    llm:4b) models=("gemma3:4b") ;;
    llm:12b) models=("gemma3:12b") ;;
  esac

  command -v ollama >/dev/null 2>&1 || die "ollama not on PATH"
  if ! curl -sf --max-time 3 http://localhost:11434/api/tags >/dev/null; then
    die "ollama is not running. Start it with: brew services start ollama"
  fi

  for m in "${models[@]}"; do
    if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$m"; then
      ok "$m already pulled"
      continue
    fi
    log "pulling $m"
    ollama pull "$m"
    ok "$m"
  done
}

case "${1:-all}" in
  stt) pull_stt ;;
  tts) pull_tts ;;
  llm) pull_llm "${2:-}" ;;
  llm:*) pull_llm "$1" ;;
  all) pull_stt; pull_tts; pull_llm ;;
  *) die "unknown target '$1' (use: stt | tts | llm | llm:gemma | llm:gemma:1b | llm:gemma:4b | llm:gemma:12b | all)" ;;
esac

log "done"
