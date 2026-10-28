# voice-ai

A **fully local, air-gapped** English + Hindi voice agent with full-stack AI observability, built for Apple Silicon.

Every turn produces an OpenTelemetry trace in Phoenix, a set of Prometheus metrics, and a pre-configured Grafana dashboard. Nothing in the agent talks to the internet at runtime: STT, LLM, and TTS run entirely on-device, and the observability stack runs locally.

```bash
scripts/voice-ai.sh install && scripts/voice-ai.sh models && scripts/voice-ai.sh start
# then open http://localhost:7860/client
```

---

## What It Is

A personal project building a responsive bilingual voice agent that runs entirely on one Apple Silicon Mac (tested on an M5 MacBook Air with 16 GB unified memory).

| Subsystem | Technology |
|---|---|
| **STT** | NVIDIA Nemotron 3.5 ASR Streaming 0.6B (MLX on Metal GPU via realtime WebSocket) |
| **LLM** | Qwen3.5 4B (`qwen3.5:4b`, thinking suppressed) via Ollama |
| **TTS** | Kokoro-82M (Metal GPU via `mlx-audio`, or in-process ONNX CPU) |
| **Orchestration** | Pipecat 1.13.0 |
| **Transport** | Serverless WebRTC via Pipecat prebuilt client |
| **Traces** | OpenTelemetry SDK → Local Collector → Arize Phoenix |
| **Metrics** | Prometheus + Grafana dashboard |
| **Evaluation** | FLEURS WER/CER quality harness + Pipecat scenario evals |

---

## Prerequisites

