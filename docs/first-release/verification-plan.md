# First-release verification and fixture plan

**Status:** implementation plan, inspected 2026-10-28. No test, service, build,
dependency install, measurement, or qualification command was run while writing
this document. Commands under “new” are acceptance interfaces to implement,
not commands claimed to exist today.

**Goal:** make every first-release gate executable, including fresh PostgreSQL
initialization, isolation between two users, shared flow execution, browser
journeys, bounded prepaid accounting, and actual audio-input qualification.

**Architecture:** the new control plane is `platform_api/`, with its own fresh
PostgreSQL database and initial schema. Shared flow logic is a pure package at
`bot/flow/`. Basic prepaid accounting lives in `platform_api/billing/`, uses
integer paise, small per-session reservations, and PostgreSQL wallet-row locks.
There are no legacy data conversion, Alembic migration, account coordinator,
shared account-pool, epoch, or pool-subgrant acceptance requirements.

**Execution:** implement tasks inline or under the implementation workflow
selected for the encompassing specification. This document authorizes no app
change by itself. Test harness adapters consume the auth and flow contracts;
they do not choose token formats, status enums, endpoint paths, flow semantics,
retail rounding, or crash-refund policy on behalf of those contracts.

## Global constraints and review focus

- Preserve `scripts/voice-ai.sh test`, Bun unit tests, local `/client`, service
  registries, backend-neutral pipeline imports, and metric-name contracts.
- Every executable gate records commit, exact command, dependency lock hashes,
  operating mode, environment, selected models/config hashes, and output.
- A local number qualifies only the machine, models, corpus, concurrency and
  configuration named with it. Deployment limits come from that deployment's
  measurements and config, not constants copied from this laptop.
- No committed audio under `evals/fixtures/audio/`, no generated data under
  `data/`, no credentials or browser authentication state in artifacts.
- Review inputs that are easy to miss: missing test prerequisites; competing
  sessions spending the last credit; cross-user IDs in otherwise valid
  requests; publish/edit races after a session pins config; and an apparent
  voice pass that injected text rather than testing STT.

## 1. Inspected inventory: what actually exists

All existing commands below run from `/Users/artist/Projects/voice_ai` unless
the command explicitly changes directory. They describe source behavior, not
an execution result from this planning session.

- `scripts/voice-ai.sh test` currently calls `uv run pytest`. Pytest discovers
  `tests/`, uses repository Python path, `-q`, and `asyncio_mode = "auto"`.
  It does not run browser tests or Bun tests. PostgreSQL and live voice can
  skip in this command, so a zero exit code alone is not a release gate.
- `uv run pytest tests/test_agent_postgres_integration.py` currently skips
  without `TEST_DATABASE_URL`. With an async SQLAlchemy PostgreSQL URL it
  creates a UUID-named schema, initializes existing `bot.db.base.Base`
  metadata, exercises concurrent prompt saves/activation/seeding, then drops
  its schema. It does not exercise `platform_api/` or its future schema.
- `RUN_LIVE_VOICE=1 uv run pytest -s tests/test_voice_live_integration.py`
  expects an already-running agent at `http://127.0.0.1:7860`, a seeded
  `bilingual-assistant` preset and available real local LLM/TTS. It creates an
  aiortc peer with `iceServers=[]`, a **silent synthetic microphone track**,
  sends English/Hindi **text** with `send-text`, checks scripts in replies and
  a non-silent remote audio peak. This is text-to-LLM-to-TTS transport smoke;
  it supplies no evidence of audio-input STT or microphone permission UI.
- `cd web && bun test` runs existing `src/lib/*.test.mjs` tests. These include
  injected fake peer/media/fetch lifecycle tests and a disposable Bun HTTP
  server. `bun run typecheck` runs `tsc --noEmit`; `bun run build` runs Next
  build with telemetry disabled. No Playwright dependency/config exists.
- `scripts/voice-ai.sh start --background` starts the current local stack:
  host Phoenix, Docker observability/PostgreSQL, Ollama/local models, Studio,
  mlx-audio server, then agent. It is not a disposable test provisioning
  command. `stop` stops this local stack; never use it as generic cleanup for
  verification-owned processes or another user's running stack.
- `scripts/voice-ai.sh memory`, `bench`, and `eval` are existing measurement
  entrypoints; `smoke` calls `scripts/smoke_stt.py`. Other measured paths have
  scripts such as `bench_turn.py`, `bench_tts.py`, and
  `bench_agent_profiles.py`; record their actual CLI arguments when used.
  Current wrapper commands are `scripts/voice-ai.sh tts-bench both`,
  `scripts/voice-ai.sh profile-bench`, and
  `scripts/voice-ai.sh turn-bench --audio /absolute/fixture.wav --runs 5 --backend mlx-audio --out /absolute/result.json`.
  The turn benchmark uses real audio/STT/TTS with a **stub LLM streaming a
  fixed reply**; it cannot certify actual-model end-to-end latency.
- Existing API/service fixtures generally initialize in-memory SQLite with
  SQLAlchemy; those remain useful for existing behavior but cannot qualify
  PostgreSQL locks, isolation, constraints or financial concurrency.
