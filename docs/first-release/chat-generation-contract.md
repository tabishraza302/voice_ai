# Chat Test and agent generation contract

## Chat Test

Chat Test is a text-only preview of a saved agent configuration. It runs the same flow reducer as the voice controller, but it does not reserve a voice slot, invoke speech services, or publish an agent.

### Snapshot identity

`GET /api/v1/agents/{agent_id}/draft` returns `snapshot_hash`. It is `null` when the draft has a Chat Test-blocking configuration or flow error, or its runtime LLM is not qualified. Errors limited to STT, TTS voices, or playback capability do not make the hash null; those are still required for publication. The full `validation.errors` list continues to report publication-only voice errors, so use `snapshot_hash` to determine Chat Test eligibility. Otherwise the value is the lowercase SHA-256 of canonical JSON v1 containing the draft `authoring_hash`, current `persona_prompt`, and active platform `policy_hash`. Treat it as opaque; clients must not calculate it.

A published version is addressed by its immutable `artifact_hash`. A chat session stores its selected compiled flow, prompt and policy text, persona, model selection, and snapshot identity at creation. Later draft, persona, or active-policy changes do not change an existing session. A new draft-based session must present the current `snapshot_hash`; a stale hash returns HTTP 409 with error code `REVISION_CONFLICT` before any turn is committed. A published version can be started by presenting its exact `artifact_hash`.

### Start or continue a test

`POST /api/v1/agents/{agent_id}/test/chat` requires authentication, CSRF, and this JSON body:

```json
{
  "snapshot_hash": "<64 lowercase hexadecimal characters>",
  "test_id": "<client-generated UUID>",
  "expected_turn_sequence": 1,
  "text": "I am salaried."
}
```

For a new session, the client must generate and persist a UUID `test_id` before sending the first request. Reuse that ID and the exact original payload to recover a turn if the response is lost. For each later turn, send the same `test_id`, pinned `snapshot_hash`, and next sequence number. Text is nonblank and limited to 8,192 characters. The response contains `test_id`, `turn_sequence`, `reply`, `flow_state`, `trace`, and `outcome`.

A sequence or snapshot mismatch returns HTTP 409 with error code `REVISION_CONFLICT` before a new user message is inserted. A request for a pending turn also returns 409 while its lease is active. After an interrupted request's lease expires, retry the same `test_id`, `snapshot_hash`, `expected_turn_sequence`, and exact text to resume the already-persisted user turn. A different text cannot replace it. If the turn had already reached a terminal reducer state, the retry finalizes that state without another model call. Sessions are owner- and agent-scoped; a missing or foreign session returns 404.

The server commits the user message before calling the configured runtime model. Required authored `inform`, `summary`, and `end` output is rendered by the controller; the assistant message is committed before each `ChatCommittedOutputEvent` advances the reducer and before the response is returned. A model failure leaves the user message and failure trace durable, returns an empty `reply` with `outcome: "failed"`, and adds a safe diagnostic (`code`, `path`, `severity`, optional `message`) to the trace. Provider details, prompts, and secrets are not returned. The server does not invent a fallback answer. Start a new test ID to reset.

`GET /api/v1/agents/{agent_id}/test/chat/{test_id}` recovers the pinned `snapshot_hash`, durable messages, flow state, trace, turn sequence, `pending_turn_sequence`, and outcome. A pending turn has a durable user message but no assistant message until the runtime result and reducer state commit together. Recovery responses are private and marked `Cache-Control: no-store`.

Chat calls use the operator-configured `PLATFORM_RUNTIME_LLM_BASE_URL` and the model pinned by the saved configuration. `PLATFORM_ENABLE_CHAT_TEST` must be enabled. Runtime calls have a configured timeout and token limit. Chat is unavailable rather than falling back to a fake or downloaded model.

## Agent generation jobs

`GET /api/v1/generation-models` returns the operator-qualified generation models. `POST /api/v1/agents/generate` requires authentication, CSRF, and a requirement, selected `generation_model_id`, language, and `token_budget`. It returns HTTP 202 with `{ "job_id": "<UUID>", "status": "QUEUED" }`; it creates a job, not an agent or draft.

Poll `GET /api/v1/generation-jobs/{job_id}` for `status`, `proposal`, `unknown_business_facts`, `validation_warnings`, and `usage`. States are `QUEUED`, `RUNNING`, `CANCELLING`, `COMPLETED`, `FAILED`, `CANCELLED`, and `EXPIRED`. Only the job owner can read or cancel it. `POST /api/v1/generation-jobs/{job_id}/cancel` accepts `{}`. Queued jobs become `CANCELLED` immediately; running jobs become `CANCELLING` until the owning worker aborts the provider request and commits `CANCELLED`. Poll while the status is `CANCELLING`.

The PostgreSQL-backed worker claims jobs with expiring leases, limits queue depth, running work, per-owner active jobs, token budgets, and execution time, and validates the complete proposal against the current schema and qualified model catalog. A completed proposal is still an editable proposal; the user must review and save it through the normal draft API. Generation never auto-saves or publishes an agent. Queued cancellation releases its reservation. A started cancellation keeps its reserved 24-hour budget and records usage as unknown when the provider request is aborted before reporting usage. A worker interruption requeues at most once, reserves budget for the retry, and records usage as unknown when the interrupted provider call did not return usage.

Generation requires `PLATFORM_ENABLE_GENERATION` and the operator-configured `PLATFORM_GENERATION_LLM_BASE_URL`. Jobs use the configured generation endpoint, bounded timeout and token budget, and durable PostgreSQL records. Neither feature uses SQLite or downloads model files.
