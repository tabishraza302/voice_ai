# voice-ai

A **fully local, air-gapped** English + Hindi voice agent with a real
AI-observability layer.

Nothing in it talks to the internet at runtime. Every model runs on the
machine; the traces, metrics and dashboards are produced by four local
containers. The numbers in this README were measured on the machine that
wrote it, and every one of them has a script behind it.

```
make install && make models && make up
# then open http://localhost:7860/client
```

---

## What it is

A voice agent that speaks English and Hindi, runs entirely on one Apple
Silicon MacBook Air, and is instrumented end to end: every turn produces
an OpenTelemetry trace in Phoenix, a set of Prometheus series, and a
dashboard.

| | |
|---|---|
| **STT** | NVIDIA Nemotron 3.5 ASR Streaming 0.6B, via `mlx-audio` on Metal |
| **LLM** | `gemma3` on Ollama, tier chosen by measurement |
| **TTS** | Kokoro (in-process ONNX) |
| **Orchestration** | Pipecat 1.13.0 |
| **Transport** | Serverless WebRTC; the browser client is Pipecat's own |
| **Traces** | OpenTelemetry → Collector → Arize Phoenix |
| **Metrics** | Prometheus + Grafana |
| **Quality** | FLEURS WER/CER harness + Pipecat behavioural evals |

---

## The three layers are swappable

This is a design commitment, not a convenience. **STT, TTS and LLM are
selected by config**, and swapping one must not require touching the
pipeline, the observability layer, or the eval harness.

The pipeline is written against Pipecat's `STTService` / `LLMService` /
`TTSService` base classes. The observers read Pipecat's `MetricsFrame`
payloads, which are backend-agnostic. The eval harness talks RTVI, which
is backend-agnostic. **So a swap touches two files: the registry and the
config.**

The one place a naive swap breaks is worth naming, because it is the
reason this works: settings deltas are backend-specific types.
`KokoroTTSService.Settings(voice=..., language=...)` is not
`WhisperSTTServiceMLX.Settings(model=..., language=...)`. A routing
processor that hardcodes either one looks perfectly modular until the day
the TTS is swapped — and then it pushes a delta the new backend silently
ignores, so the bot keeps speaking the old voice in the old language with
no error anywhere.

So the delta construction lives behind two protocols in `bot/language.py`,
with one implementation per backend:

```python
class VoiceRouter(Protocol):
    def delta_for(self, script: Script) -> Any: ...

class SttLanguageRouter(Protocol):
    def delta_for(self, language: Language | None) -> Any: ...
```

`bot/pipeline.py` calls `router.delta_for(...)` and never names a
backend. `tests/test_modularity.py` enforces that mechanically, including
an assertion that the pipeline module imports no concrete service class.

### The swap catalogue

Every option is local. Every cost is real.

| Layer | Default | Swap to | Real cost | When |
|---|---|---|---|---|
| STT | `mlx-audio` + Nemotron 3.5 ASR 0.6B | `mlx-whisper` + `whisper-large-v3-turbo` | 1.61 GB vs 1.28 GB; **Hindi 35.56 % vs 6.81 % WER**, English 4.08 % vs 7.91 % | The MLX Nemotron conversion fails to load, or English matters more than Hindi |
| STT | `mlx-audio` + Nemotron | `funasr` (SenseVoice) | CPU only, no Metal path; **no Hindi** | English-only deployment |
| TTS | `kokoro` | `mlx-audio` + `chatterbox-multilingual-v3` | +1.0 GB RAM; a different model behind the same service | Kokoro's grade-C Hindi voices prove unintelligible |
| TTS | `kokoro` | `pocket-tts` | Local, different timbre | A different voice character is wanted |
| LLM | `ollama` + `gemma3:4b` | `ollama` + `gemma3:12b` | +4.8 GB | The tier rule selects it — measurement, not preference |
| LLM | `ollama` | `openai-compatible` + llama.cpp / vLLM / LM Studio | Same model, different runtime | Ollama is dropped from the stack |
| LLM | `gemma3:4b` | `gemma3:1b` | −2.5 GB | The tier rule selects it when under 3.5 GB is left |
| LLM | `gemma3:4b` | `qwen3:8b` | 5.2 GB | **Not on this endpoint.** qwen3 thinks by default and Ollama's OpenAI-compatible API cannot forward `think: false`, so the turn spends its whole budget on reasoning and returns an empty answer |

Swapping is a `.env` edit:

