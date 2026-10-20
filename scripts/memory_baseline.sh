#!/usr/bin/env bash
# Measure the real memory baseline, then apply the tier decision rule.
#
# The tier rule needs numbers, not assumptions:
#
#   available_for_llm_gb = 16.0 - macos_baseline - stt - tts - python - 1.5
#   >= 9.0 -> gemma3:12b  >= 3.5 -> gemma3:4b  else -> gemma3:1b + 8-bit STT
#
# The 1.5 GB reserve covers the `newotnite` and `hear_help` Docker stacks.
# They are stopped while voice-ai runs (`make stop-others`), so this
# baseline is measured with them down and the reserve is the headroom
# left for them.
#
# Every component is measured in isolation, in its own process, so the
# numbers are additive.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$REPO_ROOT/.cache/uv}"

OUT="$REPO_ROOT/data/memory_baseline.json"
mkdir -p "$REPO_ROOT/data"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  --\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m  !!\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. System facts
# ---------------------------------------------------------------------------
log "system facts"
TOTAL_BYTES=$(sysctl -n hw.memsize)
TOTAL_GB=$(python3 -c "print(round($TOTAL_BYTES/1024**3, 2))")
ok "total unified memory: ${TOTAL_GB} GB"

SWAP_USAGE=$(sysctl -n vm.swapusage 2>/dev/null || echo "unknown")
ok "swap: ${SWAP_USAGE}"

MEM_PRESSURE=$(memory_pressure 2>/dev/null | grep -i "free percentage" | head -1 || true)
ok "memory_pressure: ${MEM_PRESSURE:-unknown}"

# Used bytes, from vm_stat. On Apple Silicon the meaningful figure is
# active + wired + compressed: that is what is unavailable to new work.
VM_STAT_USED_GB=$(python3 - <<'PY'
import re, subprocess
out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
page = 4096
vals = {}
for line in out.splitlines():
    m = re.match(r"^([^:]+):\s+(\d+)\.?$", line.strip())
    if m:
        vals[m.group(1).strip()] = int(m.group(2))
# "Pages free" is scaled by a factor on some releases; fall back sanely.
def g(k):
    return vals.get(k, 0) * page / 1024**3
used = g("Pages active") + g("Pages wired down") + g("Pages occupied by compressor")
print(f"{used:.2f}")
PY
)
ok "system in use (active+wired+compressed): ${VM_STAT_USED_GB} GB"

# Top-10 resident processes. Our own stack is excluded: the baseline is
# "the machine without us", so the delta is what we cost.
TOP10_JSON=$(python3 - <<'PY'
import json, subprocess
# Anything matching these belongs to the voice-ai stack.
OURS = ("mlx_audio", "voice_ai", "uvicorn", "pipecat")
out = subprocess.run(["ps", "-A", "-o", "rss=,comm="], capture_output=True, text=True).stdout
rows = []
for line in out.splitlines():
    line = line.strip()
    if not line:
        continue
    rss, _, comm = line.partition(" ")
    try:
        rss_kb = int(rss)
    except ValueError:
        continue
    if any(o in comm for o in OURS):
        continue
    rows.append({"rss_mb": round(rss_kb / 1024, 1), "command": comm.strip()})
rows.sort(key=lambda r: -r["rss_mb"])
print(json.dumps(rows[:10]))
PY
)
ok "top-10 resident processes captured"

# ---------------------------------------------------------------------------
# 2. Component isolation
# ---------------------------------------------------------------------------
# Each measurement runs the component in its own short-lived process and
# reports that process's own RSS via psutil.
measure_self_rss() {
  # $1 = python snippet file
  uv run python "$1"
}