- Existing dev pins are `pytest==9.0.1`, `pytest-asyncio==1.3.0`. Runtime pins
  already include `sqlalchemy==2.0.38`, `asyncpg==0.30.0`,
  `aiosqlite==0.22.1`. `uv.lock` already resolves `httpx==0.28.1`,
  `aiortc==1.15.0`, `aiohttp==3.14.5`, `uvicorn==0.54.0`.
- Inspected local executables report Python 3.12.13, Bun 1.3.14, Node
  v24.15.0, uv 0.11.25. These are inventory evidence, not deployment minimums
  or a promise that browser binaries/models are installed.
- There is no `.github/` workflow, shared `tests/conftest.py`, test PostgreSQL
  compose file, release verifier, or browser E2E suite in the inspected tree.
  `docs/`, `data/`, `evals/`, and audio extensions are ignored by `.gitignore`.

## 2. Minimal tooling decisions

Keep pytest, pytest-asyncio and Bun. Add **only** `httpx==0.28.1` to Python dev
dependencies so the ASGI test transport is a declared dependency rather than
an accidental transitive import. Use `httpx.ASGITransport` with explicit app
lifespan entry/exit; no extra lifespan, clock, property-testing, Docker-control
or retry packages are needed. A seeded stdlib `random.Random` state-machine
test is sufficient initially; always print its seed on failure.