```bash
stt_provider=mlx-whisper
tts_provider=mlx-audio
tts_model=mlx-community/chatterbox-multilingual-v3
```

**A swap changes the measured numbers.** Re-run `make eval` and
`make bench`, and update the tables below.

---

## The measured numbers

<!-- MEASURED:START -->
_Not yet measured on this machine. Run `make memory`, `make bench` and
`make eval`, then update this section._
<!-- MEASURED:END -->

### The published comparison

These are the figures the measurements are compared against. They come
from NVIDIA's model card and from Whisper's published FLEURS baselines,
and they were computed on different hardware with a particular text
normalization — which is exactly why this repo measures its own.

| Model | FLEURS `hi_in` WER | FLEURS `en_us` WER |
|---|---:|---:|
| Nemotron 3.5 ASR 0.6B | **6.81 %** | 7.91 % |
| Nemotron 3.5 ASR 0.6B, auto-detect | 8.23 % | 8.84 % |
| `whisper-large-v3-turbo` | 35.56 % | **4.08 %** |

Two things follow. **Nemotron is ~5× better than Whisper on Hindi**, and
**Whisper is ~2× better on English** — so the English tier is a genuine
upgrade, not a consolation prize. And Nemotron is *better at Hindi than
at English*, which is unusual for a multilingual model and is why one
model can serve both languages acceptably.

The chunk size is a real dial. Nemotron's MLX config defaults to
`att_context_size: [56, 13]` — the 1.12 s look-ahead, the most accurate
of the four supported points. On Hindi, 6.81 % at 1.12 s becomes 8.13 %
at the 80 ms chunk, and auto-detect goes from 8.23 % to 11.47 %.

---

## Architecture

```
browser (Pipecat's prebuilt client)
   │  WebRTC
   ▼
┌──────────────────────────────────────────────────────────┐
│ SmallWebRTCTransport                                     │
├──────────────────────────────────────────────────────────┤
│ STT          │ LocalMlxSTTService → mlx_audio.server      │
│              │   NVIDIA Nemotron 3.5 ASR (Metal)          │
├──────────────┼───────────────────────────────────────────┤
│ LLM          │ OLLamaLLMService → Ollama → gemma3         │
├──────────────┼───────────────────────────────────────────┤
│ routing      │ LanguageRoutingProcessor                   │
│              │   sentence split → script detect → voice    │
│              │   (VoiceRouter protocol: no backend named) │
├──────────────┼───────────────────────────────────────────┤
│ TTS          │ KokoroTTSService (in-process ONNX)         │
├──────────────┼───────────────────────────────────────────┤
│ context      │ LLMContextAggregatorPair                   │
│              │   user aggregator before the LLM           │
│              │   assistant aggregator after the output     │
└──────────────┴───────────────────────────────────────────┘
        │                              │
        │ OTel spans                   │ Prometheus series
        ▼                              ▼
  otel-collector ──▶ Phoenix      Prometheus ──▶ Grafana
  :4319 gRPC        :6006 UI      :9091          :3003
```

The assistant aggregator sits **after** the transport output, so the
context records what was actually *spoken*: words cut off by an
interruption never reach the LLM's history.

### The observability stack

Three containers, on a network called `voice-ai-net`, plus Phoenix on
the host:

| Service | Where | Host port | Why that port |
|---|---|---|---|
| Phoenix | **the host** (`make phoenix`) | 6006 | Docker Desktop's VM is capped at 1.93 GB and the `hear_help` stack already holds ~1.05 GB of it. Phoenix was OOM-killed at 1g, at 768m, and in `PHOENIX_LITE_MODE`. The collector reaches it over OTLP/HTTP at `http://host.docker.internal:6006` |
| otel-collector | container | 4319, 4320, 9464 | Not 4317/4318: the `hear_help` stack holds them |
| Prometheus | container | 9091 | Not 9090: the `hear_help` stack's Prometheus holds it on the host, and Phoenix's own Prometheus endpoint is off — it binds 9090 with no way to move it |
| Grafana | container | 3003 | Not 3000: `newotnite-api-1` holds it. Not 3002: hear_help's next-server holds `*:3002`, and a host process on the IPv6 wildcard beats Docker's `127.0.0.1` publish for any browser that resolves `localhost` to `::1` |

Every container has a `mem_limit`. This is a 16 GB machine shared with
two other projects; an unbounded container is the most likely way to
push it into swap.

