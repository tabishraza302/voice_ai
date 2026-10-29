# First-release conversation history, wallet and admin API

Status: backend API contract implemented on the Task 9 branch. The separate product UI and full Task 6 session-state contract are not part of this backend slice. This document describes stored platform facts; it does not claim production rollout or completed release qualification.

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
- `GET /api/v1/admin/agents` lists agent inventory. `POST /api/v1/admin/agents/{agent_id}/disable` requires step-up/MFA, CSRF, idempotency and a reason. It changes the existing `Agent.enabled` flag and records an audit event transactionally. It does not end or rewrite existing sessions.
- `GET /api/v1/admin/templates` and `GET /api/v1/admin/prompts` list versioned platform defaults. Their `POST .../{slug}/versions` routes create audited, idempotent template or prompt versions under step-up/MFA and CSRF. They do not rewrite configurations already pinned to sessions.
- `GET /api/v1/admin/audit-logs` provides a bounded, filter-bound audit listing.

No new metric labels are emitted by this API. Cursor values are bounded and bound to the endpoint and query filters. All new API routers are mounted by `platform_api/app.py`; existing billing router registrations are unchanged.

## Explicit Task 6 dependencies and out-of-scope controls

Session state values come from persisted session rows; this API does not create, normalize, or transition lifecycle statuses. Task 6's canonical owner detail endpoint and final lifecycle/admission state meanings must be integrated before the owner session-detail experience is complete.

The only tariff source in the current runtime contract is `PLATFORM_SESSION_RATE_MINOR_PER_MINUTE` plus `PLATFORM_SESSION_TARIFF_VERSION`, pinned to each `Session.tariff_snapshot` and `CallBudget`. There is no admin-owned tariff policy record or safe in-process writer, so this slice adds no tariff mutation route. The current platform schema also has no transcript-visibility policy model or established correction event/audit contract. This slice therefore adds no visibility override, transcript correction, emergency stop, or active-call termination endpoint. Those controls need their owning lifecycle and one shared tariff/visibility/audit contract, not a second rate source or model. Template and prompt versioning is included because those existing versioned models and validation boundaries are already present.
