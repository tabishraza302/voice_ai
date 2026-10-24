# AGENTS.md — working on this repository

Last reviewed: 2026-10-19.

This file is the contract for anyone (human or agent) changing this
repository. It is short on purpose: the rules below are the ones that
have actually prevented mistakes, not a restatement of the code.

## What this project is

A fully local, air-gapped English + Hindi voice agent with an AI
observability layer, on one Apple Silicon MacBook Air (M5, 16 GB unified
memory). Nothing in it talks to the internet at runtime, and the README's
numbers are measured on this machine, not copied from a paper.

## The rules that matter

### 1. Measure before you assume

Every claim in the README has a script behind it:

| Claim | Script |
|---|---|
| Which LLM tier fits | `scripts/voice-ai.sh memory` |
| How fast the STT is | `scripts/voice-ai.sh bench` |
| How accurate the STT is | `scripts/voice-ai.sh eval` |

If you change a model, a prompt, a chunking rule or a latency budget,
**re-run the relevant script and update the README's table with the new
number**. A number in the README that no longer matches a measurement is
worse than no number.

Do not cite a remembered benchmark. The published figures are recorded in
`evals/wer_eval.py` under `published_reference`, with their source, and
they are the *comparison* baseline — the measured numbers are the ones
this repo publishes.

**A measurement is a claim about the conditions it was measured under.**
Speed and memory are properties of this machine; a WER is a property of
this model on this corpus; a latency budget is a property of this
deployment. Say the conditions with the number, every time.

**A local baseline is not a production number.** Anything another
deployment depends on — a threshold, a budget, a tier, a timeout — must
be re-derived in that deployment's environment, from that environment's
own measurements. The rule that *re-derives* travels; the number it
produced here does not. The tier rule is the model for this: it re-runs
the memory baseline on whatever machine it is asked about, so the same
rule selects the right tier in production. A hard-coded number would
not.

So: a rule that needs a value from the environment takes it from config,
and the config is where the deployment's own value goes. Never smuggle a
number measured here into a claim about somewhere else.

### 2. Keep the three layers swappable

STT, TTS and LLM are selected by config, and swapping one must not
require touching the pipeline, the observers, the collector config, the
metric names, the dashboard, the alert rules or the eval scenarios.

The mechanism, in three places:

- `bot/services.py` — the three registries. A new backend is a factory
  and a registry entry.
- `bot/language.py` — the `VoiceRouter` and `SttLanguageRouter`
  protocols. Settings deltas are backend-specific types, so the delta
  construction lives behind a protocol, one implementation per backend.
- `bot/pipeline.py` — names no backend at all.

`tests/test_modularity.py` enforces this mechanically, including an
assertion that the pipeline module imports no concrete service class. If
a change to add a backend makes that test fail by importing a backend
into the pipeline, the change is wrong — fix the design, not the test.

### 3. A new backend is a new implementation, not a special case

When you add a backend:

- add a factory in `bot/services.py` and register it;
- add its router in `bot/language.py` and register it there too — a
  backend with a service but no router is a latent bug, because the
  first language change will push a delta it silently ignores;
- add it to the swap catalogue in the README, with its **real** cost
  (memory, quality, latency) and the trigger for using it.

Never add a branch like `if settings.stt_provider == "x"` inside the
pipeline. That is the thing the registries exist to prevent.

### 4. The metric names are a contract

`bot/observers.py` lists them in `METRIC_NAMES`.
`observability/alerts.yml` and
`observability/grafana/dashboards/voice-ai.json` reference them by
name; `observability/prometheus.yml` is coupled to them by endpoint —
it scrapes the agent and the collector, it does not name a series.
Renaming one breaks the alerts and the dashboard, silently.
`tests/test_observers.py` asserts the declared set matches the
instruments.

Label cardinality is bounded to a fixed set: `processor` and `model` on
the pipeline metrics, `kind` on the token counter, `component` on the
memory gauge. **Never** add a session id, a user id or a call id — they
are unbounded, and Prometheus is the wrong place to store them.

### 5. Versions are pinned exactly

`pyproject.toml` pins every dependency. The WER and latency numbers are
only reproducible against these versions. If you add a dependency, pin
it. If you must relax a pin — `soundfile` and `soxr` are constrained by
Pipecat's own ranges — say why in a comment next to it.

Watch for transitive conflicts: Pipecat 1.13.0 requires
`soundfile~=0.13.1` and `soxr~=1.0.0`, so the newer majors do not
resolve.

### 6. Resource isolation and port hygiene

This project runs locally alongside user and system processes.

The project ports are shifted off standard defaults to prevent collisions:
4319 for OTLP (avoiding 4317), 3003 for Grafana (avoiding 3000), 6006 and
6007 for Phoenix, 9091 for Prometheus (avoiding 9090), 8020 for the STT server,
7860 for the agent. Ollama keeps its standard 11434. **Check for a conflict before
adding a port** — and check for host processes, not just containers.

Memory is the binding constraint on Apple Silicon: unified memory is shared between
GPU, models, OS, and containers. The `mem_limit` on every container in
`docker-compose.yml` is mandatory to prevent pushing the host into swap.

**Phoenix runs on the host, not in Docker.** Docker Desktop's VM memory limits
leave insufficient headroom for Phoenix. `scripts/voice-ai.sh install` puts
it in its own `uv tool` environment; `scripts/voice-ai.sh phoenix` runs
it on the host, and the collector reaches it over OTLP/HTTP at
`http://host.docker.internal:6006`. Its internal Prometheus endpoint is off:
it binds 9090 with a hardcoded port and no env var to move it.

**Phoenix files a trace under `openinference.project.name`,** not under
`service.name`. With only `service.name`, every trace lands in Phoenix's
`default` project and the traces look empty even though they arrived.
The collector sets it, because the collector is the Phoenix boundary.

### 7. Air-gapped means air-gapped

`PHOENIX_ALLOW_EXTERNAL_RESOURCES=false`. Every model is downloaded
before the demo, and the demo runs with the network off. If you add a
component that reaches out, it breaks the project's central claim.

## Layout

```
bot/            the agent
  config.py       settings, one selector per swappable layer
  services.py     the three registries
  language.py     script detection, the router protocols, the chunker
  stt_service.py  the local mlx-audio STT service
  tts_service.py  the local mlx-audio TTS service
  pipeline.py     the language-routing processor
  observers.py    the Prometheus metrics contract
  tracing.py      OpenTelemetry setup
  bot.py          the runner entrypoint
evals/          the quality harness
  wer_eval.py     FLEURS WER/CER, per model and per normalization
  scenarios/      behavioural eval scenarios
scripts/        model pulls, the memory baseline, the STT benchmark
tests/          the regression suite
observability/  the stack's config, mounted into docker-compose.yml
  otel-collector.yaml
  prometheus.yml
  alerts.yml
  grafana/dashboards/voice-ai.json
  grafana/provisioning/  datasource + dashboard provider
data/           generated measurements (gitignored)
```

## Before you call a change done

- `scripts/voice-ai.sh test` passes.
- If you touched a model, a prompt or a latency path: the relevant
  measurement script has been re-run and the README updated.
- If you touched a metric: `tests/test_observers.py` passes, and the
  dashboard and alert rules still reference the right names.
- If you added a backend: `tests/test_modularity.py` passes without
  edits, and the README's swap catalogue lists it.
- Nothing under `data/` or `evals/fixtures/audio/` is committed.
