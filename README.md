# voice-ai

A **fully local, air-gapped** English + Hindi voice agent with a real
AI-observability layer.

The agent itself never talks to the internet: every model runs on the
machine, and the traces, metrics and dashboards are produced by local
containers and a local Phoenix. The measurement scripts do fetch their
fixtures (FLEURS) and their models from the hub — once, before the
demo. The numbers in this README were measured on the machine that
wrote it, and every one of them has a script behind it.

The one exception is opt-in: pointing the `openai-compatible` LLM
provider at a cloud API instead of a local server sends that turn
somewhere else. The default configuration reaches nothing.

```
scripts/voice-ai.sh install && scripts/voice-ai.sh models && scripts/voice-ai.sh start
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
| **STT** | NVIDIA Nemotron 3.5 ASR Streaming 0.6B, over `mlx-audio`'s realtime WebSocket on Metal |
| **LLM** | `gemma3` on Ollama, tier chosen by measurement |
| **TTS** | Kokoro-82M (in-process ONNX, or the same model on Metal through `mlx-audio`) |
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

Every option is local. Every cost is real — and every one of them is
measured on this machine, or it is not claimed.

Two kinds of cost appear below, and they are not the same kind.
**Weights and corpus results belong to the backend**: 1.6 GB of weights
is 1.6 GB anywhere, and a WER on a named corpus is a fact about that
model on that corpus. **Headroom, speed and the tier choice belong to
the machine**: they are this box's, and another machine re-derives them
by re-running the script that produced them.

| Layer | Default | Swap to | Real cost | When |
|---|---|---|---|---|
| STT | `mlx-audio-realtime` + Nemotron 3.5 ASR 0.6B | `mlx-whisper` + `whisper-large-v3-turbo` | 1.28 GB → 1.61 GB of weights; on FLEURS, **Hindi 7.18 % → 35.92 % WER**, English 8.56 % → 4.98 % | The MLX Nemotron conversion fails to load, or English matters more than Hindi |
| STT | `mlx-audio-realtime` + Nemotron 3.5 ASR 0.6B | `mlx-audio` + Nemotron 3.5 ASR 0.6B | Same Nemotron checkpoint over batch HTTP (`/v1/audio/transcriptions`) instead of the WebSocket transport: the transcript waits for the whole segment, so TTFS p50 / p99 is **0.94 s / 1.06 s (English)** and **1.26 s / 1.80 s (Hindi)** against the realtime **0.89 s / 0.99 s** and **1.00 s / 1.45 s** (FLEURS, this machine, local `mlx_audio.server`; worst p99 1.45 s → 1.80 s). It buys what `/v1/realtime` cannot: a language field, so `stt_language=en|hi` is accepted and a mid-session switch can be routed | The session must pin a language, or the STT itself must follow a mid-session language switch |
| TTS | `mlx-audio` + `Kokoro-82M-4bit` | `kokoro` (in-process ONNX) | **Time to first audio 0.28 s against 0.71 s** (p50 of five sentences, warm, this machine); the default needs the server, the swap does not, and the swap costs 0.48 GB in the agent's own process | The mlx-audio server is not wanted on this deployment, or its memory is not there |
| LLM | `ollama` + `gemma3:4b` | `ollama` + `gemma3:12b` | +4.8 GB of weights; the headroom to hold it is this machine's | The tier rule selects it when its weights are on the machine — measurement, not preference |
| LLM | `ollama` | `openai-compatible` + llama.cpp / vLLM / LM Studio | Same model, different runtime | Ollama is dropped from the stack |
| LLM | `ollama` + `gemma3:4b` | `openai-compatible` + a cloud API (`gpt-5.4-mini`) | **Quality up, latency up:** the LLM stage goes from 0.25 s to 1.12 s (median over six turns, this machine) — about +0.87 s on a 2.2 s turn — and the reply gets shorter (16 → 11 tokens) and better in Hindi. The turn leaves the machine, and the air-gap claim above stops holding | The local model's Hindi is not good enough, and the network is there |
| LLM | `gemma3:4b` | `gemma3:1b` | −2.5 GB of weights; **it answers Hindi questions in English** (both its builds, on every sample), so the rung below the 4B one breaks the bot's language contract while keeping the persona | The tier rule selects it when under 3.5 GB is left — and the bot stops speaking Hindi |
| LLM | `gemma3:4b` | `qwen3:4b-instruct-2507-q4_K_M` | **0.027 s faster to the first token** (0.057 s against 0.084 s, median of five on the bot's own endpoint) and 0.79 GB smaller on disk, at the same resident memory — but its Hindi is fluent nonsense on a content question: 5 of 5 replies invented a pizza recipe out of rocks, ice and sleep, while gemma3:4b answered all five correctly | Only with the model id pinned to the `-instruct-2507` tag. The family default `qwen3:4b` thinks, and Ollama's OpenAI-compatible endpoint forwards neither `think: false` nor `enable_thinking` — only `reasoning_effort: none`, which Ollama does forward |
| LLM | `gemma3:4b` | `gemma4:e2b` | **The fastest model measured here: 0.042 s to the first token** against 0.084 s, on the bot's own endpoint — but only with `reasoning_effort: none`, without which it thinks on that endpoint for 2.3 s. Its Hindi is correct (5 of 5 on both Hindi prompts, where `gemma4:e4b` answered 2 of 5 in English). It costs **6.89 GB resident against 2.62 GB**, on a box where memory is the binding constraint | A box with the headroom for it, and the flag wired into the Ollama provider |

Two more backends ship in the dependency set — `funasr` (SenseVoice)
and `pocket-tts` — and neither is registered. A swap is not a `.env`
edit until it has a factory in `bot/services.py` and a router in
`bot/language.py`; see "A new backend is a new implementation" in
`AGENTS.md`.

Swapping is a `.env` edit:

```bash
stt_provider=mlx-whisper
tts_provider=mlx-audio
```

One line is enough: the model that the other provider left behind is
resolved to the new provider's own model, so the service and the
routers always agree on what is loaded.

**A swap changes the measured numbers.** Re-run
`scripts/voice-ai.sh eval` and `scripts/voice-ai.sh bench`, and update
the tables below.

---

## The measured numbers

<!-- MEASURED:START -->
Measured on this machine — an M5 MacBook Air, 16 GB, macOS 27 — with
the other projects' stacks stopped. Every row below has a script behind
it: `scripts/voice-ai.sh memory`, `bench`, `eval`.

**These are baselines for this box and this corpus, not production
budgets.** What travels is the mechanism, the model weights and the
*rules* that re-derive a value on whatever machine they are asked
about. What does not travel is speed, memory headroom and latency:
those are properties of this Apple Silicon box, and a deployment's own
numbers have to be measured where it runs. A WER is a property of a
model on a corpus — FLEURS here — and a different corpus can reorder
the ranking. So nothing below is a claim about another machine, and no
threshold in the configuration is derived from it.

### The LLM tier

| | |
|---|---|
| Total unified memory | 16.0 GB |
| macOS baseline (active + wired + compressed) | 2.31 GB |
| Ollama, `gemma3:4b` resident | 2.62 GB |
| mlx-audio server, both models (ASR + TTS) | 0.45 GB |
| The agent's own process | 0.13 GB |
| Reserve for the other projects' stacks | 1.5 GB |
| **Available for the LLM** | **11.61 GB** |
| **Memory ceiling — the tier the rule selects** | **`gemma3:12b`** (8.16 GB) |
| **Tier that actually runs here** | **`gemma3:4b`** (3.35 GB) |

The rule is arithmetic, not preference: it subtracts everything that is
not the LLM and picks the largest Gemma 3 rung that fits. The ceiling
is `gemma3:12b`, whose weights are not on this machine. So the
*configured* tier is the largest rung that both fits and is installed,
and `scripts/voice-ai.sh apply-tier` writes that one and says which it
picked. On a machine with the 12B weights, the same rule configures
`gemma3:12b` with no edit.

The TTS is part of that arithmetic, and where it lives changes it. The
mlx-audio server holds both the ASR model and the Kokoro TTS model
(0.45 GB resident, measured); the in-process Kokoro swap adds 0.48 GB
to the agent's own process instead.

### The turn, stage by stage

One turn, measured end to end: a recorded utterance replayed through
the **real** pipeline — real STT, real TTS, real VAD, real turn
strategies — with a stub LLM that streams a fixed reply, so the number
does not move with the model's mood.
`STT_PROVIDER=mlx-audio scripts/voice-ai.sh turn-bench` (batch
transport); medians of five runs, on this machine, with the agent
stopped so nothing else competes for the CPU. Run-to-run spread is about
±0.05 s.

| Stage | Time | From the end of the user's speech |
|---|---:|---:|
| The VAD decides the user has stopped | 0.79 s | 0.79 s |
| The final transcript arrives | 0.77 s | 1.56 s |
| The model's first token | 0.20 s | 1.76 s |
| The first sound of the reply | 0.46 s | 2.23 s |

The first row is the silence window, in full (`vad_stop_secs = 0.8`); the
second is the STT request and the model's inference on the whole
segment; the third is the turn analyzer's verdict and the context
write; the fourth is the model's first sentence and then the TTS.

**Where the 0.8 s of STT time goes.** On this batch path the bot sends
the whole utterance to the STT in one request, and the model's cost
grows with the audio it is given: 2 s of audio takes 0.18 s to
transcribe, 8.8 s takes 0.69 s.
The 0.19 s a two-second clip suggests is not what a nine-second
utterance costs. Nothing in this repository is slow here — Pipecat's
batch STT transcribes the segment it was handed, and the model is
linear in its length.

**The streaming transport — the default — takes that 0.8 s to 0.14 s.**
Same clip, same VAD, same stub LLM, with `stt_provider=mlx-audio-realtime`
and the in-process Kokoro for the reply: the final transcript lands
**0.93 s** after the user stops instead of 1.56 s, and the STT stage
itself — VAD stop to transcript — is **0.14 s** against 0.77 s. The
audio was transcribed while the user was still speaking, so what is left
after the silence window is the buffered tail, not the whole segment.
`STT_PROVIDER=mlx-audio-realtime scripts/voice-ai.sh turn-bench
--backend kokoro --runs 3`, this machine; artifact
`data/turn_bench_realtime.json`. The reply's TTS is Kokoro here, which
the batch table above did not use — it changes the last row only, not
the two that move.

**The 0.8 s silence window is load-bearing.** The turn analyzer judged
**9 of 10** natural internal pauses in six FLEURS clips to be a
complete turn, and the ASR put a sentence-final full stop on **3 of 4**
mid-utterance fragments. A shorter window buys latency with answers to
fragments — which is what the traces already show ("Hello, what is
what? Please finish your question."). The window stays.

**Speaking the first clause.** The bot waits at most
`first_chunk_deadline_seconds` for the model's first whole sentence
and then speaks the first clause, with the rest of the sentence
following. That takes **0.21 s** off the time to the first sound
(0.668 s → 0.463 s):
`STT_PROVIDER=mlx-audio scripts/voice-ai.sh turn-bench --deadline 0
--out data/turn_bench_deadline0.json`, against the default-deadline
run above. A deadline of 0.4 s would buy nothing — the
model's first sentence is already complete by then — and 0.1 s would
buy 0.43 s at the price of a first chunk one word long. The cost is
prosody: the bot may say "Certainly," before the rest of the sentence.
Setting the deadline to 0 turns the cut off.

### STT speed

Measured through the batch HTTP endpoint (`/v1/audio/transcriptions`), on
FLEURS clips; the streaming default's time to the final transcript is in
the swap table above. Speed here is this machine's — Apple Silicon, MLX
on Metal — and a deployment on other hardware re-measures it with the
same script.

| Language | Clips | Audio | Inference | Real-time factor | Time to final transcript (p50 / p99) |
|---|---:|---:|---:|---:|---:|
| English | 9 | 80.4 s | 7.6 s | 0.094 | 0.94 s / 1.06 s |
| Hindi | 10 | 113.6 s | 11.8 s | 0.104 | 1.26 s / 1.80 s |

A real-time factor of 0.1 means the model transcribes ten seconds of
speech per second. The p99 the bot is configured with is **1.80 s**:
the batch transport's worst, and the larger of the two, so it also
covers a runtime switch to streaming (whose worst is 1.45 s). That is
`ttfs_p99_latency` in `.env.example`, written by
`scripts/voice-ai.sh apply-tier`.

### STT quality

WER over 40 FLEURS clips per language, through the batch HTTP endpoint
(`/v1/audio/transcriptions`, the transport the eval harness speaks; the
streaming default runs the same checkpoint). The numbers below use the
`standard` normalization — OpenAI's `whisper-normalizer`, which is what
every published FLEURS number is computed with.

| Model | `en_us` WER | `hi_in` WER | `en_us` CER | `hi_in` CER |
|---|---:|---:|---:|---:|
| Nemotron 3.5 ASR 0.6B | 8.56 % | **7.18 %** | 4.96 % | 4.29 % |
| Nemotron 3.5 ASR 0.6B, 8-bit | 8.45 % | 7.24 % | 4.92 % | 4.36 % |
| `whisper-large-v3-turbo` | **4.98 %** | 35.92 % | 2.42 % | 27.45 % |

This is the repo's own measurement, and it lands where the published
figures do: **Nemotron is 5.0× better on Hindi** (35.92 / 7.18) and
**Whisper is 1.7× better on English** (8.56 / 4.98). The 8-bit Nemotron
is within noise of the bf16 one, which is why it is the fallback tier
and not the default.

That ranking is a FLEURS ranking. FLEURS is read speech in a quiet
room; a deployment that hears phone calls, or a domain vocabulary, or
code-switching measures on its own corpus first — and the order can
change. What this table buys a deployment is the *harness*: the same
script, the same normalization and the same three views, pointed at a
different corpus.

The report holds two more views of the same corpus, and neither changes
the conclusion. The `indic` normalization (`indicnlp`) gives the same
numbers on this corpus. The `romanized` one is the standard
normalization with both scripts folded to Latin, so a model that answers
in Latin script is not punished for it: **7.13 %** for Nemotron on
Hindi, **35.19 %** for Whisper.

One caveat on precision: Whisper's transcription is not
bit-deterministic on Metal. Two runs over the same 40 clips differed by
about 1.5 WER points on Hindi — one character in a handful of clips.
Nemotron's output was byte-identical across the same check.

### TTS speed

Time to first audio — the gap between the bot deciding what to say and
the first sound of it. Five sentences (two English of different lengths,
one longer, two Hindi), each spoken through a real Pipecat pipeline,
so the number includes the service's own bookkeeping and the resampler.
Warm: the first request in each language loads the model and is
discarded, because a cold load is not what a conversation feels like.
`scripts/voice-ai.sh tts-bench`; this machine, the MLX model on Metal
and the ONNX build on the CPU.

| Backend | Time to first audio (p50) | (max) | Real-time factor |
|---|---:|---:|---:|
| `mlx-audio` + Kokoro-82M-4bit, Metal | **0.28 s** | 0.47 s | 0.07 |
| `kokoro`, in-process ONNX, CPU | 0.71 s | 1.22 s | 0.23 |

Run-to-run spread on this machine is about ±0.02 s on the medians, so
the numbers are quoted to the precision the measurement holds; the gap
between the two backends is an order of magnitude larger than that
spread.

The same voices on both sides (`af_heart` for English, `hf_alpha` for
Hindi), so the swap is a runtime change and not a voice change. The
mlx-audio backend is the default because it reaches the first sound
2.5× sooner; it needs the server, which the STT needs anyway. The
in-process Kokoro is the swap for a deployment that does not want the
server: it costs 0.48 GB in the agent's own process and 0.43 s more
before the first sound.
<!-- MEASURED:END -->

### The published comparison

These are the figures the measurements are compared against. They come
from NVIDIA's model card and from Whisper's published FLEURS baselines,
and they were computed on different hardware with a particular text
normalization — which is exactly why this repo measures its own. Every
published number the README quotes is recorded in
`evals/wer_eval.py` under `published_reference`.

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

The chunk size is a real dial, and the same model card reports what it
costs. Nemotron's MLX config defaults to `att_context_size: [56, 13]` —
the 1.12 s look-ahead, the most accurate of the four supported points.
On Hindi, 6.81 % at 1.12 s becomes 8.13 % at the 80 ms chunk, and
auto-detect goes from 8.23 % to 11.47 %.

---

## Architecture

```
browser (Pipecat's prebuilt client)
   │  WebRTC
   ▼
┌──────────────────────────────────────────────────────────┐
│ SmallWebRTCTransport                                     │
├──────────────────────────────────────────────────────────┤
│ STT          │ LocalMlxRealtimeSTTService (the default)  │
│              │  → mlx_audio.server, Nemotron 3.5 (Metal) │
├──────────────┼───────────────────────────────────────────┤
│ LLM          │ OLLamaLLMService → Ollama → gemma3        │
├──────────────┼───────────────────────────────────────────┤
│ routing      │ LanguageRoutingProcessor                  │
│              │   sentence split → script detect → voice  │
│              │   (VoiceRouter protocol: no backend named)│
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

The assistant aggregator sits **after** the transport output, so the
context records what was actually *spoken*: words cut off by an
interruption never reach the LLM's history.

The context shaper sits **between the user aggregator and the LLM**,
because two user messages in a row is not a conversation. It happens
when the user speaks again before the bot has answered: Pipecat cancels
the in-flight generation, the assistant message never lands, and the
next user message is appended after the one before it. The traces show
the result — a turn whose LLM span ended with no output and no error
(a cancellation, not a failure), followed by a context of `[user
"Hello.", user "Can you hear me?"]`, and a model that answered the
malformed history with *"Hello, what is what? Please finish your
question."* Consecutive user messages are merged into the one turn
they really are, before the model sees them.

### The observability stack

Three containers, on a network called `voice-ai-net`, plus Phoenix on
the host:

| Service | Where | Host port | Why that port |
|---|---|---|---|
| Phoenix | **the host** (`scripts/voice-ai.sh phoenix`) | 6006 (UI and OTLP/HTTP), 6007 (gRPC) | Docker Desktop's VM is capped at 1.93 GB and the `hear_help` stack already holds ~1.05 GB of it. Phoenix was OOM-killed at 1g, at 768m, and in `PHOENIX_LITE_MODE`. The collector reaches it over OTLP/HTTP at `http://host.docker.internal:6006` |
| otel-collector | container | 4319, 9464 | Not 4317/4318: the `hear_help` stack holds them. Only OTLP/gRPC is published — the collector's HTTP receiver stays inside the container network, because nothing on the host sends OTLP/HTTP |
| Prometheus | container | 9091 | Not 9090: the `hear_help` stack's Prometheus holds it on the host, and Phoenix's own Prometheus endpoint is off — it binds 9090 with no way to move it |
| Grafana | container | 3003 | Not 3000: `newotnite-api-1` holds it. Not 3002: hear_help's next-server holds `*:3002`, and a host process on the IPv6 wildcard beats Docker's `127.0.0.1` publish for any browser that resolves `localhost` to `::1` |
| Ollama | the host | 11434 | The LLM runtime; it is the one service that was already running here |
| mlx-audio | the host | 8020 | The STT and TTS server. The WER harness runs its own on 8021+ |

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

`bot/observers.py` owns the metric names. `observability/alerts.yml`
and `observability/grafana/dashboards/voice-ai.json` reference them by
name; `observability/prometheus.yml` is coupled to them by endpoint —
it scrapes the agent and the collector, it does not name a series.

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

Labels are bounded to a fixed set: `processor` and `model` on the
pipeline metrics, `kind` on the token counter, `component` on the memory
gauge. Never a session id, a user id or a call id.

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

Everything is a subcommand of one script, so there is one way in:

```bash
scripts/voice-ai.sh install   # venv + dependencies + the host-side Phoenix
scripts/voice-ai.sh models    # STT, TTS and LLM weights
scripts/voice-ai.sh start     # Phoenix, the observability stack, the STT server, the agent
```

`start` brings the pieces up in order and waits until each one is
actually ready, rather than sleeping and hoping. It leaves the agent in
the foreground; `--background` puts it in the background instead.
`scripts/voice-ai.sh help` lists every command, and
`scripts/voice-ai.sh status` says what is running.

Then open **http://localhost:7860/client** and talk to it.

| | |
|---|---|
| Agent | http://localhost:7860/client |
| STT mode switch | http://localhost:7860/settings (also a corner bar on the client page) |
| Phoenix (traces) | http://localhost:6006 |
| Grafana (dashboards) | http://localhost:3003 |
| Prometheus | http://localhost:9091 |
| Metrics (raw) | http://localhost:7860/metrics/ |

Grafana's login is `admin` / `admin`.

To stop everything again, in reverse:

```bash
scripts/voice-ai.sh stop
```

### Talking to it

Type in the client's message box, or speak. The bot replies in the
language you used.

The STT transport is switchable while the agent runs: the **STT** bar in
the client's bottom-right corner, or
[`/settings`](http://localhost:7860/settings), moves between batch HTTP
and the realtime WebSocket. The choice applies to the next connection —
a session keeps the transport it started with.

To force a language, send an RTVI message:

```json
{"type": "set_language", "language": "hi"}
```

`auto` (the default, and the only value the realtime transport accepts)
is Nemotron's language-ID conditioning on the batch transport — the mode
that survives code-switching. To pin `en` or `hi`, switch the transport
to batch (`/settings`, or `stt_provider=mlx-audio`).

### Measuring

```bash
scripts/voice-ai.sh memory          # the memory baseline, and the LLM tier it selects
scripts/voice-ai.sh bench           # STT speed, batch: real-time factor, time to final
scripts/voice-ai.sh bench --realtime # STT speed, streaming: TTFS p50/p99
scripts/voice-ai.sh turn-bench      # the turn, end to end (the default: streaming STT)
STT_PROVIDER=mlx-audio scripts/voice-ai.sh turn-bench  # the same turn, batch STT
scripts/voice-ai.sh eval            # FLEURS WER/CER, per model and per normalization
scripts/voice-ai.sh eval-scenarios  # behavioural evals (needs the agent running)
scripts/voice-ai.sh smoke           # synthesise speech, transcribe it back (needs: stt)
scripts/voice-ai.sh test            # the regression suite
```

`memory` writes `data/memory_baseline.json` and applies the tier
rule: it subtracts the measured macOS baseline, the STT, the TTS, the
agent and a 1.5 GB reserve for the other projects' Docker stacks, and
picks `gemma3:12b`, `gemma3:4b` or `gemma3:1b` from what is left.
`apply-tier` writes the result into `.env.example`.

### The other projects on this box

This is a shared 16 GB machine: the `hear_help` and `newotnite` stacks
are running here too, and they hold the memory and some of the ports
voice-ai needs. So voice-ai stops them before it runs — `start` does it,
and it is reversible:

```bash
scripts/voice-ai.sh others stop   # stop the hear_help and newotnite containers
scripts/voice-ai.sh others start  # bring them back exactly as they were
```

Only `docker stop` is used: no container is removed, no volume is
touched, no configuration is changed. The 1.5 GB reserve in the tier
rule is the headroom left for them.

### Air-gapped

Every model is downloaded by `scripts/voice-ai.sh models` before the
demo. After that, disconnect the network and the conversation still
works — that is the claim, and it is only true because
`PHOENIX_ALLOW_EXTERNAL_RESOURCES=false` and nothing else reaches out.

The boundary is the *agent*. `scripts/voice-ai.sh bench` and `eval`
fetch the FLEURS clips and their models from the hub, so the
measurement scripts need the network; the agent never does.

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