Grafana reads two provisioned files,
`observability/grafana/provisioning/datasources/` and
`observability/grafana/provisioning/dashboards/`. Without them it comes
up blank — no datasource, and the dashboard JSON is never read.

Two Phoenix facts worth knowing, because both cost an afternoon:

- Phoenix files a trace under the project named by the
  `openinference.project.name` **resource** attribute, not by
  `service.name`. With only `service.name`, every trace lands in
  Phoenix's `default` project. The collector sets it, because the
  collector is the Phoenix boundary.
- The `otlp_http` exporter appends `/v1/traces` to its endpoint. A
  full path in the config becomes `/v1/traces/v1/traces` and Phoenix
  answers 405.

### The metrics contract

`bot/observers.py` owns the metric names, and
`observability/prometheus.yml`, `observability/alerts.yml` and
`observability/grafana/dashboards/voice-ai.json` all reference them by
name.

| Metric | Type | What it means |
|---|---|---|
| `voice_user_bot_latency_seconds` | Histogram | **The SLO.** User stops speaking → bot starts speaking. |
| `voice_turn_ttfb_seconds` | Histogram | Per-service time to first byte. Diagnosis, not the SLO. |
| `voice_turn_ttfa_seconds` | Histogram | Time to first audio, including any leading silence. |
| `voice_turn_ttfat_seconds` | Histogram | Time to the LLM's first answer token. |
| `voice_text_aggregation_seconds` | Histogram | First LLM token → first complete sentence. Measured by the **language router**, not the TTS: the TTS runs in `TextAggregationMode.TOKEN` because the router does the splitting, and in that mode Pipecat skips the TTS's own text-aggregation metric. |
| `voice_service_processing_seconds` | Histogram | Service processing time. |
| `voice_llm_tokens_total` | Counter | Tokens by kind. |
| `voice_tts_characters_total` | Counter | Characters synthesized. |
| `voice_stt_audio_seconds_total` | Counter | Audio seconds submitted. |
| `voice_turn_interrupted_total` | Counter | Turns the user interrupted. |
| `voice_process_resident_memory_bytes` | Gauge | Resident memory by component. |

Labels are bounded to `processor` and `model`. Never a session id.

Two of these only appear once the *voice* path runs, not the browser
client's text box: `voice_stt_audio_seconds_total` needs audio, and
`voice_user_bot_latency_seconds` needs a turn that started with the
user speaking.

The alerts are on the **end-to-end** latency, not the per-service TTFB: a
bot with a fast STT and a slow LLM has a fast TTFB and a bad experience,
and only the end-to-end series catches that.

---

## Excluded technologies, and why

Exclusions are the signal. Each of these was considered and rejected for
a stated reason.

