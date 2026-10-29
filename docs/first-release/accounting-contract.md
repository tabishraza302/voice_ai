# First-release prepaid accounting

Status: selected design for a fresh first release; application implementation is a planned task. This replaces the earlier account-pool/coordinator/epoch design. Currency is INR; amounts are integer paise. No general ledger, invoicing, taxes, subscriptions, distributed account coordinator or provider-cost-based customer price is introduced.

## Scope and price

Each user has one wallet and an append-only list of credits, charges, refunds and administrative corrections. The dashboard shows posted, reserved and available balance. Retail rate and billing policy are pinned when a session is created. Admin-funded credit is sufficient for the first release; payment-gateway integration is optional and remains disabled until independently integrated and verified.

Accumulate elapsed ACTIVE time using a monotonic clock across the session's connected intervals. Setup, disconnected reconnect grace and time after ending are free. Aggregate nanoseconds first; expose cumulative whole milliseconds by floor division, without flooring each audio chunk separately. For pinned nonnegative rate `r` paise/minute and cumulative billable milliseconds `t`:

```text
charge(t, r) = (r * t + 59_999) // 60_000
delta_charge = charge(new_proven_ms, r) - charged_minor
available_minor = posted_minor - reserved_minor
posted_minor >= reserved_minor >= 0
wallet.reserved_minor = SUM(open call_budgets.reserved_minor)
wallet.posted_minor = SUM(wallet_entries.amount_minor)
```

The ceiling is applied to each session's cumulative cost, never separately to every checkpoint. At 200 paise/minute, 10 seconds cost 34 paise and 30 seconds cost 100 paise. Three independent 10-second calls need 102 paise. Partial seconds use the same millisecond formula; there is no whole-minute or whole-second minimum. The smallest positive charged amount is one paise under this disclosed rounding rule. Reject negative/overflowing values; compute with Python integers and check signed BIGINT bounds before storage. Rate zero is explicitly free.

## Minimal records

- `wallet_accounts`: UUID `user_id` PK/FK, INR currency, BIGINT `posted_minor`, BIGINT `reserved_minor`, version; checks posted ≥ reserved ≥ 0.
- `wallet_entries`: UUID ID, user FK, kind `credit|charge|refund|adjustment`, signed BIGINT amount, optional session FK, unique operation key, actor, reason, UTC time. Charges are negative; credits/refunds positive. Amounts are immutable; corrections append new entries.
- `call_budgets`: session PK/FK, owner FK, pinned rate, cumulative proven milliseconds, cumulative authorized milliseconds, cumulative charged paise, reserved paise, permit expiry, version and `OPEN|CLOSED`. One budget per session. The associated session identifies its only assigned runner.
- `billing_operations`: UNIQUE `(session_id, operation_id)`, canonical request SHA-256, full response/outcome; persist in the same transaction as money changes.
- `meter_checkpoints`: `(session_id, operation_id)` unique, cumulative proven time, assigned runner, session/config versions, pinned rate, immutable tariff snapshot and UTC receipt. These are synchronous evidence, independent of RabbitMQ ingestion.

These tables belong to the new platform database/Base. Do not reuse the existing local global-active-agent model, seed behavior or local SQLite fallback. No old records are imported.

## Service boundary

`platform_api/billing/pricing.py` provides pure `charge_minor(rate_minor_per_minute: int, cumulative_billable_ms: int) -> int`. `platform_api/billing/service.py` provides the transaction functions below; `platform_api/billing/routes.py` exposes authenticated HTTP wrappers. Only these functions change a wallet. The event consumer records history/provider expense and never debits a wallet.

```text
credit_wallet(actor, user_id, amount_minor, reason, operation_id) -> WalletView
checkpoint_and_reserve(runtime_principal, session_id, operation_id,
                       expected_budget_version, cumulative_billable_ms,
                       requested_future_ms) -> BudgetPermit
close_budget(runtime_principal, session_id, operation_id,
             expected_budget_version, cumulative_billable_ms,
             stop_evidence, stop_attestation) -> BudgetView
expire_stopped_budget(session_id, operation_id, now) -> BudgetView
```

Owner identity is derived from the stored session, never from an internal request's arbitrary owner field. Validate the service credential, assigned runner and session state. All amounts/time fields are bounded integers; reject unknown request fields. Validation errors return 422; unavailable DB returns retryable 503; closed sessions reject new authority with 409. Duplicate identical operations return the recorded result; conflicting request hashes under the same key return 409 without mutations.

A session that exists without its required call budget is a conflict (409), not a missing session (404).

`POST /internal/sessions/{id}/budget/checkpoint` accepts exactly `operation_id`, `expected_budget_version`, `cumulative_billable_ms`, `requested_future_ms`. Response `BudgetPermit` contains `session_id`, `budget_version`, `proven_ms`, `authorized_through_ms`, `reserved_minor`, `permit_id`, `expires_at`, `funds_exhausted`. `POST /internal/sessions/{id}/budget/close` accepts `operation_id`, `expected_budget_version`, `cumulative_billable_ms`, optional `stop_evidence`, and optional `stop_attestation`, with no requested future time. While a permit is live, `stop_attestation` must bind the authenticated runner, session, registration, and current ending-session version. `GET /api/v1/wallet` and `/wallet/entries` are owner-only. `POST /api/v1/admin/users/{id}/wallet-adjustments` requires admin step-up, signed amount, reason and an Idempotency-Key; it cannot reduce posted funds below outstanding reservations.

## Checkpoint/reservation transaction

