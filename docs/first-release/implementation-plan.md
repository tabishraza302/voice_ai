# Voice AI First Release Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` to implement this plan task by task. Steps below track future code work; this planning pass does not claim that application modules or tests have been implemented.

**Goal:** Build the first multiuser Voice AI release from the existing local agent, with basic prepaid accounting and no legacy-data migration.

**Architecture:** Retain `web/` and `bot/`; add a separate `platform_api/` whose metadata lives in a dedicated `platform` schema of the PostgreSQL database selected by the shared `DATABASE_URL` (the agent keeps `public`; Phoenix keeps `phoenix`). Chat and voice share a pure `bot/flow/` engine; one bounded runtime serves immutable session snapshots. Wallet transactions are synchronous and use per-call reservations; RabbitMQ is for durable history, not financial authorization.

**Tech stack:** Exact selected versions in master III.2; preserve existing audio pins, pytest/asyncio and Bun; add only the named auth, AMQP, JWT and browser-test dependencies at their owned task. No installation is claimed by this plan.

**Spec:** [Master](../../VOICE_AI_MASTER_IMPLEMENTATION_SPEC.md), [auth](auth-contract.md), [flow](flow-contract.md), [accounting](accounting-contract.md), [verification](verification-plan.md).

## Global constraints

- One PostgreSQL runtime database selected by the explicitly exported shared `DATABASE_URL`, with separate schemas: `public` for the local agent, `platform` (via `PLATFORM_POSTGRES_SCHEMA`) for the control plane, and `phoenix` for telemetry. Platform commands/startup must not silently load `.env`, invent a URL or fall back to SQLite. The Phoenix host launcher derives its URL from `DATABASE_URL`. No `PLATFORM_DATABASE_URL`, no legacy backfill, local-table imports, Alembic work or implicit migration.
- All new application paths below are Create unless explicitly Modify. No source files or records are deleted; no `web/`/`bot/` move.
- Existing local launcher/client, swappable registries, router protocols, metric names and bounded labels remain valid. SaaS startup does not expose local management routes.
- All platform feature flags default false; tests/nonproduction may enable a named feature with synthetic credits. Public/real-paid flags remain off until Task 12 passes.
- Exact observable types/rules live in their linked contracts. Private helper organization is implementation freedom, not an unresolved product decision.
- Every model/prompt/latency-path code change reruns relevant measurement scripts and records conditions; a selected default or fixture is not a benchmark.
- `docs/` is currently gitignored. When saving implementation artifacts to Git, explicitly include these reviewed contract files or change only the intended ignore exception; generated data/audio/secrets stay ignored. Do not assume a green Git status backed up this package.

## Review focus

1. A valid user substitutes another user's ID: every owner route returns 404 without data or mutation (Tasks 1, 3, 7, 9).
2. A funding response is lost after commit: the identical operation returns the original outcome without another charge/reservation (Task 5).
3. A draft/policy/global-STT override changes during admission: the running call still executes its recorded immutable snapshot or fails closed (Tasks 3, 6).
4. Barge-in occurs between state/audio events: retain committed facts, reject stale generation events, and never count queued text as completed speech (Tasks 2, 6).
5. A required test dependency is absent or the audio test injects text: release qualification fails rather than silently skipping or mislabeling evidence (Tasks 0, 11, 12).

## Shared interface decisions

