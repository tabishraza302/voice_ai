# Task 6 platform session admission

This document describes the platform control-plane boundary and the local voice runner's Task 6 admission bridge. The bridge now binds an immutable runtime snapshot to Pipecat SmallWebRTC offers and blocks speech until admission and real media readiness. The runtime still fails closed unless durable PostgreSQL flow/stop storage, committed playback receipts, and the installed adapter qualification are provided; those production proofs are not fabricated here.

## Scope and storage

The platform creates only `voice_test` sessions. It uses the existing canonical `platform_api.models.session.Session` row and the same PostgreSQL database and configured platform schema as auth, agents, and billing. Runner capacity, registration, owner participant, join JTI, peer, and slot records are added in that schema; they are not a second session store. User, wallet, and agent-version data remain in place. Public calls and real-paid calls are rejected by admission configuration and remain disabled.

The first-release schema contract is fresh-schema-only. The initializer creates a new platform schema or verifies an identical schema and fails closed on a metadata mismatch. No migration, backfill, or Alembic path is included. Use a disposable fresh PostgreSQL database for tests; never reset or drop an existing user database, use SQLite, or modify the shared `public` schema.

## Owner API

The owner API is mounted under `/api/v1`:

| Method and path | Purpose |
| --- | --- |
| `POST /api/v1/agents/{agent_id}/test/voice-sessions` | Reserve a voice-test session for a caller-owned enabled immutable `agent_version_id`. |
| `GET /api/v1/sessions/{session_id}` | Read the safe session state for its owner only. |
| `POST /api/v1/sessions/{session_id}/join` | Issue/reissue the owner-bound, short-lived join credential for the existing live session. |
| `POST /api/v1/sessions/{session_id}/end` | Request idempotent owner teardown. |

Create, join, and end require an authenticated owner, the configured exact `Origin`, CSRF protection, and an `Idempotency-Key`. They use the existing auth cookie and CSRF contract. The body contains only the immutable version ID; it cannot choose a runner, URL, price, capacity, model, or provider. Create is feature-gated by `PLATFORM_ENABLE_VOICE_TEST`, and does not enable public or real-paid calls. Session responses are private/no-store and expose no other owner's session data.

The owner must have a funded wallet for a first permit. The selected published version must have at least one voice and match the trusted, pinned adapter capability and qualification settings for both EN and HI fragment playback, metadata, gating, and cancellation. Missing, stale, or mismatched qualification fails closed; mutable draft/profile/default configuration is not consulted to construct a runtime snapshot.

## Admission and compensation

Admission uses the configured local runner only. Before reserving capacity, both owner and guest admission paths check persisted global, owner and selected-agent pause policies under shared PostgreSQL transaction-level advisory locks. Policy writes take matching exclusive locks, including when no row exists yet, so an admission cannot pass concurrently with a pause update. The database transaction also locks the owner and runner capacity records, checks the distinct owner and runner limits, and durably creates the canonical session, execution slot, pending registration, owner participant, and idempotency record with stable session and registration IDs. The HTTP call to the runner occurs only after that transaction commits.

The platform then persists the runner's bound registration acknowledgement, creates the call budget using the session's stored rate, and reserves the first permit using its stored rate, slice, and TTL. A retry after a crash between funding and finalization uses that same immutable tariff snapshot, never the current tariff. Only after a live permit is confirmed does it move the session to `REGISTERED`, persist the single-use join JTI and its complete binding, and return the signed token. An unresolved bridge or database outcome is retried against the same durable session, registration, and idempotency IDs; it is not converted to success. A definitive registration rejection, setup timeout, or funding denial follows compensation. If a runner reservation may exist, execution capacity is not released until the runner confirms release. Any open budget is closed with the matching stop evidence; a terminal failed session is written only after compensation completes. An unresolved compensation remains retryable and continues to hold capacity.

Owner capacity and runner capacity are independently configured and enforced. A confirmed runtime release frees execution capacity, but does not by itself close billing. The registration remains `REGISTERED` while the runtime provides the final stop attestation, so `close_budget` can authenticate the stop against the assigned runner and exact session version.

## Join and runtime lifecycle

Join tokens are EdDSA-signed and short lived. Each JTI is stored as a single-use row; the token itself is not stored. The token and database record bind the owner auth session, participant, session and exact session version, immutable config version, assigned runner, registration, configured signaling URL, exact web origin, key fingerprint, issuer, audience, scope, and expiry. Runtime consumption verifies the signed token and every stored binding before atomically consuming the JTI and assigning the sole peer. A wrong origin, runner, session, configuration, registration, or peer replay is rejected. A released peer may reconnect only within the stored grace window; reconnect gaps are not billed.