log "measuring Ollama resident size"
OLLAMA_RESIDENT_GB="null"
# The model the bot is configured to use, so the measurement matches
# what will actually be resident at runtime: .env first, then the
# measured .env.example, which `make apply-tier` writes.
OLLAMA_MODEL="${OLLAMA_MODEL:-$( { cat .env .env.example 2>/dev/null || true; } | grep -E '^llm_model=' | head -1 | cut -d= -f2 || true)}"
OLLAMA_MODEL="${OLLAMA_MODEL:-gemma3:4b}"
if command -v ollama >/dev/null 2>&1 && curl -sf --max-time 3 http://localhost:11434/api/tags >/dev/null; then
  MODEL="$OLLAMA_MODEL"
  if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$MODEL"; then
    # Load the model, then read back the resident size Ollama itself
    # reports. Done in one Python block: a shell pipeline here is
    # fragile, and `set -o pipefail` turns a SIGPIPE into a silent
    # abort of the whole script.
    OLLAMA_RESIDENT_GB=$(MEASURED_MODEL="$MODEL" uv run python - <<'PY'
import json, os, urllib.request

model = os.environ["MEASURED_MODEL"]
root = "http://localhost:11434"

try:
    request = urllib.request.Request(
        f"{root}/api/generate",
        data=json.dumps(
            {"model": model, "prompt": "hi", "stream": False, "keep_alive": "2m"}
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        response.read()
except Exception:
    pass

try:
    with urllib.request.urlopen(f"{root}/api/ps", timeout=30) as response:
        listing = json.load(response)
    for entry in listing.get("models", []):
        if entry.get("name") == model or entry.get("model") == model:
            size = entry.get("size", 0) / 1024**3
            print(f"{size:.2f}")
            break
    else:
        print("null")
except Exception:
    print("null")
PY
)
    ok "ollama ${MODEL} resident: ${OLLAMA_RESIDENT_GB} GB"
  else
    warn "ollama model ${MODEL} not pulled yet — skipping (run scripts/pull_models.sh llm)"
  fi
else
  warn "ollama not running — skipping"
fi

log "measuring STT server resident size (one model only)"
STT_RESIDENT_GB="null"
STT_MODEL="${STT_MODEL:-mlx-community/nemotron-3.5-asr-streaming-0.6b}"
STT_PORT="${STT_PORT:-8020}"
if [[ -d "$HOME/.cache/huggingface/hub/models--${STT_MODEL//\//--}" ]]; then
  # Done in one Python block: the server is `uv run`'s child, so the
  # shell's `$!` is uv's pid, not the server's, and measuring the wrong
  # process reports nothing.
  STT_RESIDENT_GB=$(MEASURED_STT_MODEL="$STT_MODEL" MEASURED_STT_PORT="$STT_PORT" \
    MEASURED_REPO="$REPO_ROOT" uv run python - <<'PY'
import os
import subprocess
import time
import urllib.request
import wave

import psutil

model = os.environ["MEASURED_STT_MODEL"]
port = int(os.environ["MEASURED_STT_PORT"])
repo = os.environ["MEASURED_REPO"]

log = open("/tmp/mlx_stt_server.log", "wb")
process = subprocess.Popen(
    ["uv", "run", "mlx_audio.server", "--host", "127.0.0.1", "--port", str(port)],
    stdout=log,
    stderr=subprocess.STDOUT,
    cwd=repo,
)


def wait_for_port(timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


try:
    if not wait_for_port(180):
        print("null")
    else:
        # Force the model to load, so the measurement covers the weights
        # and not just the server's runtime. A real (silent) WAV: the
        # server has to be able to decode it or the request fails and
        # the model never loads.
        with wave.open("/tmp/silence.wav", "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 16000)

        import io
        import json

        boundary = "----voiceaibaseline"
        data = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="silence.wav"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode() + open("/tmp/silence.wav", "rb").read() + b"\r\n"
        for key, value in (("model", model), ("response_format", "json")):
            data += (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
            ).encode()
        data += f"--{boundary}--\r\n".encode()

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/audio/transcriptions",
            data=data,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=900) as response:
                response.read()
        except Exception as exc:
            print(f"load failed: {exc}", file=__import__("sys").stderr)

        # Measure the server process itself, and any child it spawned.
        total = 0
        for proc in psutil.process_iter(["pid", "cmdline", "memory_info"]):
            try:
                cmdline = " ".join(proc.info["cmdline"] or [])
                if "mlx_audio.server" not in cmdline:
                    continue
                total += proc.info["memory_info"].rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        print(f"{total / 1024**3:.2f}" if total else "null")
finally:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
PY
)
  ok "mlx_audio.server resident (1 model): ${STT_RESIDENT_GB} GB"
else
  warn "${STT_MODEL} not downloaded yet — skipping (run scripts/pull_models.sh stt)"
fi

log "measuring Kokoro resident size"
TTS_RESIDENT_GB="null"
# Use `settings=`: the direct `voice=` kwarg was deprecated in Pipecat
# 0.0.105 and now raises, which would silently measure nothing.
TTS_OUT=$(uv run python - <<'PY' 2>/tmp/kokoro_measure_err.log || true
import os
import psutil
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.transcriptions.language import Language

# Measure the marginal cost of the TTS engine, not the whole interpreter.
before = psutil.Process(os.getpid()).memory_info().rss
svc = KokoroTTSService(
    settings=KokoroTTSService.Settings(voice="af_heart", language=Language.EN)
)
after = psutil.Process(os.getpid()).memory_info().rss
print(f"{(after - before) / 1024**3:.2f}")
PY
)
if [[ -n "$TTS_OUT" ]]; then
  TTS_RESIDENT_GB="$TTS_OUT"
else
  warn "kokoro measurement failed; see /tmp/kokoro_measure_err.log"
fi
ok "kokoro marginal resident: ${TTS_RESIDENT_GB} GB"

log "measuring the bot process (pipecat + services) resident size"
PYTHON_RESIDENT_GB=$(uv run python - <<'PY' 2>/dev/null || true
import os
import psutil
import pipecat
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

print(f"{psutil.Process(os.getpid()).memory_info().rss / 1024**3:.2f}")
PY
)
ok "bot process resident: ${PYTHON_RESIDENT_GB} GB"

# ---------------------------------------------------------------------------
# 3. Apply the tier rule and write the JSON
# ---------------------------------------------------------------------------
log "applying the tier decision rule"
export MEASURED_TOTAL_GB="$TOTAL_GB" \
       MEASURED_BASELINE_GB="$VM_STAT_USED_GB" \
       MEASURED_OLLAMA_GB="$OLLAMA_RESIDENT_GB" \
       MEASURED_STT_GB="$STT_RESIDENT_GB" \
       MEASURED_TTS_GB="$TTS_RESIDENT_GB" \
       MEASURED_PYTHON_GB="$PYTHON_RESIDENT_GB" \
       MEASURED_TOP10_JSON="$TOP10_JSON" \
       MEASURED_SWAP="$SWAP_USAGE" \
       MEASURED_MEM_PRESSURE="$MEM_PRESSURE" \
       MEASURED_STT_MODEL="$STT_MODEL"

uv run python - "$OUT" <<'PYEOF'
import json, os, sys

def num(key, default=None):
    v = os.environ.get(key, "")
    try:
        return float(v)
    except ValueError:
        return default

total_gb   = num("MEASURED_TOTAL_GB", 16.0)
baseline   = num("MEASURED_BASELINE_GB", 0.0) or 0.0
ollama_gb  = num("MEASURED_OLLAMA_GB")
stt_gb     = num("MEASURED_STT_GB")
tts_gb     = num("MEASURED_TTS_GB")
python_gb  = num("MEASURED_PYTHON_GB")

RESERVE_GB = 1.5   # other projects' Docker stacks + swap headroom

available = (
    total_gb
    - baseline
    - (stt_gb or 0.0)
    - (tts_gb or 0.0)
    - (python_gb or 0.0)
    - RESERVE_GB
)

# The Gemma 3 ladder. Every tier is a non-thinking model: a thinking model
# burns the latency budget on reasoning before it answers, and Ollama's
# OpenAI-compatible endpoint cannot turn the reasoning off. Sizes are the
# registry's, measured: 3.35 GB, 8.16 GB, 0.81 GB.
if available >= 9.0:
    llm_model, stt_fallback = "gemma3:12b", None
elif available >= 3.5:
    llm_model, stt_fallback = "gemma3:4b", None
else:
    llm_model = "gemma3:1b"
    stt_fallback = "mlx-community/nemotron-3.5-asr-streaming-0.6b-8bit"

report = {
    "total_gb": total_gb,
    "macos_baseline_gb": round(baseline, 2),
    "ollama_resident_gb": ollama_gb,
    "stt_resident_gb": stt_gb,
    "tts_resident_gb": tts_gb,
    "python_process_gb": python_gb,
    "reserve_gb": RESERVE_GB,
    "available_for_llm_gb": round(available, 2),
    "llm_model": llm_model,
    "stt_model": os.environ.get("MEASURED_STT_MODEL"),
    "stt_fallback_model": stt_fallback,
    "swap": os.environ.get("MEASURED_SWAP"),
    "memory_pressure": os.environ.get("MEASURED_MEM_PRESSURE"),
    "top10_resident": json.loads(os.environ.get("MEASURED_TOP10_JSON", "[]")),
}

with open(sys.argv[1], "w") as fh:
    json.dump(report, fh, indent=2)
    fh.write("\n")

print(f"  available_for_llm_gb = {report['available_for_llm_gb']}")
print(f"  llm_model           = {report['llm_model']}")
if report["stt_fallback_model"]:
    print(f"  stt_fallback_model  = {report['stt_fallback_model']}")
print(f"  wrote {sys.argv[1]}")
PYEOF