- `platform_api.config.PlatformSettings`: namespaced settings, validated environment, origins, flags, DB/service URLs and secret references; never inherits the local `.env` implicitly in tests.
- `platform_api.app.create_app(settings: PlatformSettings | None = None) -> FastAPI`: no GPU/provider startup during import; lifespan owns API DB pools and task cleanup.
- `platform_api.db.schema.initialize_schema(engine: AsyncEngine, *, expected_version: str, metadata_hash: str) -> None`: creates or verifies only the dedicated `platform` schema; identical schema is no-op, unknown nonempty/mismatch raises `SchemaMismatch`; never touches `public`/`phoenix`, no destructive reset, no implicit migration.
- `platform_api.auth.types.Principal` and `require_user/require_admin/require_step_up/require_csrf`: exact signatures and models in auth contract; all downstream queries use `principal.user_id`.
- `bot.flow.schemas.FlowLimits`, `FlowDiagnostic`, `SessionState`, `StateProposal`, normalized adapter events and compiled artifacts: fields/discriminators in flow contract and schemas. The public pure operations are `validate_authoring(source, limits) -> list[FlowDiagnostic]`, `normalize_executable(source, limits) -> dict`, `validate_executable(flow, limits, capabilities) -> list[FlowDiagnostic]`, `compile_flow(flow, policy, limits, capabilities) -> CompiledFlow`, and `reduce_event(compiled: CompiledFlow, state: SessionState, event: FlowEvent) -> Reduction`. `Reduction={state,commands,diagnostics}`. No reducer I/O.
- Platform publication wraps existing `bot.agents.compiler.compile_system_prompt`; `bot.flow` does not import `bot.agents`, SQLAlchemy or backend services. Define `CompiledFlow={normalized,manifest,flow_hash,artifact_hash}`; the manifest fields are exactly the flow contract's compiler identity. `FlowEvent` is the union of accepted user/locale/model/playback/chat/action/lifecycle events declared there.
- Wallet service signatures, request/response models and exact arithmetic are in accounting contract; `AccountingSnapshot` in tests contains posted/reserved/entries/budgets/checkpoints for one committed transaction boundary.
- `RegisteredSession`, `RuntimeReservation` and `JoinDetails` are strict schemas in `platform_api/sessions/schemas.py`. JoinDetails is `{session_id,runner_id,config_version_id,signaling_url,join_token,expires_at}`. RuntimeReservation is `{session_id,runner_id,registration_id,expires_at}`. RegisteredSession adds immutable config/tariff/participant references and execution state/version.
- Event transport envelope is `{event_id,session_id,sequence,schema_version,event_type,emitted_at,payload}`. Schemas/maximum payloads are owned in `platform_api/events/schemas.py`; fingerprints use the same strict canonical JSON/hash rules. Idempotent event identity never changes on retry.

## Task 0: Fresh control plane and executable test foundations

**Files:** Create `platform_api/{__init__,app,config,health,cli}.py`, `platform_api/db/{__init__,base,session,schema}.py`, `platform_api/models/__init__.py`, `scripts/verify.py` and verification-plan Phase 0 fixtures/Compose/CI. Create core identity/wallet model declarations needed for zero-credit account initialization; controllers remain in later tasks. Modify selected pins/locks, `.env.example`, shell wrapper, web dev test tools and only intended ignore entries. Delete none.

**Consumes:** existing runtime/dependency inventory and master III.2–3. **Produces:** app factory, independent Base, validated settings, explicit init command, get_db dependency and `verify unit/postgres` harness. Test setup uses this same initializer.

- [ ] Write `tests/platform_api/test_auth_schema.py` for empty platform-schema init, repeated no-op, schema mismatch, independent metadata and no local fallback. Write `tests/test_verification_policy.py` and lifecycle cases from verification plan.
- [ ] Write this import-boundary assertion in `tests/platform_api/test_import_boundary.py`, then run it and observe failure while the new package is absent:

```python
def test_platform_import_does_not_load_models():
    import subprocess, sys
    check = subprocess.run([sys.executable, '-c',
        "import platform_api.app; import sys; assert 'mlx.core' not in sys.modules; assert 'bot.db.session' not in sys.modules"],
        capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
```