The runtime may advance the session with version-checked state transitions. Entering `ACTIVE` requires an open budget and unexpired funded permit. For teardown, the platform first moves the session to `ENDING`, sends a version-bound local release command, and marks the execution slot released only after an explicit acknowledgement. The runtime must stop media before it submits a `voice-runtime` stop attestation for that same `ENDING` version. The platform closes the budget from cumulative proven usage; only then may a version-checked terminal transition set the registration to `RELEASED` and the canonical session to `COMPLETED`, `FAILED`, or `EXPIRED`. If budget closure or terminal persistence is retried, the committed stop and billing facts remain durable.

A terminal flow teardown is not a peer disconnect: after the media connection is confirmed stopped, the runner enters this same `ENDING` path and waits for the version-bound release, durable playback stop record, and budget closure before transitioning to `COMPLETED`; a flow failure uses `FAILED` with `FLOW_FAILED`. A nonterminal transport disconnect continues to use peer release and may return to `ACTIVE` within the reconnect window.

Admin termination uses this same idempotent `ENDING` and version-bound runner-release path. The state change, idempotency result, and audit record commit together before the release command is sent; the admin operation does not directly close billing or release the budget hold.

The internal API is mounted under `/internal/sessions` and is authenticated with the configured runtime bearer token and `X-Runner-ID`. It exposes:

- `GET /internal/sessions/{session_id}/runtime-config`
- `POST /internal/sessions/{session_id}/join/consume`
- `POST /internal/sessions/{session_id}/peer/release`
- `POST /internal/sessions/{session_id}/state`

The runtime uses the same bearer and runner identity for synchronous budget metering at `POST /internal/sessions/{session_id}/budget/checkpoint` and `POST /internal/sessions/{session_id}/budget/close`. The first permit is reserved by the platform before join credentials are issued; runtime checkpoints extend only cumulative, verified usage. Budget close requires the final cumulative usage and, while a permit remains live, a stop attestation bound to the `ENDING` session version.

Runtime configuration is built only from the immutable version snapshot and current budget facts. `immutable_config.policy` carries the exact `prompt_text` alongside its pinned version ID, version, slug, and hash. Publication and runtime loading recompute `policy_hash = SHA256(b'voice-ai/policy/v1\0' + prompt_text.encode('utf-8'))` and verify the text against the pinned policy row. Runtime never falls back to mutable agent state or shared global provider selection.

## Local runner bridge contract

The platform's authenticated, outbound bridge sends a registration command to `POST {PLATFORM_SESSION_RUNNER_URL}/internal/runtime/sessions/{session_id}/register` and an idempotent release command to `POST {PLATFORM_SESSION_RUNNER_URL}/internal/runtime/sessions/{session_id}/release`. Requests carry `Authorization: Bearer <PLATFORM_RUNTIME_SERVICE_TOKEN>`, `X-Runner-ID`, and JSON. The bridge disables ambient proxy configuration and redirects, uses a bounded timeout, and accepts only the exact bound response shape.

Registration command fields are `session_id`, `runner_id`, `registration_id`, `config_version_id`, `session_version`, `connect_deadline`, and `hard_end_deadline`. The runner must return `session_id`, `runner_id`, the same `registration_id`, and an `expires_at` no later than `connect_deadline`. The runner must make registration idempotent by the same IDs. A timeout, malformed acknowledgement, or other uncertain response is unresolved and must be retried without allocating a second session.

Release command fields are `session_id`, `runner_id`, `registration_id`, and the exact `session_version`. Only an HTTP 200 response with matching session, runner, registration, and version plus `released: true` frees platform execution capacity. An absent, malformed, stale, or ambiguous acknowledgement keeps the slot reserved for retry.

The local runner mounts authenticated registration and release routes at `/internal/runtime/sessions/{session_id}/register` and `/release`. Browser admission is bound to the real Pipecat SmallWebRTC `POST /api/offer` contract: `request_data` must contain exactly `session_id` and `join_token`, and the exact configured `PLATFORM_WEB_ORIGIN` must be present. The returned `pc_id` is bound to that session and origin before the answer is returned; later `PATCH /api/offer` ICE candidates require the same origin and a live `pc_id` binding. The runner reports `ACTIVE` only after the pipeline hooks start, the answer is bound, and `SmallWebRTCConnection.is_connected()` is true. The SaaS path skips local agent-profile lookup and process-wide mode overrides, and it never installs the local text/language app-message handler. Local runner offers without session credentials retain their prior path.

## Configuration and external prerequisites