Add `@playwright/test: "1.64.1"` to `web/package.json` devDependencies and
update `web/bun.lock`. Microsoft published this stable version on 2026-10-14;
the pin is verified against the [official release](https://github.com/microsoft/playwright/releases/tag/v1.64.1).
Use the installed package CLI (`bun run test:e2e`, or
`bun ./node_modules/@playwright/test/cli.js`) so a missing dependency fails
without fetching an unpinned package through `bunx`. Install matching browser
binaries only during explicit preparation; Playwright versions require
[matching browser binaries](https://playwright.dev/docs/browsers).

Use Chromium for required automated browser journeys. Firefox/WebKit get
separate qualification only if advertised as supported. Avoid claiming a
three-engine support matrix from a Chromium result. Configure one worker,
zero retries, `forbidOnly: true`, explicit base URL, trace on failure and JSON
reporting; `reuseExistingServer: false`. Playwright supports these runner
[configuration options](https://playwright.dev/docs/test-configuration).

Preserve `"test": "bun test"`; add `"test:e2e": "playwright test"` and
`"test:e2e:list": "playwright test --list"`. Put Playwright specs in
`web/e2e/*.spec.ts` and add `web/bunfig.toml` with `[test] root = "./src"`
so `bun test` never imports Playwright specs.

## 3. New command contract and environment

Add one dispatcher to `scripts/voice-ai.sh`, keeping its current subcommands:

```text
scripts/voice-ai.sh verify prepare
scripts/voice-ai.sh verify unit
scripts/voice-ai.sh verify postgres
scripts/voice-ai.sh verify browser
scripts/voice-ai.sh verify voice --profile local_offline
scripts/voice-ai.sh verify voice --profile deployment --config /absolute/qualification.json
scripts/voice-ai.sh verify release --profile deployment --config /absolute/qualification.json
scripts/voice-ai.sh schema init
```

Implement the verifier as `scripts/verify.py`, invoked by the shell with
`exec uv run --frozen python scripts/verify.py ...`. It resolves repository
root from its own path; callers may invoke the shell from any directory.
Every gate writes an ignored run directory
`data/verification/<UTC timestamp>-<uuid>/`, including `manifest.json`,
`commands.json`, JUnit/Playwright results, redacted logs and `result.json`.
`result.json` names each required gate `passed`, `failed`, or `not_run` and
has a nonempty reason for every failure/not-run. Only all required `passed`
results permit overall success.

New environment keys, read by the verifier rather than inherited blindly:

- `VERIFY_REQUIRED=1`: required tests may not skip/xfail or select zero tests.
  The verifier sets this itself for every gate, including local runs of
  `verify`. Raw `pytest` retains its opt-in developer behavior.
- `PLATFORM_TEST_DATABASE_ADMIN_URL`: optional URL to a dedicated disposable
  PostgreSQL cluster with database-creation privilege. It must target a
  verification-owned cluster and identify the run; never accept a production
  URL or the local default `voice_ai` database. Without it, provision the
  isolated container below. Tests do not log the credential-bearing URL.
- `PLATFORM_DATABASE_URL`: injected by the verifier for the freshly created
  database only. The new app must not fall back to SQLite. `TEST_DATABASE_URL`
  points at a different disposable database for existing bot PostgreSQL tests.
- `VERIFY_ARTIFACT_DIR`: verifier-owned absolute run directory. User-supplied
  artifact paths may not overwrite source files or prior evidence.
- `VERIFY_AUDIO_ROOT`: absolute directory of pre-provisioned, checksummed
  English/Hindi speech fixtures. Required only for actual voice qualification.
- `VERIFY_QUALIFICATION_CONFIG`: absolute, reviewed deployment-specific JSON
  containing allowed origins, selected backend/model identifiers and hashes,
  runner/API endpoints, TURN configuration when required, concurrency,
  configured timeout/latency/quality limits and their baseline artifact IDs.
  No endpoint credentials go into the exported evidence.

Tests/app processes receive a temporary explicit settings file, no ambient
repository `.env`, fresh signing secrets and test-only users. Fake adapters
are injected by test app factories, never enabled by a public API parameter.
Telemetry/remote fonts are disabled. Fake suites block non-loopback egress;
`local_offline` qualification additionally asserts that no external network
dependency is invoked. Deployment qualification allows only endpoints named
by its profile and records them.

### Preparation and lifecycle

`verify prepare` is explicit setup, not part of any runtime test. From root it
runs `uv sync --frozen --extra dev`; in `web/` it runs
`bun install --frozen-lockfile`, then
`bun ./node_modules/@playwright/test/cli.js install chromium`. Linux CI image
preparation additionally installs Chromium OS dependencies. Online downloads
belong to this setup step; offline test/voice commands must never install
anything. Record tool versions and dependency/browser hashes. Missing tools
or unavailable pinned binaries fail preparation.

Add `tests/compose.postgres.yml`: a single `postgres:16-alpine` service
(same major image family as the existing stack), `mem_limit: 256m`, fresh
temporary storage, no persistent named volume, localhost-only **dynamic**
published port `127.0.0.1::5432`, healthcheck `pg_isready`, unique project
name `voice-ai-verify-<uuid>`. Record resolved image digest, PostgreSQL server
version, actual host port and limits in evidence. Preparation pulls/caches
the image; test commands use the cached image and may not pull.

For `postgres`, `browser`, and `release`, the verifier uses Docker Compose
with `--project-name <owned-name> --file tests/compose.postgres.yml`, inspects
its allocated port, and verifies an actual PostgreSQL connection. Reject
startup or unhealthy DB with failure, never skip. Dynamic bindings avoid a
new fixed project port. For owned API/web child servers bind an OS-assigned
loopback port, keep the socket until child handoff when supported, otherwise
check host listeners immediately before launch and fail/retry a bind race;
never attach to a pre-existing listener. Browser base URLs are injected from
the resulting manifest, not hard-coded to 3001 or 7860.

Fresh database names are `platform_verify_<uuid>` and
`legacy_verify_<uuid>`. The DB fixture creates each database from the empty
template, initializes the target metadata once through the same production
initializer, and uses independent sessions/connections for each participant.
Each test gets its own platform database; concurrency participants share only
that test's database. Reject SQLite and databases without the verified run
ownership token. Do not recreate tables on every API request.

Every gate owns and closes its HTTP clients, peer connections, app lifespans,
engines and child processes. In `finally`, terminate only recorded owned
process groups, wait then force-stop only those groups, dispose DB pools,
drop run-owned test databases, and run Compose `down --volumes --remove-orphans`
on the exact owned project. An interrupt triggers the same cleanup and exits
nonzero. Cleanup failure also fails the gate. Do not call the current global
`voice-ai.sh stop`, use broad `pkill`, drop unowned schemas/databases, or
remove unrelated Docker containers. Preserve redacted artifacts on failure.

### Exact gate matrix

1. **Existing regression:** root `scripts/voice-ai.sh test`; web `bun test`,
   `bun run typecheck`, `bun run build`. Existing PostgreSQL/live skips remain
   visible and permitted here. This is a developer regression result only.
2. **New `verify unit`:** root
   `uv run --frozen pytest tests -m 'not postgres and not browser and not live_voice and not qualification' --strict-markers --junitxml=<run>/python-unit.xml`;
   web `bun test`, `bun run typecheck`, `bun run build`. Mark existing optional
   PostgreSQL and voice modules appropriately so their prerequisites do not
   leak into this lane. Required selected tests may not skip or xfail. At
   least one Python and one Bun test must run; missing reports fail.
3. **New `verify postgres`:** provision disposable PostgreSQL, then root
   `uv run --frozen pytest tests/test_agent_postgres_integration.py tests/platform_api -m postgres --strict-markers --junitxml=<run>/postgres.xml`.
   Populate the separate legacy/platform DB environment described above. No
   model server, gateway or browser is needed for this lane. Phase 8 adds its
   actual disposable RabbitMQ fixture for tests marked `broker`; until then
   no broker tests exist. Once Phase 8 is present, this gate also executes
   its required `postgres and broker` cases rather than excluding them.
4. **New `verify browser`:** provision PostgreSQL, start owned control-plane,
   fake-runner and built Studio servers with temporary settings, then in web
   `bun run test:e2e --project=chromium --reporter=json`. Capture stdout to
   `<run>/playwright.json` and traces under `<run>/browser/`. Real HTTP auth,
   ownership and PostgreSQL are exercised; deterministic model/transport
   adapters make this lane independent of model downloads and inference.
5. **New `verify voice --profile local_offline`:** use an isolated configured
   real local model/runner stack and checksummed speech inputs, root
   `uv run --frozen pytest tests/qualification/test_voice_audio_input.py tests/qualification/test_flow_adapter.py --strict-markers --junitxml=<run>/audio.xml`.
   Set `RUN_AUDIO_QUALIFICATION=1`, `VERIFY_REQUIRED=1`. No text injection is
   allowed in this lane. Execute the existing text/silence smoke separately
   with `RUN_LIVE_VOICE=1`; it has its own result and cannot substitute for
   audio-input results. Support configurable test endpoint in that existing
   module instead of assuming it always owns 7860.
6. **New `verify voice --profile deployment --config ...`:** same real-audio
   tests plus `tests/qualification/test_public_transport.py`, against a
   dedicated test account/deployment. Verify TLS, actual supported browser
   media, relay transport when TURN is required, interruption/reconnect,
   measured concurrency, and configured latency/quality budgets. No public
   availability claim follows from a loopback-only test.
7. **New `verify release --profile deployment --config ...`:** run `unit`,
   `postgres`, `browser`, both voice profiles, then
   `uv run --frozen pytest tests/qualification/test_backup_restore.py tests/qualification/test_release_evidence.py --strict-markers --junitxml=<run>/release-evidence.xml`.
   Require all first-release phase gates, including Flow QA and recovery
   evidence. A deployment lacking the required local-offline qualification
   capability consumes a signed-off artifact for the same commit/config
   from the nominated compatible qualification runner; absence fails.

`schema init` is a new production/bootstrap command, separate from verify.
It requires an explicit `PLATFORM_DATABASE_URL`, creates the initial platform
schema in an empty database, and validates an already-current schema without
data changes. Serialize concurrent initializers with a database transaction
lock. Existing incompatible schemas fail with a clear fresh-install error;
there is no conversion/backfill path and no legacy bot DB import. Tests call
the same initializer, not a parallel test-only SQL schema.

## 4. Fixture interfaces

Create `tests/support/` as the shared home; `tests/conftest.py` registers
markers and imports fixtures. Interfaces below are harness APIs only. Domain
objects and endpoint expectations come from the auth/flow/billing contracts.

- `fresh_pg` (`tests/support/postgres.py`) yields `FreshPostgres` with
  `url: str`, `sessions: async_sessionmaker[AsyncSession]`,
  `new_session() -> AsyncSession`, `initialize() -> None` (async), and
  `snapshot() -> AccountingSnapshot` (async, for committed state). No shared
  rollback transaction: independent connections and real commits are
  mandatory for lock/crash tests. Teardown drops only its owned database.
- `fake_clock` (`tests/support/clock.py`) provides `now_ns() -> int`,
  `advance_ns(delta: int) -> None`, and async
  `sleep_until_ns(deadline: int)`. It has monotonic integer time, rejects
  negative advances, and wakes scheduled waiters deterministically. Domain
  timers take this injected clock; wall-clock timeouts exist only as harness
  deadlock guards. No global monkeypatch of asyncio or SQL server time.
- `fake_models` (`tests/support/models.py`) exposes STT/LLM/TTS factories
  conforming to current backend interfaces. A scripted ordered frame list
  can delay until an `asyncio.Event`, raise at a named boundary, stream two
  fragments, or produce a late cancelled fragment. STT records received PCM;
  LLM records immutable prompt/config hash; TTS records text and cancellation.
  Each fake records creation/close counts and refuses external HTTP. These
  qualify orchestration, never model quality or actual STT recognition.
- `two_users` (`tests/support/auth.py`) yields two independent user/owner
  records, an admin and separate authenticated HTTP clients/session jars.
  `client_for(principal)`, `expire_login(principal)`, and `logout(principal)`
  use the chosen auth contract. Test-only record construction may prepare
  accounts, but login/logout/expiration/browser tests use the real auth
  implementation. No universal fake identity bypass. Enumerate cross-user
  resource/action cases from the actual router/OpenAPI inventory.
- `pinned_configs` (`tests/support/configs.py`) creates two immutable
  revisions through the contract and exposes `first`, `second`, and
  `publish_second: asyncio.Event`. An explicit barrier pauses admission
  after its config read, publishes revision two using another DB connection,
  then resumes. Observe recorded hashes from compiler, chat and voice
  adapters; do not infer pinning from a profile ID alone.
- `billing_harness` (`tests/support/billing.py`) exposes independent request
  clients, a deterministic operation-key factory, `pause_at(boundary)` and
  `resume(boundary)` barriers, and committed wallet/ledger/reservation
  snapshots. Named failure boundaries are `before_commit`,
  `after_commit_before_response`, `after_usage_persist_before_settlement`,
  and `after_settlement_commit_before_ack`. Reservation amount/rate/rounding
  come from test config and the billing contract, not a deployment default.
- `browserPeerTransport` (`web/e2e/fixtures/peer.ts`) supplies an explicit
  fake media/peer/data-channel implementation through the same dependency
  seam used by `VoiceSessionDependencies`. Track open/close/cancel/send
  calls and deliver ordered/late messages. Keep the existing Bun fake-peer
  suite; Playwright uses it only for DOM journeys and accurately labels
  those results `fake_transport`. A separate real browser peer fixture
  sends actual PCM in qualification. Fake peers cannot satisfy that gate.
- `flow_cases` (`tests/fixtures/flow_cases.py`) creates minimal legal/illegal
  source graphs and expected compiler/validator outcomes according to the
  shared flow contract. JSON examples must validate against its actual
  schema; do not invent parallel graph fields in tests. Test shared flow
  imports in a clean process with DB/network/backend imports denied.
- `audio_corpus` (`tests/support/audio.py`) reads a committed metadata
  manifest at `tests/fixtures/audio_manifest.json`, with fixture ID,
  language, expected normalized reference, license/provenance, SHA-256,
  sample format and local filename. Actual audio is provisioned under
  `VERIFY_AUDIO_ROOT`/ignored data storage. Missing/corrupt/silent/incorrect
  format input fails qualification. Silence remains a separate negative
  case. A transport wrapper records that speech PCM crossed the sender and
  STT input; no `send-text` fallback is allowed.

## 5. Implementation phases, exact files and assertions

These are verification work packages attached to the encompassing release
phases. For each: first write the named failing tests, run the corresponding
new gate, implement only its harness/production contract needs, rerun the
gate, then keep the evidence with that phase. **Delete: none** in every
package; a first release needs no legacy migration-test removal.

### Phase 0 — executable infrastructure and honest qualification

Create `scripts/verify.py`, `tests/__init__.py`, `tests/conftest.py`,
`tests/compose.postgres.yml`, `tests/support/__init__.py`,
`tests/support/qualification.py`, `tests/support/postgres.py`,
`tests/support/clock.py`, `tests/support/models.py`,
`tests/test_verification_policy.py`, `tests/test_verifier_lifecycle.py`,
`web/bunfig.toml`, `web/playwright.config.ts`,
`.github/workflows/verify.yml`, `.github/workflows/qualify.yml`.
Modify `scripts/voice-ai.sh`, `tests/test_voice_ai_script.py`,
`tests/test_agent_postgres_integration.py`, `tests/test_voice_live_integration.py`,
`pyproject.toml`, `uv.lock`, `web/package.json`, `web/bun.lock`, `.gitignore`
(ignore `.next/`, browser reports and auth state; decide explicitly whether
first-release docs should remain local/ignored).

Tests: `test_required_prerequisite_missing_is_failure`,
`test_optional_developer_prerequisite_skips`,
`test_required_skip_or_xfail_fails_gate`, `test_zero_collection_fails_gate`,
`test_missing_report_fails_gate`, `test_gate_uses_frozen_local_tools`,
`test_interrupt_cleans_only_owned_resources`,
`test_busy_port_never_reuses_foreign_server`,
`test_cleanup_failure_prevents_pass`,
`test_current_test_subcommand_still_invokes_pytest`,
`test_bun_test_does_not_collect_playwright_specs`.
Run `verify unit` and infrastructure fixture tests under `verify postgres`.

### Phase 1 — fresh schema, auth and ownership

Create `tests/support/auth.py`, `tests/platform_api/conftest.py`,
`tests/platform_api/test_schema.py`, `tests/platform_api/test_auth.py`,
`tests/platform_api/test_ownership.py`.
Production counterparts consumed by these tests: the `platform_api/` app
factory/settings/initial-schema and auth boundary named in its contract;
the verification plan does not create a second app factory implementation.

Tests: `test_initial_schema_on_empty_postgres`,
`test_initial_schema_repeat_is_noop`,
`test_concurrent_initializers_produce_one_schema`,
`test_incompatible_existing_schema_fails`,
`test_platform_outage_never_falls_back_to_sqlite`,
`test_platform_initialization_does_not_read_legacy_bot_db`,
`test_login_logout_expiration_contract`,
`test_unauthenticated_protected_routes_rejected`,
`test_user_cannot_invoke_admin_routes`,
`test_cross_user_resource_matrix`, `test_browser_origin_and_csrf_contract`.
The resource matrix covers list/read/update/delete/publish/session/history/
wallet/QA using user B's IDs from user A and checks both response and unchanged
database state; exact HTTP status/body follows the auth contract.
Run `verify postgres`; no SQLite substitution or skipped auth mechanism.

### Phases 2–4 — pure flow, draft publication and integrated Chat Test

Create `tests/fixtures/flow_cases.py`, `tests/support/configs.py`,
`tests/flow/test_validation.py`, `tests/flow/test_compilation.py`,
`tests/flow/test_runtime.py`, `tests/flow/test_purity.py`,
`tests/platform_api/test_config_pinning.py`,
`tests/platform_api/test_chat_voice_parity.py`,
`tests/platform_api/test_generation.py`,
`web/e2e/fixtures/auth.ts`, `web/e2e/fixtures/peer.ts`,
`web/e2e/agents.spec.ts`, `web/e2e/flow-editor.spec.ts`,
`web/e2e/integrated-test.spec.ts`.
Keep `tests/test_modularity.py` unchanged. Place the independent flow-purity assertions in `tests/flow/test_purity.py`; never weaken existing pipeline/backend assertions.

Phase 2 owns pure validation/compiler/controller fixtures and the adapter
qualification spike: `test_adapter_stream_does_not_speak_unvalidated_state`
and `test_adapter_cancel_invalidates_late_delta`, first with scripted fakes,
then actual configured model/transport evidence before adopting its protocol.
Phase 3 owns draft/template ownership, immutable revisions and publication:
`test_template_copy_is_owned_and_independent`,
`test_published_revision_cannot_be_modified_in_place`.
Phase 4 owns generation and customer UI/Chat Test:
`test_generation_result_validates_before_draft_save`,
`test_generation_cancellation_and_budget_bound_model_work`,
`test_chat_does_not_allocate_audio_capacity`.

Shared tests: `test_invalid_graph_cannot_publish`,
`test_compile_is_deterministic_for_identical_source`,
`test_runtime_transitions_match_contract_cases`,
`test_cancelled_generation_cannot_mutate_flow_state`,
`test_playback_ack_is_bound_to_current_transport_generation`,
`test_flow_import_has_no_database_network_or_backend_dependency`,
`test_edit_publish_race_keeps_session_snapshot`,
`test_chat_and_voice_consume_identical_pinned_artifact` (complete its voice
side in Phase 6).
Browser specs assert save/reload, invalid graph presentation, publish errors,
selected pinned draft in integrated chat, and cancellation/resource cleanup;
complete actual integrated voice and permission handling in Phase 6. Use DOM
assertions plus real control-plane persistence.
Add no arbitrary live latency threshold to deterministic fakes.
Run `verify unit`, `verify postgres`, `verify browser`; actual latency is
measured in Phase 12 with identical model/corpus/deployment conditions.

### Phase 5 — small prepaid reservations and ledger correctness

Create `tests/support/billing.py`,
`tests/platform_api/test_billing_amounts.py`,
`tests/platform_api/test_billing_reservations.py`,
`tests/platform_api/test_billing_idempotency.py`,
`tests/platform_api/test_billing_recovery.py`,
`tests/platform_api/test_billing_sequences.py`,
`tests/platform_api/test_billing_meter_api.py`.
Tests consume `platform_api/billing/` contract functions and its DB schema.

Tests/assertions:

- `test_money_rejects_fractional_or_wrong_currency_input`: integer paise
  through every request, DB column, ledger entry and response; no float
  conversion. `test_retail_rounding_matches_policy` uses the specified
  duration/rate table, including zero and sub-minute cases.
- `test_two_sessions_compete_for_last_reservation`: separate PostgreSQL
  connections start at a barrier; both request amounts whose sum exceeds
  spendable credit. Committed outcomes never authorize that sum; inspect
  committed ledger/wallet after each operation. This must use real wallet
  `SELECT ... FOR UPDATE`, not an in-memory lock or SQLite.
- `test_topup_races_reservation_without_lost_update`,
  `test_renewal_cannot_spend_unconfirmed_credit`,
  `test_settlement_cannot_charge_beyond_authorized_reservation`,
  `test_unused_reservation_release_is_exact`: verify posted/held/spendable
  identities and non-negativity according to the billing schema.
- `test_duplicate_grant_returns_one_operation_and_one_hold` (grant means
  this session's reservation), `test_duplicate_settlement_posts_once`,
  `test_duplicate_credit_posts_once`,
  `test_same_key_different_payload_rejected`: sequential and simultaneous
  retries preserve one durable operation outcome and ledger effect.
- `test_lost_response_after_commit_retries_original_result`: inject failure
  after DB commit but before HTTP response; reconnect/retry the same key,
  assert the same operation ID and one balance change.
- `test_meter_ack_requires_durable_database_commit`,
  `test_duplicate_meter_update_has_one_usage_effect`,
  `test_out_of_order_meter_update_obeys_contract`,
  `test_meter_outage_blocks_unconfirmed_chargeable_work`: synchronous meter
  API uses durable PostgreSQL state and the reservation contract; no broker
  acknowledgement or in-memory counter can stand in for committed usage.
- `test_crash_before_commit_leaves_no_partial_hold`,
  `test_crash_after_usage_persist_recovers_one_settlement`,
  `test_crash_after_settlement_commit_does_not_double_charge`,
  `test_unknown_usage_is_not_guessed_or_automatically_refunded`: restart a
  separate app/worker process on the same fresh DB and verify recovery under
  the billing contract. Killing only an asyncio task is not crash coverage.
- `test_seeded_financial_sequences_preserve_committed_invariants`: generate
  credit/reserve/renew/settle/release/retry/failure sequences with a recorded
  seed, integer amounts and fake clock; assert ledger reconciliation and
  authorized-total bounds after every committed transition. Include multiple
  sessions for one owner and an isolated second owner.

Run pure arithmetic cases in `verify unit`, all transactional cases in
`verify postgres`; browser balance/insufficient-credit disclosure in
`verify browser`. No real gateway calls. Real top-ups remain disabled until
the selected gateway's separate verified webhook/reconciliation suite passes.

### Phases 6–9 — authenticated voice, public preconditions, events and UI

Create `tests/platform_api/test_admission.py`,
`tests/platform_api/test_join_security.py`,
`tests/platform_api/test_event_persistence.py`,
`tests/platform_api/test_recovery.py`,
`tests/platform_api/test_admin.py`,
`tests/support/broker.py`, `tests/compose.rabbitmq.yml`,
`web/e2e/authenticated-voice.spec.ts`,
`web/e2e/public-call.spec.ts`, `web/e2e/history.spec.ts`,
`web/e2e/admin.spec.ts`.

Phase 6 owns authenticated voice admission, config pinning, cancellation and
observer coverage; Phase 7 owns guest/public security prerequisites with
public launch still disabled; Phase 8 owns the actual durable event spool,
RabbitMQ publisher/consumer and history ingestion; Phase 9 owns history,
billing and admin UI. Synthetic-credit tests are permitted in nonproduction;
public and paid release flags stay off until Phase 12.

Tests: `test_admission_reserves_capacity_and_funds_before_chargeable_work`,
`test_rejected_admission_releases_capacity`,
`test_duplicate_join_obeys_single_use_contract`,
`test_other_user_cannot_join_or_read_session`,
`test_disconnect_cleanup_releases_execution_slot`,
`test_events_replay_without_duplicate_transcript_or_charge`,
`test_local_spool_survives_producer_restart`,
`test_unroutable_publish_is_not_acknowledged_durable`,
`test_consumer_redelivery_deduplicates_history`,
`test_broker_outage_does_not_override_synchronous_billing_authority`,
`test_unacknowledged_persistence_is_not_reported_durable`,
`test_restart_reconciles_open_session_with_wallet_evidence`,
`test_admin_mutation_is_authorized_and_audited`,
`test_normal_user_never_receives_provider_secret`.
The Phase 8 `broker` fixture provisions a unique disposable RabbitMQ instance
with explicit memory limit, dynamic loopback binding and recorded image
digest under the same ownership/cleanup rules as PostgreSQL. Pin the selected
RabbitMQ image in that phase against its deployment image; this plan adds no
broker package dependency. Kill/restore the actual broker for outage tests,
inspect durable spool files on disk across producer restarts, and verify
publisher confirms plus mandatory routing outcomes and consumer redelivery.
Broker history delivery never authorizes credit or acknowledges a billing
meter update: those remain synchronous PostgreSQL/API operations from Phase 5.
Run `verify postgres`/`verify browser`; qualify public TLS/TURN in Phase 12.

### Phase 10 — Flow QA is a required release capability

Create `tests/flow/test_scenario_derivation.py`,
`tests/platform_api/test_flow_qa.py`, `web/e2e/flow-qa.spec.ts`.
Tests: `test_branch_exit_cases_derived_deterministically`,
`test_qa_uses_selected_immutable_flow_version`,
`test_qa_budget_and_concurrency_are_enforced`,
`test_qa_suggestion_cannot_mutate_published_flow_without_user_action`,
`test_other_user_cannot_read_qa_results`,
`test_cancelled_qa_job_stops_fake_models_and_records_terminal_result`.
Unit/DB/browser lanes use deterministic generated-model output; actual-model
QA compatibility receives separate model/config-tagged qualification evidence.
Run `verify unit`, `verify postgres`, `verify browser`.

### Phases 11–12 — actual audio, deployment faults and release evidence

Create `tests/support/audio.py`, `tests/fixtures/audio_manifest.json`,
`tests/qualification/test_voice_audio_input.py`,
`tests/qualification/test_flow_adapter.py`,
`tests/qualification/test_public_transport.py`,
`tests/qualification/test_release_evidence.py`,
`tests/qualification/test_backup_restore.py`.
Modify `README.md` only when measurements have actually been rerun and new
conditions/numbers can be cited. Modify existing observability contract tests
only when instruments are added, preserving the declared-name invariant.

Tests: `test_english_speech_pcm_reaches_stt_and_completes_turn`,
`test_hindi_speech_pcm_reaches_stt_and_completes_turn`,
`test_silent_input_does_not_fabricate_user_turn`,
`test_audio_qualification_rejects_send_text_fallback`,
`test_barge_in_rejects_cancelled_generation_audio_and_state`,
`test_configured_turn_budget_at_measured_concurrency`,
`test_actual_model_flow_delta_and_safe_streaming_contract`,
`test_actual_model_qa_scenarios_validate_against_pinned_flow`,
`test_local_offline_has_no_external_runtime_egress`,
`test_public_tls_and_relay_path_when_required`,
`test_network_loss_stops_chargeable_interval_per_contract`,
`test_backup_restores_fresh_database_and_reconciles_ledger`,
`test_release_requires_all_phase_artifacts_same_commit`.

Audio assertions require non-silent outbound **speech** PCM, STT input receipt,
normalized recognized text measured against the fixture reference under the
profile's corpus-specific acceptance policy, valid pinned-flow progress,
assistant output audio, and clean teardown. Record duration, sample format,
backend versions and fixture hashes. The existing English/Hindi text smoke
remains separate. Do not infer STT accuracy from script detection in an LLM
reply. Do not compare fake model timing to a real model baseline.

Execute actual audio tests on the compatible local machine and the nominated
deployment; run public browser/relay scenarios through the actual ingress.
Re-run `memory`, `bench`, `eval` when their covered models/prompts/chunking or
latency paths change, plus paired turn measurements for shared-flow overhead.
Use the same models, voices, corpus, hardware and concurrency for before/after
comparisons, and derive configured limits from recorded results. A missing
baseline/budget blocks qualification rather than inventing a universal SLO.

## 6. Required-test policy: executable examples

Create the following behavior in `tests/conftest.py`/`scripts/verify.py`, with
pure policy helpers in `tests/support/qualification.py`. Examples are
implementation targets, not current code. Policy is deliberately independent
of model/gateway credentials.

```python
# tests/support/qualification.py
from dataclasses import dataclass

@dataclass(frozen=True)
class GateCounts:
    collected: int
    passed: int
    failed: int
    errors: int
    skipped: int
    xfailed: int
    xpassed: int

def require_gate(counts: GateCounts) -> None:
    if counts.collected == 0:
        raise ValueError("required gate collected zero tests")
    if counts.passed == 0:
        raise ValueError("required gate passed zero tests")
    if any((counts.failed, counts.errors, counts.skipped,
            counts.xfailed, counts.xpassed)):
        raise ValueError("required gate contains non-pass outcomes")

def prerequisite_action(*, available: bool, required: bool) -> str:
    if available:
        return "run"
    if required:
        raise ValueError("required qualification prerequisite unavailable")
    return "skip"
```

```python
# tests/test_verification_policy.py
import pytest
from tests.support.qualification import (
    GateCounts, prerequisite_action, require_gate,
)

def test_optional_developer_prerequisite_skips():
    assert prerequisite_action(available=False, required=False) == "skip"

def test_required_prerequisite_missing_is_failure():
    with pytest.raises(ValueError, match="prerequisite unavailable"):
        prerequisite_action(available=False, required=True)

@pytest.mark.parametrize("field", ["skipped", "xfailed", "xpassed"])
def test_required_skip_or_xfail_fails_gate(field):
    values = dict(collected=2, passed=1, failed=0, errors=0,
                  skipped=0, xfailed=0, xpassed=0)
    values[field] = 1
    with pytest.raises(ValueError, match="non-pass"):
        require_gate(GateCounts(**values))

def test_zero_collection_fails_gate():
    with pytest.raises(ValueError, match="zero tests"):
        require_gate(GateCounts(0, 0, 0, 0, 0, 0, 0))

def test_all_passed_gate_is_accepted():
    require_gate(GateCounts(3, 3, 0, 0, 0, 0, 0))
```

`tests/__init__.py` makes `tests.support` resolve to this repository rather
than an unrelated installed `tests` package. Register `postgres`, `browser`,
`live_voice`, `qualification`, `broker` markers. Missing DB/audio/model prerequisites
in raw pytest can use `pytest.skip` only when `VERIFY_REQUIRED != "1"`;
required mode uses `pytest.fail`/collection error. Before collecting existing
`skipif` live tests, the verifier sets the opt-in flags and validates inputs.
Additionally enforce counts after execution, so a retained/decorator-based
skip cannot become a false green. Count collection skips/errors as well as
call/setup outcomes, preserve subprocess nonzero exit codes, and fail missing
or malformed report files. Xfail/XPASS are both forbidden in required gates.

For Playwright reject `skipped`, `interrupted`, unexpected outcomes, zero
tests and missing suites/reports. Use zero retries so flakes remain visible.
For Bun check successful exit, nonzero test count and zero skipped/todo
outcomes using its captured runner report/output; missing/unparseable counts
fail closed. Assert the parser against recorded runner-output fixtures for
the pinned Bun version before relying on it.

## 7. CI, release decisions and evidence

`verify.yml` is a new pull-request workflow on a compatible macOS runner
because the existing runtime dependency tree imports Apple MLX. Explicitly
prepare Python 3.12.13, uv 0.11.25, Bun 1.3.14 and Node 24.15.0, install frozen
dependencies, then run `verify unit`. Run PostgreSQL/browser gates on a runner
with Docker and the same frozen environment; GitHub-hosted macOS does not
provide the project's Docker Desktop by assumption. Configure the nominated
`voice-ai-qualification` self-hosted runner, or a separately prepared compatible
runner with Docker, for these gates. Do not claim a hosted Linux job runs the
current Apple-specific full dependency graph unchanged.

`qualify.yml` is an explicit dispatch/release-candidate workflow on the
nominated compatible runner. It consumes a reviewed qualification profile,
pre-provisioned audio/model/browser artifacts and ephemeral test credentials,
and runs `verify release`. It never funds a production wallet or enables real
payments. Required branch/release checks must include both deterministic
verification and the qualification result for the release commit; running
only the PR unit lane is insufficient for shipping.

Every gate attaches redacted reports with actual executed commands and
collected/pass/fail/skip counts, initial-schema identity, runtime and lock
hashes, DB version/image digest, test seed, config/model/corpus hashes and
cleanup outcome. Financial evidence includes starting/ending posted/held
amounts and ledger reconciliation, without tokens or personal transcripts.
Voice evidence includes the input path (`speech_pcm` versus `send-text`),
STT received samples, transport class, measured concurrency and conditions.
Never persist auth cookie jars, TURN passwords, provider keys or raw private
recordings in CI attachments.

The release gate fails for missing required phase evidence, any required
skip/xfail, missing audio-input proof, cross-user access, duplicate financial
effect, over-authorization, unreconciled accounting reported as settled,
false durability, failed backup restore, or a configured deployment budget
breach. A deliberately unsupported optional browser/provider is recorded as
not supported in the product matrix; relabeling a required capability as
optional after a failed run cannot satisfy the first-release gate.