- [ ] Implement app/settings/DB lifecycle and initial `platform`-schema creation with the exact schema-only refusal behavior. `init-db` imports platform tables, not current local models; startup only checks readiness and never runs create-all or an implicit migration. Add fake-safe model catalog loading from a generated qualification/catalog JSON, never by importing GPU services into API startup.
- [ ] Implement the verifier command parser, disposable process/DB ownership and mandatory skip policy using the verification plan's code examples. Preserve the old `test` dispatch and Bun collection.
- [ ] Run focused tests and `verify unit/postgres`; attach manifest/logs. Check host ports and configured container memory. Commit this bounded slice with its contract and lock changes after tests pass; no production schema modification.

## Task 1: User/admin authentication and accounts

**Files:** Auth contract's exact Create/Modify map, including auth/account modules, CLI/audit, platform frontend client/types/guards/pages and owned tests. Tests live under `tests/platform_api/`; no local DB edits. **Consumes:** Task 0 settings/Base/get_db. **Produces:** verified Principal/guards, account initialization, session/cookie/CSRF, reset/mail, admin bootstrap/TOTP/step-up and auth UI.

- [ ] Follow the auth contract's five component tasks in order: schema; passwords/session/CSRF; signup/reset/delivery/profile; admin MFA/bootstrap/audit; frontend/mode isolation. Write each named negative/race test before code.
- [ ] Assert route enumeration: only the explicit auth bootstrap/public/health allowlist can lack a full owner/admin guard; restricted MFA sessions access no resources. User A cannot name User B as an owner. Run reset consumption and TOTP replay on independent DB connections.
- [ ] Implement using opaque sessions and scrypt policy exactly as written, bounded hashing off the event loop, no DNS deliverability lookup or external identity SDK. Make durable delivery participate in token creation and inject the selected file/SMTP adapter.
- [ ] Run `verify unit`, `verify postgres` and auth browser journeys; preserve offline imports/navigation. Confirm expired sessions, forbidden origins, CSRF failure and server-side suspended accounts fail closed. Commit the reviewed auth slice.

## Task 2: Pure graph/controller/compiler and adapter qualification spike

**Files:** Flow contract's `bot/flow/` package; `tests/flow/{test_validation,test_compilation,test_runtime,test_purity,test_adapter_contract}.py`, `tests/qualification/test_flow_adapter.py`, fixtures and `bot/adapter_qualification.py`. Modify existing compiler only at its wrapper seam; preserve local behavior/modularity tests.

**Consumes:** selected JSON schemas/fixtures/FlowLimits and pure policy text. **Produces:** the five pure functions/types above, normalized hashes, validated reducer/events and capability report format. No auth or DB inside the reducer.

- [ ] Copy the checked schema/graph/hash/trace fixtures into the test fixture directory; assert authoring versus executable structure results, declared semantic error cases and exact canonical vectors. Add clean-import purity tests.
- [ ] Write first-match/unknown/precondition, early three-field capture, correction, refusal, explicit-confirmation and interruption boundary cases from the flow contract before reducer implementation. The tests assert state/commands, not private collection layout.
- [ ] Implement restricted predicate validation, acyclic authoring rules, finite clarification and pure event reduction. Render controller-owned informational fragments from validated state; never let LLM prose authorize a transition/tool/money operation.
- [ ] Implement qualification runner/spike against pinned actual model/transport with the contract's six scenario classes. A fake qualifies only reducer orchestration. Record unsupported tuples explicitly; no voice capability is enabled from TTSTextFrame alone.
- [ ] Run owned unit/fixtures and qualified adapter checks; retain exact output. If a tuple cannot meet required completion evidence, it remains unsupported and is excluded from advertised release configurations; do not invent a timer-based pass. Commit pure foundations and qualified glue separately.

## Task 3: Owner agents, templates, drafts and immutable publication

**Files:** `.env.example`, `bot/flow/qualification.py`, `platform_api/{app,catalog,config,health,qualification}.py`, `platform_api/agents/{__init__,config,models,schemas,validation,publishing,service,routes}.py`, `tests/flow/test_runtime.py`, `tests/platform_api/{test_auth_csrf,test_agents,test_agent_versions,test_publishing,test_config_pinning,test_capability_source}.py`, and `docs/first-release/flow-contract.md`. **Consumes:** Task 1 principal/audit and Task 2 compiler/validation. **Produces:** master III.5 agent APIs and immutable config references.

