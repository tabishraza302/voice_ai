# First-release conversation history, wallet and admin API

Status: the owner history/wallet APIs, platform customer views, admin inventory, audit-protected policy operations, transcript review tools and termination route are implemented in this worktree. This document describes the contracts only; it does not claim production rollout or release qualification.

## Owner history and wallet

All history routes derive identity from the authenticated owner session. Request parameters never select an owner. Session and usage query cursors are scoped to the owner, endpoint and normalized filters. Date bounds must be supplied together, include time zones, be ordered, and span no more than 366 days. Session and usage pages are limited to 100 rows; transcript pages are limited to 200 segments. Responses are `no-store`.

- `GET /api/v1/sessions` returns that owner's persisted session state, kind, version, timing, failure code and pinned agent-version ID. It does not invent a terminal state.
- `GET /api/v1/sessions/{session_id}/config-snapshot` returns only the immutable agent version and tariff snapshot pinned to that owner's session. Cross-owner session IDs and mismatched agent ownership are not found; protected fields fail closed.
- `GET /api/v1/sessions/{session_id}/transcript` uses the existing Task 8 revision-bound event projection, including its gap information and processing state. If transcript content changes between pages, the old cursor is rejected rather than silently mixing revisions.
- `GET /api/v1/usage` returns stored call-budget facts for the owner's sessions. `charged_minor` is the actual stored charge. An `OPEN` budget is labelled provisional; a `CLOSED` budget is labelled final. `authorized_estimate_minor` is a separate estimate calculated from the budget's pinned rate and authorized time ceiling. It is never substituted for a charge. Reserved amount and waived unknown usage are reported from the budget.
- Existing `GET /api/v1/wallet` and `GET /api/v1/wallet/entries` remain the wallet read APIs. Existing `POST /api/v1/admin/users/{user_id}/wallet-adjustments` remains the admin adjustment path and its append-only ledger; this slice does not introduce a second ledger or write directly to wallet totals.

There is intentionally no owner `GET /api/v1/sessions/{session_id}` route in this slice. Task 6 owns the canonical owner-session detail route, avoiding a conflicting lifecycle contract.

## Admin operations

All admin reads require an admin principal and are `no-store`. They expose actual database projections only:

- `GET /api/v1/admin/overview` reports stored user, agent, session, wallet and call-budget aggregates. OPEN-budget charges are separated from CLOSED-budget charges; no rates, conversion, extrapolated activity or fabricated operational metrics are added.
- `GET /api/v1/admin/sessions` filters stored session metadata and budget facts. `GET /api/v1/admin/sessions/{session_id}` returns metadata, pinned tariff snapshot and stored billing facts, never transcript content.
- `POST /api/v1/admin/sessions/{session_id}/transcript-access` is the only admin transcript-content route. It requires admin step-up/MFA, CSRF, an `Idempotency-Key`, and a trimmed 10–500 character support reason. The reason, actor/session, request ID, source IP, target session, transcript revision and page size are audited in the same transaction as the replay record. The replay record stores only session, cursor, page size and revision, not transcript text. A replay against a changed transcript revision conflicts; use a fresh key and re-authorize support access.
- `GET /api/v1/admin/users/{user_id}/wallet` and `/wallet/entries` reuse the existing wallet service and ledger.
- `GET /api/v1/admin/agents` supports owner, enabled, date and text filters for searchable inventory; `GET /api/v1/admin/agents/{agent_id}` inspects the current immutable configuration and its hashes. `POST /api/v1/admin/agents/{agent_id}/disable` requires step-up/MFA, CSRF, idempotency and a reason. It changes the existing `Agent.enabled` flag and records an audit event transactionally. It does not end or rewrite existing sessions.
- `GET /api/v1/admin/templates` and `GET /api/v1/admin/prompts` list versioned platform defaults. Their `POST .../{slug}/versions` routes create audited, idempotent template or prompt versions under step-up/MFA and CSRF. They do not rewrite configurations already pinned to sessions.
- `GET /api/v1/admin/audit-logs` provides a bounded, filter-bound audit listing.
- `GET /api/v1/admin/admission-pauses` and `POST /api/v1/admin/admission-pauses` inspect and update bounded global, owner, or agent admission policies. Changes use expected revisions, step-up/MFA, CSRF, idempotency and transactional audit. Admission checks and policy writes share PostgreSQL transaction advisory locks, including for scopes with no row yet.
- `GET /api/v1/admin/tariffs` lists immutable persisted rate versions; `POST /api/v1/admin/tariffs/versions` appends a new rate under expected-revision, step-up/MFA, CSRF, idempotency and transactional audit. Only the active pointer affects new admissions. Existing sessions keep their pinned tariff and budget rate.
- `POST /api/v1/admin/sessions/{session_id}/transcript-corrections` appends an audited correction without rewriting the original transcript segment. `POST /api/v1/admin/sessions/{session_id}/transcript-visibility` changes owner visibility under an expected policy revision. Both require step-up/MFA, CSRF, idempotency and a reason. Corrections and visibility changes invalidate old transcript cursors; hiding text affects owner reads, not audited admin access.
- `POST /api/v1/admin/sessions/{session_id}/terminate` delegates emergency termination to the session lifecycle service. It does not fake a terminal state: execution is released through the runtime acknowledgement flow.

No new metric labels are emitted by this API. Cursor values are bounded and bound to the endpoint and query filters. All new API routers are mounted by `platform_api/app.py`; existing billing router registrations are unchanged.

## Persisted tariff bootstrap and admission policy

Fresh schema initialization requires all three explicit values: `PLATFORM_INITIAL_SESSION_RATE_MINOR_PER_MINUTE`, `PLATFORM_INITIAL_BILLING_SLICE_MS`, and `PLATFORM_INITIAL_BILLING_PERMIT_TTL_MS`. They seed exactly the first persisted tariff version; if any value is missing, fresh initialization fails closed. Runtime admission reads the database's active tariff record and copies its values into the durable session snapshot and call budget. Admin changes append immutable versions and switch only the active pointer, so existing calls keep their original price and configuration. There is no runtime environment-rate fallback or second rate source.

Admission pause policies are stored in PostgreSQL with bounded global, owner and agent scopes. Both owner and guest admission checks take shared transaction-level advisory locks for all applicable scopes; a policy mutation takes the corresponding exclusive lock. This serializes admission and pause changes even before a pause row exists. Database failures do not turn into an unpaused fallback.

Transcript source segments remain append-only. Admin corrections are separate append-only records overlaid on reads. The owner-visible flag defaults to visible; when hidden, owner transcript responses contain no segment text, while admin content access still requires a support reason, step-up/MFA, CSRF, idempotency and an audit record.
