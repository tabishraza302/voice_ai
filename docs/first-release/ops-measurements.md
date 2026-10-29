# First-release operations budgets and measurements

## Qualification status

The ops code was exercised against the provided disposable PostgreSQL service at `127.0.0.1:56884`. This is a correctness-test service, not the local Compose database or a deployment profile. No target deployment model/audio corpus/concurrency tuple or named alert-owner roster is declared in the repository. Target speech latency, throughput, memory high-water and quality are therefore **NOT MEASURED / NOT QUALIFIED**. The configured defaults below are policy guardrails, not performance claims or production SLOs.

## Operations limits

`PlatformSettings` values and environment variables:

| Setting | Default | Enforced limit |
|---|---:|---|
| `PLATFORM_OPS_EXPORT_MAX_BYTES` | 16,777,216 (16 MiB) | Maximum JSON export bytes. |
| `PLATFORM_OPS_EXPORT_MAX_RECORDS` | 10,000 | Combined records read into an export, including related audit records. |
| `PLATFORM_OPS_RETENTION_BATCH_SIZE` | 500 | Rows per table and pass; service hard ceiling is 1,000. Repeat audited passes to continue. |
| `PLATFORM_OPS_BACKUP_MAX_BYTES` | 8,589,934,592 (8 GiB) | Maximum completed archive bytes. |
| `PLATFORM_OPS_RESTORE_MAX_BYTES` | 8,589,934,592 (8 GiB) | Maximum archive input bytes. |

These default values are not measured capacities. Each deployment must set and review them against its own data volume, largest rows, storage budget, database load and maintenance window. CLI overrides may lower but not exceed configured maxima. Keep archive storage protected and encrypted at rest; the backup archive itself is not encrypted by this CLI.

Every operations command emits `elapsed_seconds` and relevant counts/size. Export and backup report artifact size and row counts; retention/deletion report per-table redaction counts; restore reports restored table counts. These whole-command timings do not separate SQL, COPY, filesystem or audit time. Preserve outputs with the change record, but do not use a single duration as a latency percentile.

## Checked-in local Compose profile

Facts below come from `docker-compose.yml` and describe the local profile only. They are not target deployment budgets. Runtime container health and host-port availability were not checked in the PostgreSQL-only operations tests.

| Service | Declared `mem_limit` | Host bind / mapping | Declared dependencies/network |
|---|---:|---|---|
| `otel-collector` | 384 MiB | `127.0.0.1:4319 -> 4317`; `127.0.0.1:9464 -> 9464` | `voice-ai-net` at `10.88.0.4`; reaches host Phoenix on port 6006. |
| `prometheus` | 384 MiB | `127.0.0.1:9091 -> 9090` | `voice-ai-net` at `10.88.0.2`; scrapes host agent and collector. |
| `grafana` | 512 MiB | `127.0.0.1:3003 -> 3000` | `voice-ai-net` at `10.88.0.3`; reads Prometheus. |
| `postgres` | 256 MiB | `127.0.0.1:${POSTGRES_PORT:-5439} -> 5432` | `voice-ai-net` at `10.88.0.5`; persistent local volume. |
| Phoenix | Host-run | Host port 6006 | Local script-managed; not a Compose container. |

The declared network is fixed `10.88.0.0/24`. Before starting the local profile, inspect listeners and compose configuration without printing environment secrets (`docker compose config --quiet`, `docker compose ps`, `lsof -nP -iTCP -sTCP:LISTEN`). Resolve port or subnet conflicts in the deployment's own profile. The operations CLI adds no service or port; it needs PostgreSQL, a protected local output directory, and the role permissions in [the runbook](ops-runbook.md). The test PostgreSQL endpoint at port `56884` is isolated and not a deployment port.

## Existing voice alert policy

`observability/alerts.yml` has a 2.0-second end-to-end p95 warning and a 12,000,000,000-byte resident-memory alert. The alert file describes 2.0 seconds as an initial guess; the memory rule describes the local 16-GB Apple Silicon host. This task did not change those rules. Neither threshold is evidence for another deployment. The rules do not specify a named/team owner or routing destination. Map the runbook's platform on-call, database owner, privacy/security approver and incident commander roles to the deployment roster before production; alert-owner verification is **NOT QUALIFIED** here.

## Speech measurements: not measured

| Required measure | Conditions to record | Result |
|---|---|---|
| User-stops-speaking to bot-starts-speaking p50/p95/p99 | Exact STT/LLM/TTS models+versions, target host/container profile, audio/corpus hashes and format, concurrency, run count | **NOT MEASURED**; target tuple and corpus not supplied. |
| STT finalize/real-time factor, LLM TTFT/tokens per second, TTS TTFA | Same pinned models, audio and host; report stage conditions | **NOT MEASURED**. |
| Session throughput, process/container memory high-water, DB/storage load | Target profile, concurrency, duration, steady/peak window | **NOT MEASURED**. |
| WER/CER or speech quality | Pinned language/corpus/audio/model tuple and repository evaluation procedure | **NOT MEASURED**. |

Do not fill results from alert thresholds, another machine, test-suite time, or remembered benchmarks. Once the deployment and fixtures are selected, run `scripts/voice-ai.sh turn-bench --audio FILE --backend kokoro|mlx-audio --runs N` and the matching STT/TTS/LLM benchmarks and language-quality evaluation. Record exact model identifiers/hashes, hardware/container limits, corpus/audio hashes, concurrency, warm/cold conditions, runs, percentiles, memory and quality with results. Re-derive latency and storage budgets from those conditions and place target values in deployment configuration. The backup/restore round-trip test establishes seeded data correctness in an isolated test database; it does not measure production RPO, RTO, restore duration, availability or throughput.