- [ ] Write two-owner CRUD/duplicate/restore isolation, structurally valid incomplete autosave, invalid test/publish, revision collision and lost-response publish tests. Publish requires `Idempotency-Key`; set `READ COMMITTED` before auth queries and serialize same-key requests in PostgreSQL so concurrent retries replay the committed outcome even when the database role defaults to stronger isolation, while key reuse with a different payload conflicts.
- [ ] Implement templates copied on creation, mutable draft CAS and immutable version transactions; policy/model/voice hashes are pinned, secrets excluded. Load the operator-pinned protocol-v2 capability report at app startup and clear it at shutdown; compare the exact model, STT, EN/HI voice, adapter, and deployment-revision tuple against a separately trusted deployment identity (`PLATFORM_DEPLOYMENT_ADAPTER_ID`, `PLATFORM_DEPLOYMENT_ADAPTER_VERSION`, `PLATFORM_DEPLOYMENT_MODEL_REVISION`, `PLATFORM_DEPLOYMENT_PROFILE_HASH`, and `PLATFORM_DEPLOYMENT_SOURCE_REVISION`), and block voice publication without an exact match. Use the configured catalog for validation/publish and fail closed on an invalid configured catalog. Enforce full-config and setting-value limits before hashing/persistence; cap all body-bearing agent requests before JSON parsing; bound warning acknowledgment count and string length; audit metadata updates with field names and create/duplicate audits with IDs only.
- [ ] Pause an admission/config read with a test barrier, publish/edit the second version through another connection, then verify the first immutable bytes/hash are retained. Test stale authoring hashes and warning acknowledgments.
- [ ] Run focused unit/PostgreSQL tests plus baseline tests and commit. No UI gate claims publication before this compiler exists.

## Task 4: Customer UI, generation jobs and integrated Chat Test

**Files:** `platform_api/testing/{__init__,chat}.py`, `platform_api/generation/{__init__,models,service,routes}.py`, platform frontend agent client/workspace/editor/test components and dashboard/agents pages; browser cases named in verification plan. Modify existing form/sidebar only behind explicit platform mode.

**Consumes:** Tasks 1–3 APIs/compiler. **Produces:** two creation paths, same-schema Step/Flow views and pinned Chat Test. Generation is a bounded proposal job with a selected configured generator model; it never auto-publishes or invents unknown business facts.

- [ ] Write API/browser cases for empty accounts, keyboard/touch creation, manual and generated drafts, incomplete saves, stale revisions and same-hash chat reset.
- [ ] Implement generation API/job persistence and quota/timeout cancellation, editor bindings and deterministic graph view. Preserve original local client and Playground navigation.
- [ ] Run the same controller traces through chat, prove no voice slot consumed and require durable assistant-message commit before delivered completion. Abort pending jobs on budget/timeout without mutating a published version.
- [ ] Run unit/PostgreSQL/browser gates, including cross-owner job IDs and draft edits during a test. Commit each independent UI/service slice with its tests.

## Task 5: Basic wallet/reservations and synchronous meter

**Files:** Accounting contract billing package; `tests/platform_api/test_billing_{amounts,reservations,idempotency,recovery,sequences,meter_api}.py`, `tests/support/billing.py`. **Consumes:** Task 1 owner/admin guards and session record primitives. **Produces:** `charge_minor`, credit/checkpoint/close/expiry services and exact wallet/budget APIs.

- [ ] Port oracle scenarios to service-boundary tests while keeping the Decimal/search reference independent from the integer production implementation. Test 3×10-second rounding, subsecond accumulation, mixed rates and zero/setup charge.
- [ ] Write PostgreSQL concurrency/commit-barrier tests for duplicate and conflicting replay, two calls spending the final credit, admin debit below reservation, lost-response retry and stopped/expired crash write-off.
- [ ] Implement pricing exactly:

