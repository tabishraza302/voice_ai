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
evals/          FLEURS WER/CER quality evaluation harness & scenario tests
scripts/        Management script (voice-ai.sh), benchmarks, tier calculation
tests/          Pytest regression suite (modularity, observers, pipeline, config)
observability/  Collector configuration, Prometheus scrape rules, alert definitions, Grafana provisioning
data/           Local benchmark outputs and database state (gitignored)
```