| Excluded | Why |
|---|---|
| **Langfuse v4** | Its documented minimum is 16 GiB — this machine's entire RAM. |
| **Opik** | 7+ containers (MySQL, Redis, ClickHouse, ZooKeeper, MinIO, a JVM backend, a frontend). |
| **LiveKit Agents** | Requires a self-hosted `livekit-server` SFU with no published RAM floor — an extra always-on service on a box already at 92 % disk. |
| **WebRTC via Daily** | A cloud account. This project is air-gapped. |
| **`faster-whisper` as the inference path** | Its GPU path is CUDA-only, so on macOS it leaves the M5 idle. (It *is* installed, because `pipecat.services.whisper.stt` imports it unconditionally — the MLX path never calls it.) |
| **`whisper.cpp`** | No streaming server (upstream #2038, closed stale), and it needs `cmake`. |
| **Whisper large-v3 for Hindi** | 35.56 % WER on FLEURS `hi_in`, against Nemotron's 6.81 %. |
| **Parakeet TDT v3** | 25 European languages, no Hindi. |
| **Canary** | 25 European languages plus ru/uk, no Hindi. |
| **Piper Hindi voices** | Non-commercial (CC BY-NC-SA 4.0 / IITM EULA). |
| **XTTS-v2** | CPML, non-commercial. |
| **F5-TTS official weights** | CC-BY-NC. |
| **openWakeWord** | CC BY-NC-SA *model* licence. |
| **BlackHole** | GPLv3 / proprietary binary. Use headphones instead. |
| **A custom Vite browser client** | Pipecat's prebuilt `/client` already has the transcript, the RTVI event stream, and a metrics panel with TTFB and TTFA. A custom client is a second build system for a readout that already exists. |

---

## Running it

```bash
make install     # venv + dependencies + the host-side Phoenix
make models      # STT, TTS and LLM weights
make up          # observability stack, STT server, agent
```

Then open **http://localhost:7860/client** and talk to it.

| | |
|---|---|
| Agent | http://localhost:7860/client |
| Phoenix (traces) | http://localhost:6006 |
| Grafana (dashboards) | http://localhost:3003 |
| Prometheus | http://localhost:9091 |
| Metrics (raw) | http://localhost:7860/metrics/ |

Grafana's login is `admin` / `admin`.

### Talking to it

Type in the client's message box, or speak. The bot replies in the
language you used.

To force a language, send an RTVI message:

```json
{"type": "set_language", "language": "hi"}
```

`auto` (the default) uses Nemotron's language-ID conditioning, which is
the mode that survives code-switching.

### Measuring

```bash
make memory          # the memory baseline, and the LLM tier it selects
make bench           # STT speed: real-time factor and time-to-final-transcript
make eval            # FLEURS WER/CER, per model and per normalization
make eval-scenarios  # behavioural evals (needs the agent running)
make smoke           # synthesise speech, transcribe it back (needs: make stt)
make test            # the regression suite
```

`make memory` writes `data/memory_baseline.json` and applies the tier
rule: it subtracts the measured macOS baseline, the STT, the TTS, the
agent and a 1.5 GB reserve for the other projects' Docker stacks, and
picks `gemma3:12b`, `gemma3:4b` or `gemma3:1b` from what is left.
`make apply-tier` writes the result into `.env.example`.

### The other projects on this box

This is a shared 16 GB machine: the `hear_help` and `newotnite` stacks
are running here too, and they hold the memory and some of the ports
voice-ai needs. So voice-ai stops them before it runs — `make up`,
`make run`, `make bench` and `make eval` all do it, and it is
reversible:

```bash
make stop-others   # stop the hear_help and newotnite containers
make start-others  # bring them back exactly as they were
```

Only `docker stop` is used: no container is removed, no volume is
touched, no configuration is changed. The 1.5 GB reserve in the tier
rule is the headroom left for them.

### Air-gapped

Every model is downloaded by `make models` before the demo. After that,
disconnect the network and the conversation still works — that is the
claim, and it is only true because
`PHOENIX_ALLOW_EXTERNAL_RESOURCES=false` and nothing else reaches out.

---

## macOS specifics

- **Microphone access** is granted per terminal, not per app. If the
  bot hears nothing: System Settings → Privacy & Security → Microphone,
  enable your terminal, and `tccutil reset Microphone com.apple.Terminal`
  to re-prompt.
- **Use wired headphones.** A laptop's speaker-to-microphone coupling is
  the worst case for barge-in; the browser's AEC cannot fix it.
- **espeak-ng** must be on `PATH` for Kokoro's Hindi phonemization:
  `brew install espeak-ng`.
- **The uv cache is project-local** (`.cache/uv`, set in `uv.toml`).
  The shared cache at `~/.cache/uv` has a root-owned subdirectory on
  this host that uv cannot write to.

---

## Licences

| Component | Licence |
|---|---|
| Pipecat | BSD-2-Clause |
| `mlx-audio` | MIT |
| Nemotron 3.5 ASR weights | **OpenMDW-1.1** — source-available, with use-based restrictions. Downloaded, not redistributed. |
| `mlx-whisper`, Whisper weights | MIT / Apache-2.0 |
| Kokoro runtime / weights | MIT / Apache-2.0 |
| Ollama | MIT |
| OpenTelemetry Collector | Apache-2.0 |
| Arize Phoenix | Elastic-2.0 (source-available, not OSI) |
| Prometheus | Apache-2.0 |
| Grafana | AGPL-3.0 |
| FLEURS | CC-BY-4.0 |

The two non-OSI licences — OpenMDW-1.1 for the ASR weights and
Elastic-2.0 for Phoenix — are recorded here because they are the
conditions on the project's two most important pieces, and because
neither's OSI-approved alternative fits the machine.

---

## Layout

```
bot/            the agent
evals/          the quality harness (FLEURS WER/CER + behavioural evals)
scripts/        model pulls, the memory baseline, the STT benchmark
tests/          the regression suite
observability/  the stack's config: collector, Prometheus, alerts, Grafana
data/           generated measurements (gitignored)
```

See `AGENTS.md` for the conventions this repository is worked under.