```python
def charge_minor(rate_minor_per_minute: int, cumulative_billable_ms: int) -> int:
    if rate_minor_per_minute < 0 or cumulative_billable_ms < 0:
        raise ValueError('negative rate/time')
    return (rate_minor_per_minute * cumulative_billable_ms + 59_999) // 60_000
```

- [ ] Implement wallet-row then budget-row transactions, exact request-hash replay and durable checkpoint/ledger/response commit. Apply the contract's affordable-duration calculation and cumulative—not additional—permits; no local account coordinator.
- [ ] Run real PostgreSQL money tests, deterministic randomized traces and baseline regression. Leave real-paid/public flags false and commit. No broker is needed for financial proof.

## Task 6: Authorized runtime, voice testing and shared metrics

**Files:** `platform_api/sessions/{__init__,models,schemas,service,routes,admission}.py`, `bot/saas_runtime.py`, `bot/runtime/{__init__,snapshots,budget_client,session_task,transport_auth,pipeline_builder}.py`, `bot/flow_runtime.py`; existing runner/composition/observer/transcript/client seams and test files from verification plan.

**Consumes:** Tasks 1–5 principal/version/flow/budget APIs and a qualified adapter tuple. **Produces:** `reserve_session`, `register_runtime`, `issue_join`, session-bound runtime config and media orchestration. The legacy runner stays local; control API import never loads GPU services.
**Platform admission boundary:** [Task 6 session admission](session-admission.md) documents the owner/internal APIs, durable admission and teardown contracts, security bindings, and the local runtime prerequisite.


- [ ] Write registration/funding compensation and join-binding/JTI replay tests before routes. Enforce single assigned runner/peer and per-user versus runner capacity separately.
- [ ] Create `build_voice_pipeline(transport, settings, flow_hooks, instruments, runner_args)` at the new runtime seam using existing registries/context/language processing. Never import a concrete backend into `bot/pipeline.py`; retain the old local entrypoint and regression contracts.
- [ ] Implement immutable-only config load; failure before ACTIVE must not call mutable profile/default fallback or process-wide STT override. Meter ACTIVE time under confirmed permits; reconnect gaps are free; shutdown stops at the funded cap.
- [ ] Make model attribution session-correct against shared instruments and start/stop memory sampling at process lifespan. Test two different model sessions and end either one without losing samples for the other.
- [ ] Run auth/config/voice/cancellation tests and actual EN/HI audio qualification. After integrated Voice Test acceptance, redirect/remove customer Playground navigation while preserving local `/client`. Commit each separately verifiable runtime/metrics/client slice.

## Task 7: Guest deployment and prelaunch public security

**Files:** `platform_api/public/{__init__,models,service,routes}.py`, `platform_api/{app,config}.py`, guest route/transport and privacy-export tests, `platform_api/ops/privacy.py`, `web/src/app/public/[publicId]/page.tsx`, the hosted guest page/client and deployment fixtures. **Consumes:** registered funded sessions from Task 6 and owner/admin guards. **Produces:** server-resolved owner admission and session-only guest capability/reconnect/end/status.

The hosted link is `/public/{public_id}`. Guest lifecycle routes are exactly `GET /api/v1/guest/sessions/{session_id}/status`, `POST /api/v1/guest/sessions/{session_id}/reconnect-token`, and `POST /api/v1/guest/sessions/{session_id}/end`; no legacy public-session aliases remain. The guest cookie is scoped to `/api/v1/guest/sessions/{session_id}`.