The runner asks for a small slice before any billable work. `BILLING_SLICE_MS=1000` is a development starting value, configurable and to be qualified under the target deployment's DB latency; it is not a universal performance measurement. `requested_future_ms` is capped by that setting and session hard-duration limit. One serialized budget loop operates per session; there is one runner process in v1. No distributed allocator or per-user epoch is needed.

1. Begin a PostgreSQL transaction. Lock the owner's wallet row first, then its call-budget row, then the session row when needed. All financial functions use this lock order.
2. Resolve the operation key/hash before expected-version checks so an identical lost-response retry returns its original result. Under the ordered locks, confirm the session still belongs to the owner whose wallet row was locked and that budget/session ownership agrees; reject ownership changes with 409 before returning stored output or changing money. For meter requests, revalidate the assigned runner before returning a stored response or applying a new operation. For a new operation, check that the session is live/assigned, the budget version matches, and `old_proven_ms <= supplied_proven_ms <= old_authorized_ms`. Initial proof is zero. A decreasing or unauthorized time report is 409 and changes nothing.
3. Compute the cumulative charge delta. Subtract it from posted balance and from that budget's reserved amount and the wallet's reserved amount. Append one charge entry if delta is positive; write the checkpoint. This settles only evidence received through this authenticated path.
4. Compute desired cumulative authorization `desired_ms = max(old_authorized_ms, supplied_proven_ms + capped_future_ms)`. Let `funds = charged_minor + remaining_budget_reservation + wallet.available_minor`. If rate > 0, affordable cumulative milliseconds are `floor(funds * 60_000 / rate)`; if rate = 0 the desired duration is affordable. Select `authorized_ms = min(desired_ms, affordable_ms, session_duration_limit_ms)`.
5. The new required reservation is `charge(authorized_ms, rate) - charge(supplied_proven_ms, rate)`. Increase the budget/wallet reservation by the difference from its remaining reservation. Existing reserved authority cannot be allocated to another session while executable. If there is no further funded duration, return `funds_exhausted=true`; old confirmed authority remains valid only inside its existing cumulative/time caps.
6. Store the incremented budget version, permit and idempotent response in the same commit. No authority is usable until its committed response arrives. A timeout is an unknown outcome: retry the same operation/body, not a new debit. On definite denial do not create unconfirmed authority.

The permit is a cumulative ceiling, not an additional duration to add on each retry. Bind it to the session, runner and budget version. Its wall expiry is set by the server, capped at `hard_end_deadline` when present, and also checked by the runner using a conservative monotonic deadline subtracting request round-trip time. At or after the hard deadline, a checkpoint may settle already-proven time but cannot issue further authority. Stop paid processing/output before either duration or expiry is exceeded. On DB/API failure, use only still-valid confirmed permission, then stop; no overdraft. A warning consumes the same funded time. A provider request's cost cap is a separate platform-risk control.

## Concurrency, closure and crashes

Concurrent calls contend on their owner's wallet row. They share available funds but have separate small reservations. One renewal can win before another; **v1 promises no equal-time fairness**. Do not divide the wallet by configured maximum concurrency. Different users lock different rows. Grant sizes are intentionally small to limit temporary stranded funds and outage exposure.

On normal end, stop media, then settle final proven cumulative time and release the verified remainder in one idempotent close transaction. Set the budget CLOSED. A lost close response is retried with the same key. Reconnect within grace retains the same session, budget, rate and cumulative duration; its disconnected interval is uncharged.

If a runner is lost, mark the session terminal and wait until its last permit is expired and no paid work remains executable. Settle only checkpoints already durably committed. Release the remaining reservation; unproven elapsed time is waived as bounded platform exposure, never guessed and charged later. This keeps v1 accounting simple without an indefinite reconciliation queue. Record the closure reason and waived-unknown-usage flag; do not fabricate an exact unobserved duration. Late messages cannot reopen a closed budget or add a surprise customer debit. Provider usage can still arrive as expense history.

Every close/expiry path needs confirmed stop or expired permission. An early administrative balance release cannot invalidate still-live spending permission. Multiple independent runner processes serving the same live session are outside v1 and must be rejected by registration/join ownership, not papered over with balance assumptions.

## Required cases and files

Create `platform_api/billing/{__init__,pricing,models,schemas,service,routes}.py`, `tests/platform_api/test_billing_amounts.py`, `tests/platform_api/test_billing_reservations.py`, `tests/platform_api/test_billing_idempotency.py`, `tests/platform_api/test_billing_recovery.py`, `tests/platform_api/test_billing_sequences.py`, `tests/platform_api/test_billing_meter_api.py`; add route registration to `platform_api/app.py` and table imports to `platform_api/models/__init__.py`. No existing application file is deleted. The runtime budget client is `bot/runtime/budget_client.py`; the authoritative arithmetic stays in the API.

Tests must cover zero/setup charge, subsecond charge, cumulative checkpoints versus one final charge, 3×10-second ceiling, concurrent users/calls, lost-response idempotency, conflicting replay, negative/overflow input, expired permits, DB outage, reconnect gap, crash/write-off after permit expiry, repeated closure and administrative debit below held funds. Run real financial concurrency tests against fresh PostgreSQL, not SQLite. The independent executable pricing/allocation examples in `accounting_oracle.py` are specification fixtures, not production accounting or proof of a running distributed system.

PostgreSQL row-lock semantics and explicit initial table creation use the selected existing libraries: [PostgreSQL explicit locking](https://www.postgresql.org/docs/current/explicit-locking.html), [SQLAlchemy metadata creation](https://docs.sqlalchemy.org/en/20/core/metadata.html#creating-and-dropping-database-tables). The contract above defines the application policy; the source links do not constitute a concurrency or deployment test.