- **Hardware**: Apple Silicon Mac (M1/M2/M3/M4/M5), unified memory ≥ 16 GB recommended.
- **Operating System**: macOS 14+ (Sonoma, Sequoia).
- **Package Manager**: [uv](https://docs.astral.sh/uv/) (`brew install uv`).
- **Containers**: Docker Desktop running (for OpenTelemetry Collector, Prometheus, and Grafana).
- **LLM Engine**: [Ollama](https://ollama.ai) (`brew install ollama` and `ollama serve`).
- **Phonemizer**: `brew install espeak-ng` (required for Kokoro Hindi phonemization).
- **Audio**: Wired headphones strongly recommended to avoid laptop speaker-to-microphone acoustic coupling.

---

## 5-Minute Quickstart

### 1. Install Dependencies & Tools
```bash
scripts/voice-ai.sh install
```
Installs Python dependencies via `uv` and sets up the host-side Arize Phoenix environment.

### 2. Download Model Weights
```bash
scripts/voice-ai.sh models
```
Fetches the STT, TTS, and LLM model checkpoints locally. The demo runs entirely with the network disconnected after this step.

### 3. Start the Full Stack
```bash
scripts/voice-ai.sh start
```
Brings up Phoenix on the host, the container stack (OpenTelemetry Collector, Prometheus, Grafana), the local `mlx-audio` server, and the voice agent, verifying readiness at each stage.

### 4. Talk to the Agent
Open **http://localhost:7860/client** in your browser. Speak in English or Hindi; the agent detects the language and responds in the same language.

### Useful URLs

| Service | URL | Notes |
|---|---|---|
| **Agent Web Client** | http://localhost:7860/client | Voice & text interface |
| **STT Mode Settings** | http://localhost:7860/settings | Switch realtime WebSocket vs batch HTTP |
| **Phoenix Traces** | http://localhost:6006 | Span waterfall, turn latency attribution |
| **Grafana Dashboard** | http://localhost:3003 | Default login: `admin` / `admin` |
| **Prometheus** | http://localhost:9091 | Raw series and query interface |
| **Agent Metrics** | http://localhost:7860/metrics/ | Prometheus scrape endpoint |

To stop the entire stack cleanly:
```bash
scripts/voice-ai.sh stop
```

### Local Agent Studio

The optional browser studio and its `/api` endpoints are intended for one
trusted user on the same machine. They have no sign-in, authorization, or
multi-user isolation. Keep the Studio, agent API, and database bound to
loopback. The API lets the browser edit local agent profiles and
prompts. The browser sends the selected saved profile id with its WebRTC
request, and that profile applies to that session only. Prebuilt clients with
no profile id keep the measured `.env` settings. A control-page STT mode
override takes precedence over a selected profile's STT choice.

The studio uses the local PostgreSQL service started by Docker Compose. Install
the web dependencies once, then start the studio alongside the stack:

```bash
cd web && bun install   # or: npm install
cd ..
scripts/voice-ai.sh start --web
```

Open [http://localhost:3001](http://localhost:3001). The in-call transcript
streams each turn into one bubble and times it by what happened, not when the
browser received the text: a user turn is stamped at the measured microphone
onset (falling back to receipt time when no onset was observed), and an
assistant turn at its first token. The final STT transcript replaces the live
bubble in place, keeping that onset time, and re-arms the onset clock for the
next turn. To run the studio alone,
use `scripts/voice-ai.sh web`; `scripts/voice-ai.sh stop` stops only the studio
process whose PID and command match this repository. If port 3001 is already
in use, the script reports the conflict and leaves that process alone.
Set `WEB_PORT=3010` before invoking the script to select another port; the
agent and Studio use the same exported value for the CORS allowlist.
The local management API is under `/api` (including `/api/agents`,
`/api/prompts/base`, and `/api/catalog`); the runner's OpenAPI page is at
`http://localhost:7860/docs`.

PostgreSQL is published on loopback port 5439 by default. Set `POSTGRES_PORT`
to move the Compose port, and set `DATABASE_URL` in `.env` to the same host
port. The agent falls back to local SQLite when PostgreSQL is unavailable;
its default location remains the existing `voice_ai.db` file. Set
`SQLITE_DATABASE_URL` to choose another file location. The fallback is useful
for a single local user, but its data is independent from PostgreSQL and does
not synchronize automatically.

The Studio, API, database, and preset prompts are new code paths; the published
STT, TTS, memory, and turn-latency tables describe the original measured
configuration. The profile prompt check below used the compiled built-in
prompts with each preset's model parameters. It is six individual streamed
requests, one per language and preset, with no warm-up or retries, against the
already installed Qwen 3.5 4B through local Ollama. It ran on this Apple M5
MacBook Air with 16 GB unified memory on 2026-10-26. First content and total
time measure text generation only: they are neither percentiles nor speech
latency, and include no STT or TTS. The reply text and run conditions are saved
in ignored `data/measurements/agent_profiles.json`.

| Built-in profile | Language | First content (s) | Total text (s) | Reply characters |
|---|---:|---:|---:|---:|
| Bilingual Assistant | English | 3.485 | 4.653 | 165 |
| Bilingual Assistant | Hindi | 0.360 | 3.123 | 211 |
| Tech Tutor | English | 0.349 | 1.842 | 240 |
| Tech Tutor | Hindi | 0.263 | 2.450 | 184 |
| Creative Storyteller | English | 0.266 | 1.913 | 249 |
| Creative Storyteller | Hindi | 0.262 | 2.437 | 180 |

To repeat the check after a prompt or profile change, run
`scripts/voice-ai.sh profile-bench`. It makes six bounded English/Hindi local
requests (one per preset and language), writes exact measurements under
ignored `data/`, and refuses to download a model.

---

## Architecture & Data Flow

```
browser (Pipecat prebuilt WebRTC client)
   │  WebRTC
   ▼
┌──────────────────────────────────────────────────────────┐
│ SmallWebRTCTransport                                     │
├──────────────────────────────────────────────────────────┤
│ STT          │ LocalMlxRealtimeSTTService (WebSocket)    │
│              │   → mlx_audio.server, Nemotron 3.5 (Metal)│
├──────────────┼───────────────────────────────────────────┤
│ LLM          │ OLLamaLLMService → Ollama → qwen3.5      │
├──────────────┼───────────────────────────────────────────┤
│ routing      │ LanguageRoutingProcessor                  │
│              │   sentence split → script detect → voice  │
│              │   (VoiceRouter protocol: backend-agnostic)│
├──────────────┼───────────────────────────────────────────┤
│ TTS          │ Kokoro-82M (Metal via mlx-audio, or ONNX) │
├──────────────┼───────────────────────────────────────────┤
│ context      │ LLMContextAggregatorPair                  │
│              │   user aggregator before the LLM          │
│              │   assistant aggregator after the output   │
│              │ ContextShapingProcessor                   │
│              │   merges consecutive user turns           │
└──────────────┴───────────────────────────────────────────┘
        │                              │
        │ OTel spans                   │ Prometheus series
        ▼                              ▼
  otel-collector ──▶ Phoenix      Prometheus ──▶ Grafana
  :4319 gRPC        :6006 UI      :9091          :3003
```

### Context Shaping & Barge-In
- **Assistant Aggregator Placement**: Placed *after* the transport output so the conversational context only records speech that was actually heard by the user. Words interrupted mid-utterance never enter model history.
- **Consecutive User Turn Merging**: When a user interrupts their own turn before the bot has answered, the previous generation is cancelled and the new message follows immediately. `ContextShapingProcessor` merges consecutive user messages into a single coherent turn before sending it to the model, preventing degenerate prompt states.

---

## Swappable 3-Layer Design

STT, TTS, and LLM backends are selected entirely by configuration without touching pipeline code:
- The pipeline relies strictly on base classes (`STTService`, `LLMService`, `TTSService`).
- Metrics observers process backend-agnostic `MetricsFrame` payloads.
- Backend-specific settings deltas (e.g. Kokoro voice vs Whisper model selection) are encapsulated behind protocols in `bot/language.py` (`VoiceRouter`, `SttLanguageRouter`).
- Verified mechanically by `tests/test_modularity.py`.

### The Swap Catalogue

| Layer | Default | Swap to | Real Cost / Tradeoff | When to Use |
|---|---|---|---|---|
| **STT** | `mlx-audio-realtime` + Nemotron 3.5 ASR 0.6B | `mlx-whisper` + `whisper-large-v3-turbo` | Weights: 1.28 GB → 1.61 GB. FLEURS WER: **Hindi 7.18 % → 35.92 %**, English 8.56 % → 4.98 %. | When English precision is primary and Hindi is secondary. |
| **STT** | `mlx-audio-realtime` + Nemotron 3.5 ASR 0.6B | `mlx-audio` + Nemotron 3.5 ASR 0.6B | Batch HTTP endpoint (`/v1/audio/transcriptions`) instead of WebSocket: TTFS p50/p99 increases from 0.89s/0.99s to 0.94s/1.06s (en) and 1.00s/1.45s to 1.26s/1.80s (hi). Supports explicit language pinning (`stt_language=en|hi`). | When mid-session language pinning or explicit locale overrides are required. |
| **TTS** | `mlx-audio` + `Kokoro-82M-4bit` (Metal) | `kokoro` (in-process ONNX CPU) | Time to first audio: **0.28 s vs 0.71 s** (p50). In-process ONNX adds 0.48 GB to agent process but eliminates dependency on `mlx-audio` server. | When running without the local HTTP server or optimizing process boundaries. |
| **LLM** | `ollama` + `qwen3.5:4b` | `ollama` + `gemma3:4b` | Gemma 3 is non-thinking, so it needs no reasoning policy; the memory baseline measured it at 2.62 GB resident. | When running in a tighter memory tier or preferring Gemma 3 Devanagari styling. |
| **LLM** | `ollama` | `openai-compatible` + llama.cpp / vLLM / LM Studio | Alternative local inference runtime. | When deploying with non-Ollama runtimes. |
| **LLM** | `ollama` + `qwen3.5:4b` | `openai-compatible` + Cloud API (`gpt-5.4-mini`) | Requires a network, so it breaks the air-gapped property. Not measured against the local default. | When testing cloud frontier model reasoning capabilities. |

To swap backends, edit `.env`:
```bash
stt_provider=mlx-whisper
tts_provider=mlx-audio
```

---

## Reference Benchmarks

Every benchmark below is measured on reference hardware: **Apple Silicon M5 MacBook Air, 16 GB unified memory, macOS 27**.

### LLM Memory Tiering (Gemma 3 Fallback Ladder, 16 GB Unified Memory)

The default configuration is Qwen3.5 4B (measured above). Separately, `scripts/voice-ai.sh memory` derives a memory ceiling and `apply-tier` can configure the largest installed Gemma 3 rung on a tighter machine. These are the numbers for that fallback ladder, not for the default.

The memory rule computes available headroom by subtracting measured components and a system reserve:

| Component | Resident Size |
|---|---|
| Total Unified Memory | 16.0 GB |
| macOS baseline (active + wired + compressed) | 2.31 GB |
| Ollama, `gemma3:4b` resident | 2.62 GB |
| `mlx-audio` server (Nemotron STT + Kokoro TTS) | 0.45 GB |
| Voice agent process | 0.13 GB |
| System & swap headroom reserve | 1.50 GB |
| **Available for LLM** | **11.61 GB** |
| **Memory Ceiling (Selected by Rule)** | **`gemma3:12b`** (8.16 GB) |
| **Installed Tier Active** | **`gemma3:4b`** (3.35 GB) |

### LLM Speed (Qwen3.5 4B via Ollama)

Measured on the bot's actual endpoint (`/v1/chat/completions`), streaming, warm, five samples per prompt after a discarded warm-up. Settings: `reasoning_effort=none`, `temperature=0.5`, `max_tokens=200`, and the system prompt from `bot/config.py`. Hardware and runtime: M5 MacBook Air, 16 GB, Ollama 0.40.9, `qwen3.5:4b` (nvfp4).

| Prompt | Time to first content token (p50) | Total reply (p50) | Completion tokens (p50) | Hidden reasoning |
|---|---:|---:|---:|---:|
| English: "Can you hear me?" | 0.07 s | 0.54 s | 15 | 0 chars |
| Hindi: "क्या आप मुझे सुन सकते हैं?" | 0.07 s | 0.76 s | 22 | 0 chars |

Qwen3.5 thinks by default, and Ollama's OpenAI-compatible endpoint does not forward `think: false`. Without `reasoning_effort=none`, the same English request spent the entire 200-token cap on hidden reasoning and returned **no content**; with the cap removed, the benchmark path generated a median of 2,412 reasoning tokens and still produced no spoken answer. That is why the Ollama factory sends the field. The build is 4.0 GB on disk and 4.4–4.7 GB resident while warm, depending on the context Ollama has allocated.

### End-to-End Turn Latency Breakdown

Measured through the complete pipeline with real VAD, real STT, real TTS, and a deterministic LLM reply:

| Stage | Duration | Cumulative Time (from end of speech) |
|---|---:|---:|
| VAD silence window (`vad_stop_secs = 0.8`) | 0.79 s | 0.79 s |
| STT final transcript arrived | 0.14 s (streaming) / 0.77 s (batch) | 0.93 s (streaming) / 1.56 s (batch) |
| LLM first token produced | 0.20 s | 1.13 s (streaming) / 1.76 s (batch) |
| First sound of synthesized audio | 0.33 s | 1.46 s (streaming) / 2.23 s (batch) |

- **Silence Window Rationale**: 0.8 s silence window prevents the bot from answering conversational fragments (evaluations showed shorter windows false-triggered on 9 of 10 natural mid-sentence pauses).
- **First-Clause Cutoff**: When the first sentence takes longer than `first_chunk_deadline_seconds = 0.25`, the bot synthesizes the first clause immediately, saving **0.21 s** on time to first audio.

### STT Quality Comparison (FLEURS Corpus)

Evaluated over 40 FLEURS clips per language using OpenAI's standard `whisper-normalizer`:

| Model | English (`en_us`) WER | Hindi (`hi_in`) WER | English CER | Hindi CER |
|---|---:|---:|---:|---:|
| **Nemotron 3.5 ASR 0.6B (Default)** | 8.56 % | **7.18 %** | 4.96 % | 4.29 % |
| **Nemotron 3.5 ASR 0.6B (8-bit quantized)** | 8.45 % | 7.24 % | 4.92 % | 4.36 % |
| **Whisper large-v3-turbo** | **4.98 %** | 35.92 % | 2.42 % | 27.45 % |

*Finding*: Nemotron is **5.0× more accurate on Hindi** (7.18 % vs 35.92 % WER), while Whisper is **1.7× more accurate on English** (4.98 % vs 8.56 % WER).

### Streaming Lookahead (Latency vs Accuracy)

The served Nemotron checkpoint declares a 13-frame right context, so its
native encoder chunk is 1.12 s. That is the default everywhere in this
deployment. The server's `--realtime-transcription-delay-ms` flag is silently
ignored for this checkpoint (`model_type` is `nemotron_asr`, not
`nemotron_asr_streaming`), so `STT_LOOKAHEAD_FRAMES` is the supported
override; `scripts/stt_server.py` applies it to each `/v1/realtime` session,
never to the loaded model, so the batch table above is unaffected.

Measured on the same 40 FLEURS clips per language, mean per-clip WER/CER
(not corpus-level — the paired interval below needs one score per clip):

| Lookahead | Native chunk | en WER | en CER | hi WER | hi CER |
|---:|---:|---:|---:|---:|---:|
| **13 (default)** | 1.12 s | **9.28 %** | **5.50 %** | **7.95 %** | **4.85 %** |
| 6 | 0.56 s | 10.07 % | 5.99 % | 8.13 % | 4.99 % |
| 3 | 0.32 s | 10.74 % | 6.42 % | 8.90 % | 5.81 % |
| 0 | 0.08 s | 11.84 % | 7.48 % | 9.57 % | 5.90 % |

Halving the chunk makes partial text refresh twice as often. The measured
13→6 difference is **+0.79 WER points on English** (95 % CI −0.56 … +2.31)
and **+0.17 on Hindi** (−0.58 … +0.93), paired bootstrap over clips. Forty
clips cannot resolve a difference that small, so this deployment keeps 13:
the freshness gain is real, the accuracy case for paying for it is not yet
measured, and lookahead 0 also collapses throughput (40 clips per language
take ~9× longer than at 13: 239 s vs 28 s English, 307 s vs 34 s Hindi).
Set `stt_lookahead_frames=6` in `.env` to opt in on a deployment
that values partial-text latency more than the last fraction of a WER
point. Reproduce with:

```bash
scripts/voice-ai.sh lookahead-bench --clips 40 --lookaheads 13,6,3,0
```

The command reads the cached FLEURS fixtures and refuses to run on fewer
clips than requested; populate them first with `scripts/voice-ai.sh eval`.

### TTS Speed Comparison

Time to first audio (TTFA) across five warm utterances:

| Backend | TTFA (p50) | TTFA (max) | Real-Time Factor |
|---|---:|---:|---:|
| **`mlx-audio` + Kokoro-82M-4bit (Metal GPU)** | **0.28 s** | 0.47 s | 0.07 |
| **`kokoro` in-process ONNX (CPU)** | 0.71 s | 1.22 s | 0.23 |

### Reproducing Benchmarks

Run the benchmark suite on your own hardware:
```bash
scripts/voice-ai.sh memory        # Measure memory headroom & derive LLM tier
scripts/voice-ai.sh bench         # Measure STT speed (batch)
scripts/voice-ai.sh bench --realtime # Measure STT speed (streaming)
scripts/voice-ai.sh lookahead-bench # Streaming WER/CER at 13, 6, 3 and 0 frames
scripts/voice-ai.sh tts-bench     # Benchmark TTS backends
scripts/voice-ai.sh turn-bench    # Measure end-to-end turn latency
scripts/voice-ai.sh eval          # Run FLEURS WER/CER evaluation
scripts/voice-ai.sh test          # Run full regression test suite
```

---

## Observability & Metrics

### Local Container Architecture

| Service | Environment | Host Port | Rationale |
|---|---|---|---|
| **Phoenix** | Host (`scripts/voice-ai.sh phoenix`) | 6006 (UI + OTLP/HTTP), 6007 (gRPC) | Runs on host to bypass Docker VM memory constraints. |
| **OpenTelemetry Collector** | Container | 4319, 9464 | Port 4319 chosen to prevent collisions with standard OTLP 4317. |
| **Prometheus** | Container | 9091 | Port 9091 chosen to prevent collisions with default Prometheus 9090. |
| **Grafana** | Container | 3003 | Port 3003 chosen to avoid default port 3000 collisions. |
| **`mlx-audio` Server** | Host | 8020 | Serves STT WebSocket & TTS endpoints on Metal GPU. |
| **Ollama** | Host | 11434 | Standard Ollama inference port. |
| **PostgreSQL** | Container | 5439 (configurable) | Local agent configuration; loopback only, with a SQLite fallback. |
| **Studio/API** | Host | 3001 / 7860 `/api` | Optional single-user local management interface; no authentication. |

### The Metrics Contract

Defined in `bot/observers.py` and validated by `tests/test_observers.py`:

| Metric Name | Type | Description |
|---|---|---|
| `voice_user_bot_latency_seconds` | Histogram | End-to-end latency: user speech finished → bot audio starts. |
| `voice_turn_ttfb_seconds` | Histogram | Per-service time to first byte. |
| `voice_turn_ttfa_seconds` | Histogram | Time to first audio (TTS output). |
| `voice_turn_ttfat_seconds` | Histogram | Time to first LLM answer token. |
| `voice_text_aggregation_seconds` | Histogram | First LLM token → first synthesized clause/sentence. |
| `voice_service_processing_seconds` | Histogram | Processing duration per pipeline step. |
| `voice_llm_tokens_total` | Counter | Generated token count (prompt vs completion). |
| `voice_tts_characters_total` | Counter | Total text characters synthesized. |
| `voice_stt_audio_seconds_total` | Counter | Total audio seconds ingested by STT. |
| `voice_turn_interrupted_total` | Counter | Count of user barge-in interruptions. |
| `voice_process_resident_memory_bytes` | Gauge | Process RSS by component. |

*Cardinality Rule*: Labels are strictly bounded (`processor`, `model`, `kind`, `component`). Session IDs, user IDs, and trace IDs are strictly forbidden as metric labels.

---

## Excluded Technologies

Each alternative was evaluated and omitted for clear architectural reasons:

| Excluded Technology | Rationale |
|---|---|
| **Langfuse v4** | Documented minimum of 16 GB RAM consumes entire machine memory. |
| **Opik** | Heavy dependency tree (MySQL, Redis, ClickHouse, ZooKeeper, MinIO, JVM). |
| **LiveKit Agents** | Requires self-hosted SFU daemon; unnecessary for 1:1 local WebRTC. |
| **Daily WebRTC** | Cloud dependency; breaks 100% air-gapped operation. |
| **`faster-whisper`** | CUDA-only GPU acceleration; leaves Apple Silicon Metal GPU idle. |
| **`whisper.cpp`** | Lacks streaming WebSocket server. |
| **Whisper large-v3 for Hindi** | 35.56 % FLEURS WER vs Nemotron's 7.18 %. |
| **XTTS-v2 / F5-TTS / Piper Hindi** | Non-commercial or restrictive licenses. |

---

## Troubleshooting & Notes

- **macOS Microphone Permission**: Microphone access is granted per terminal emulator. If the agent receives no audio:
  ```bash
  tccutil reset Microphone com.apple.Terminal
  ```
  Then restart your terminal and allow access when prompted.
- **Headphone Recommendation**: A laptop microphone easily picks up speaker audio during speech synthesis. Always use wired headphones for reliable barge-in and turn detection.
- **Hindi TTS Phonemization**: Kokoro Hindi voice synthesis uses `espeak-ng`. Ensure it is installed via Homebrew:
  ```bash
  brew install espeak-ng
  ```
- **Docker Memory Allocation**: Ensure Docker Desktop has at least 2 GB RAM allocated for Prometheus, Grafana, and OpenTelemetry Collector containers.

---

## Component Licenses

This project integrates open models and tools with the following upstream licenses:

| Component | Upstream License |
|---|---|
| **Pipecat** | BSD-2-Clause |
| **`mlx-audio`** | MIT |
| **Nemotron 3.5 ASR Weights** | OpenMDW-1.1 (Downloaded locally, not redistributed) |
| **`mlx-whisper` / Whisper Weights** | MIT / Apache-2.0 |
| **Kokoro Weights & Runtime** | Apache-2.0 |
| **Ollama** | MIT |
| **OpenTelemetry Collector** | Apache-2.0 |
| **Arize Phoenix** | Elastic-2.0 |
| **Prometheus** | Apache-2.0 |
| **Grafana** | AGPL-3.0 |
| **FLEURS Dataset** | CC-BY-4.0 |

---

## Repository Layout

```
bot/            The agent: Pipecat pipeline, service factories, language routing, observers
platform_api/   Platform control plane: account identity, wallet accounting, schema lifecycle
evals/          FLEURS WER/CER quality evaluation harness & scenario tests
scripts/        Management script (voice-ai.sh), benchmarks, tier calculation
tests/          Pytest regression suite (modularity, observers, pipeline, config)
observability/  Collector configuration, Prometheus scrape rules, alert definitions, Grafana provisioning
data/           Local benchmark outputs and database state (gitignored)
```

---

## Platform Control Plane Entrypoints

```bash
# Initialize fresh platform database schema
uv run python -m platform_api.cli init-db

# Verify database schema matches platform metadata
uv run python -m platform_api.cli check-schema

# Start platform API server
uv run uvicorn platform_api.app:create_app --factory --host 127.0.0.1 --port 8000
```

---

## Conversation Flow Engine (Pure Compiler & Reducer)

The conversation flow engine in `bot.flow` provides deterministic state machine execution and compiler validation without database, network, GPU, or provider imports.

### Pure Compiler and Reducer Usage

```python
import json
from bot.flow import (
    compile_flow,
    initial_session_state,
    reduce_event,
    UserTurnAcceptedEvent,
    StateProposalModelEvent,
    StateProposal,
    ProposeCaptureUpdate,
    Evidence,
)

# 1. Load authored or normalized flow JSON
with open("tests/fixtures/flow/valid-credit-card-en-hi.json", "r", encoding="utf-8") as f:
    flow_dict = json.load(f)["flow"]

# 2. Compile once per version into an immutable CompiledFlow artifact
compiled = compile_flow(flow_dict, policy="You are a qualification assistant.")

# 3. Initialize pure SessionState bound to a non-empty session ID
state = initial_session_state(compiled, session_id="sess_demo_1")

# 4. Accept a user turn under the per-session lock
turn_event = UserTurnAcceptedEvent(
    event_id="evt_turn_1",
    session_id="sess_demo_1",
    transport_epoch="epoch_1",
    turn_id="turn_1",
    generation_id="gen_1",
    text="I am salaried and earn INR 80000 monthly.",
)
reduction = reduce_event(compiled, state, turn_event)
state = reduction.state

# 5. Reduce structured model proposals with exact evidence substring verification
proposal = StateProposal(
    proposal_id="prop_1",
    session_id="sess_demo_1",
    turn_id="turn_1",
    generation_id="gen_1",
    base_state_version=state.version,
    updates=[
        ProposeCaptureUpdate(
            field="employment_type",
            value="salaried",
            evidence=Evidence(source_turn_id="turn_1", quote="salaried"),
        ),
    ],
)
reduction = reduce_event(
    compiled,
    state,
    StateProposalModelEvent(
        event_id="evt_prop_1",
        session_id="sess_demo_1",
        transport_epoch="epoch_1",
        turn_id="turn_1",
        generation_id="gen_1",
        proposal=proposal,
    ),
)
state = reduction.state
# reduction.commands contains controller-owned manifests or actions
# reduction.diagnostics contains any validation warnings or errors
```

### Current Scope and Voice Qualification Status

Currently, only the pure graph schemas, compiler, and in-memory event reducer exist in `bot.flow`. Runtime persistence, end-to-end chat runners, and voice adapter integration are incomplete implementation plan targets (Tasks 3–6).

- **Default flows reject voice playback**: Flows compiled without an explicit `AdapterCapabilities` object default to `playback_mode="unqualified"`, rejecting all `fragment_played` events and preventing unverified voice completion.
- **Capabilities object alone is not qualification proof**: Supplying an `AdapterCapabilities` instance to the compiler changes manifest metadata, but does not constitute empirical verification. Actual voice transport qualification requires genuine browser loopback transport evidence and data-channel acknowledgments, which are not yet implemented.
- **Durable chat runner is incomplete**: The reducer defines pure contracts for `chat_committed` events with required durable delivery IDs, but durable runner persistence and platform chat integration remain incomplete future tasks.
- **Registered actions fail closed**: Flows with action steps currently also fail closed until the durable execution, user confirmation, and result validation lifecycle is implemented.