The middleware rejects public guest POST bodies over 8 KiB before FastAPI parsing, including streamed/chunked bodies. Admission uses atomic PostgreSQL-backed limits per client IP (`PLATFORM_PUBLIC_ADMISSION_IP_LIMIT`) and per agent (`PLATFORM_PUBLIC_ADMISSION_AGENT_LIMIT`) within `PLATFORM_PUBLIC_ADMISSION_WINDOW_SECONDS`; the agent quota is shared by every public deployment link for that agent. `PLATFORM_PUBLIC_SIGNALING_URL` must be the exact HTTPS `/api/offer` endpoint. Staging/production enablement requires public TLS, TURN, Secure guest cookies, trusted ingress CIDRs, and HTTPS `PLATFORM_WEB_ORIGIN`; preserve the browser's exact origin through the TLS-terminating proxy to the loopback runner. The browser sends the session ID and bound join JWT in Pipecat `request_data`; it reports connected only after WebRTC reaches `connected`.

- [ ] Write cross-guest/owner-history, owner-wallet substitution, disabled deployment, expired capability, reconnect grace, stale JTI/peer, CSRF/origin and rate/zero-credit tests.
- [ ] Implement opaque deployment IDs, hashed capabilities, token/participant binding and admission disclosure; rate/resource/TLS/TURN/trusted-proxy protections precede public enablement.
- [ ] Verify permitted and denied paths in test/staging with synthetic credit. No guest permission changes owner routes. Global public/real-paid flags remain false through Task 11. Commit.

## Task 8: Durable event/history ingestion

**Files:** `bot/runtime/outbox.py`, `platform_api/events/{__init__,schemas,models,repository,consumer}.py`, spool/broker/consumer fixtures and tests; selected RabbitMQ Compose profile/config. **Consumes:** registered session/config IDs and shared event envelope. **Produces:** persistent capture, confirmed publication and idempotent ordered projections.

- [ ] Write restart/replay, conflicting payload, after-commit/before-ACK, delayed transcript, DB/broker outage and retry-destination-down cases. Test loss after original producer copy removal, not just duplicate insertion.
- [ ] Implement atomic spool sequence allocation, durable publishes/mandatory routing/confirm, transactional dedupe/effect and confirmed retry/quarantine transfer before source ACK. Consumer cannot mutate wallet tables.
- [ ] Run the history consumer with `PLATFORM_EVENT_CONSUMER_DATABASE_URL`, a distinct `NOINHERIT` login on the exact same PostgreSQL host, port, and database as `DATABASE_URL`; do not treat loopback aliases as the same endpoint. Missing, mismatched, or overprivileged credentials fail before broker setup. Grant `CONNECT`, `USAGE` on the effective schema, `SELECT, INSERT` on `processed_events`, `transcript_segments`, `transcript_gaps`, and `event_quarantine`, `INSERT` on `provider_usage`, and `SELECT, INSERT, UPDATE` on `transcript_state`. Require full table-level privileges only, with no column-only grants, `WITH GRANT OPTION`, wallet-table privileges, schema/database `CREATE`, elevated role attributes, or role memberships.
  If this PostgreSQL database grants `CREATE` on `public` to `PUBLIC`, revoke that grant in this database before starting the consumer.
  Compare the live server address, port, and postmaster start time from the platform and consumer connections before opening the broker. Reject PUBLIC table and column grants, including grants duplicating allowed projection privileges.
- [ ] Implement revision-bound transcript pagination and explicit gaps/processing. Apply storage ceilings/admission drain before disk exhaustion. Reclaim confirmed journal history with fsynced same-directory compaction and atomic replacement, preserving pending order and per-session sequence watermarks across restart. Run actual broker/Postgres fault gates and commit.
- [ ] Have the consumer CLI exit with status 0 on successful stop, including `--once` after a delivery, log the handled count separately, and return nonzero on failure.

## Task 9: Conversations, wallet and admin experience

**Files:** owner history/wallet APIs, `platform_api/admin/{__init__,routes,service}.py`, dashboard/conversation/billing/admin pages and browser tests. **Consumes:** existing guards, versions, sessions, budgets and history. **Produces:** actual—not fabricated—statuses, charges, metadata and audited operations.