All platform session configuration remains unset in `.env.example`; `PLATFORM_ENABLE_VOICE_TEST` remains false. The voice runner reads `PLATFORM_API_BASE_URL`, `PLATFORM_SESSION_RUNNER_ID`, `PLATFORM_RUNTIME_SERVICE_TOKEN`, `PLATFORM_WEB_ORIGIN`, `PLATFORM_BILLING_SLICE_MS`, `PLATFORM_BILLING_PERMIT_TTL_MS`, and optionally `PLATFORM_SESSION_RUNTIME_TIMEOUT_SECONDS`. The runner API URL must be a loopback origin, the web origin is exact, and runtime service tokens must be private. Invalid SaaS settings fail only when the session bridge is used and do not change local startup.

Before a local deployment can admit a browser session, an operator must provide:

1. PostgreSQL with a fresh platform schema initialized for this code version. Test only against a disposable fresh database; do not reset an existing user database or use an incompatible schema.
2. `PLATFORM_SESSION_RUNNER_ID` and `PLATFORM_SESSION_RUNNER_URL`, with the latter pointing to the real local loopback runtime bridge.
3. `PLATFORM_RUNTIME_SERVICE_TOKEN`, a private token shared only by the platform and assigned local runner.
4. Positive `PLATFORM_SESSION_OWNER_CAPACITY` and `PLATFORM_SESSION_RUNNER_CAPACITY` limits.
5. `PLATFORM_SESSION_SIGNALING_URL` at the real local HTTPS signaling endpoint and `PLATFORM_WEB_ORIGIN` set to the exact owner browser origin.
6. `PLATFORM_SESSION_JOIN_ISSUER`, `PLATFORM_SESSION_JOIN_PRIVATE_KEY_B64`, and `PLATFORM_SESSION_JOIN_PUBLIC_KEY_B64` containing a matching platform-owned Ed25519 pair; keep the private key on the platform side.
7. Positive `PLATFORM_SESSION_JOIN_TTL_SECONDS`, `PLATFORM_SESSION_MAX_DURATION_MS`, `PLATFORM_SESSION_SETUP_TIMEOUT_SECONDS`, `PLATFORM_SESSION_RECONNECT_GRACE_SECONDS`, and `PLATFORM_SESSION_RUNTIME_TIMEOUT_SECONDS`; these are required and have no code defaults. For fresh schema initialization only, explicitly set `PLATFORM_INITIAL_SESSION_RATE_MINOR_PER_MINUTE`, `PLATFORM_INITIAL_BILLING_SLICE_MS`, and `PLATFORM_INITIAL_BILLING_PERMIT_TTL_MS` together. They seed the first persisted tariff version; missing values fail fresh initialization. Runtime admission reads the active PostgreSQL tariff policy, never a second environment rate source.
8. A real trusted qualification report and stable deployment identity from `PLATFORM_ADAPTER_CAPABILITY_REPORT_PATH`, `PLATFORM_ADAPTER_CAPABILITY_REPORT_HASH`, `PLATFORM_DEPLOYMENT_ADAPTER_ID`, `PLATFORM_DEPLOYMENT_ADAPTER_VERSION`, `PLATFORM_DEPLOYMENT_MODEL_REVISION`, `PLATFORM_DEPLOYMENT_PROFILE_HASH`, and `PLATFORM_DEPLOYMENT_SOURCE_REVISION`, matching the actual qualified EN/HI adapter and published voice settings.
9. A PostgreSQL-backed `DurablePostgresFlowStore` implementing committed event writes, flow-state restore, and idempotent save/load of the exact stop record before budget close. No SQLite, JSONL, or in-memory fallback qualifies.
10. A session-bound `VerifiedPlaybackUsageMeter` sourced only from `committed_playback_receipts`, and the pinned `bot.flow.playback_receipts` implementation that verifies authenticated browser render receipts against exact server-side PCM ranges.
11. Evidence from the installed adapter—not a configured label or raw transport probe—that the reported adapter qualification hash matches the pinned snapshot and supports speech gating, cancellation, metadata, and EN/HI playback.
12. A pinned Pipecat TTS/SmallWebRTC stop-and-drain integration that stops output writes before final cursor cancellation ranges are recorded. Until then interrupted fragments cannot be credited as completed playback.
13. A funded owner wallet for each admitted session and the real platform session service endpoints described above.

Browser `AudioData.timestamp` must be mapped to the server's actual `RawAudioTrack` PTS; a missing or ambiguous mapping yields no receipt. Text-only responses, silent fixtures, byte counts, process health, and diagnostic transport probes are not proof of rendered audio. Do not enable `PLATFORM_ENABLE_VOICE_TEST` until every prerequisite is installed and verified in the intended air-gapped/loopback environment. No credentials, provider downloads, DNS-discovered services, hosted-media fallbacks, or fake successful acknowledgements are supplied by this code.