- [ ] Test owner/admin/guest response fields and escalation failures for every new route. Admin transcript content requires explicit authorized support reason/audit, not an unrestricted detail dump.
- [ ] Implement cursor filters, pinned snapshots, estimated/final labels and permitted corrections/rates/templates/prompts/visibility; step-up, request idempotency and audit commit with each consequential change.
- [ ] Run full UI journeys and concurrent price/config changes proving old calls retain their snapshot. Commit without enabling public/real-paid flags.

## Task 10: Same-engine bounded Flow QA

**Files:** `platform_api/qa/{__init__,models,schemas,service,routes,worker}.py`, graph-scenario tests and QA UI. **Consumes:** immutable snapshots and shared validation/reducer/adapters. **Produces:** frozen scenario IDs/assertions, bounded simulations/results and draft-only suggestions.

- [ ] Write branch coverage, correction/refusal regression, invalid scenario, frozen rerun, quota/time/cancellation, expired-lease reclaim/stale-commit, prompt-plus-completion token boundary, over-limit reachable branches, owner authorization, UI and no-auto-publish assertions.
- [ ] Implement deterministic scenarios first, then capped model utterance expansion and optional evidence-bearing judge. Reuse the existing validator; do not postpone it to this phase.
- [ ] Recover expired PostgreSQL leases with rotated fencing tokens; check prompt and completion budgets before each model call, and reject branch catalogs that cannot fit without truncation.
- [ ] Build the owner QA panel on the scenario/job/result/cancel APIs. Suggestions must be draft-only and require explicit apply or dismiss; never auto-publish.
- [ ] Run same pinned engine/model tuple with bounded jobs that yield resources to voice. Actual selected-model QA is required before release; fake judge success is not enough. Commit.

## Task 11: Retention, backup/restore and measured operations

**Files:** retention/export/backup CLI jobs, redaction/ops tests, observability additions and qualification fixture metadata. **Consumes:** completed storage/domain contracts. **Produces:** auditable policies, restorable initial schema/data backup, owned runbooks and deployment-specific budgets.

- [ ] Write export/delete/retention tests preserving ledger/audit records, backup round-trip assertions and secret/transcript redaction cases.
- [ ] Implement bounded housekeeping and restore into a fresh owned DB. Do not introduce legacy conversion. Verify all container limits, host ports, network dependencies and alert owners.
- [ ] Run actual deployment model/corpus/concurrency latency/memory/quality measurements and save policy config plus evidence. Preserve local offline qualification separately; never copy this machine's numbers to another target. Commit evidence references, not ignored audio/data/secrets.

## Task 12: Whole-release acceptance

**Files:** `tests/qualification/` suites, release verifier/evidence policies and staging runbook from verification plan. **Consumes:** every earlier gate and deployment profile. **Produces:** one release manifest with all required gates passed or an explicit failure; no silent skip.

- [ ] Assert missing fixtures/models/browsers/DB/TURN/report and any required skip/xfail/zero collection fail the release gate. Check text-only or fake-media evidence cannot satisfy actual speech qualification.
- [ ] Run the full verification command from the verification contract, including unit/Postgres/browser/actual model+audio/FlowQA/fault/restore/privacy/ownership/financial gates. Exercise manual/generated draft -> publish -> chat -> voice -> guest -> low-wallet concurrency -> history -> admin correction.
- [ ] Review the generated manifest and advertised support matrix; only after every required pass can server public/real-paid flags be enabled. Gateway stays independently off until implemented/verified. Record failures honestly and repair their owned task before claiming launch readiness.

## Handoff

The user's scope decisions are incorporated; there is no outstanding request for the user to design migrations, accounting, file paths, auth, graph types, ordering or test infrastructure. The next implementation slice is Task 0, then Task 1. The plan is prospective: application implementation and deployment qualification are not performed or claimed by this document revision.
