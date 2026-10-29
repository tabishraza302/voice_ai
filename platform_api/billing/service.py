"""Prepaid accounting, wallet, reservations, and synchronous meter service.

Follows accounting-contract.md:
- Lock order: owner's wallet row first, then call-budget row.
- Check idempotency operation key/hash before version check for clean lost-response retry.
- Pure integer pricing with charge_minor.
- Cumulative permits (not incremental duration).
- Expiry and recovery semantics for lost runners.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any
import uuid
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.audit import record_audit
from platform_api.auth.errors import (
    ForbiddenError,
    NotFoundError,
    PlatformError,
)
from platform_api.billing.models import (
    BillingOperation,
    CallBudget,
    MeterCheckpoint,
    WalletAccount,
    WalletEntry,
)
from platform_api.billing.pricing import MAX_BIGINT, MIN_BIGINT, charge_minor
from platform_api.billing.schemas import (
    BudgetPermit,
    BudgetView,
    WalletEntriesPage,
    WalletEntryView,
    WalletView,
    decode_stored_response,
)
from platform_api.models.session import Session

MAX_OPERATION_ID_LENGTH = 128

# New spending authority is only valid for states that can still execute paid
# work. Unknown/future states fail closed rather than being treated as live.
LIVE_SESSION_STATES = frozenset(
    {"ACTIVE", "CONNECTING", "CONNECTED", "RECONNECTING"}
)
TERMINAL_SESSION_STATES = frozenset(
    {"CLOSED", "COMPLETED", "FAILED", "EXPIRED", "ENDED", "TERMINATED", "CANCELLED", "STOPPED"}
)


@dataclass(frozen=True)
class RuntimePrincipal:
    """Authenticated runtime runner principal."""

    runner_id: str
    service_id: str | None = None


# --- Billing Exceptions -------------------------------------------------------


class BillingError(PlatformError):
    """Base exception for billing operations."""

    status_code: int = 400
    code: str = "BILLING_ERROR"
    message: str = "Billing operation failed"


class BillingValidationError(PlatformError):
    """Bounded integer/identity validation failure (HTTP 422)."""

    status_code: int = 422
    code: str = "VALIDATION"
    message: str = "Invalid billing request"


class ConflictError(PlatformError):
    """Base conflict exception."""

    status_code: int = 409
    code: str = "CONFLICT"
    message: str = "Operation conflict"


class ReservationHoldError(ConflictError):
    """Raised when adjustment would reduce balance below outstanding holds."""

    code: str = "RESERVATION_HOLD"
    message: str = "Operation violates outstanding reservation hold"


class BudgetClosedError(ConflictError):
    """Raised when an operation is attempted on a closed budget."""

    code: str = "BUDGET_CLOSED"
    message: str = "Call budget is closed"


class BudgetVersionMismatchError(ConflictError):
    """Raised when optimistic concurrency budget version does not match."""

    code: str = "VERSION_MISMATCH"
    message: str = "Budget version mismatch"


class IdempotencyConflictError(ConflictError):
    """Raised when request payload conflicts with recorded operation."""

    code: str = "IDEMPOTENCY_CONFLICT"
    message: str = "Conflicting request payload for existing operation key"


class InvalidTimeReportError(ConflictError):
    """Raised when proven billable time decreases or exceeds authorization."""

    code: str = "INVALID_TIME_REPORT"
    message: str = "Decreasing or unauthorized billable time report"


# --- Hash & Cursor Utilities --------------------------------------------------


def _canonical_hash(payload: dict[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of canonical JSON."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _encode_cursor(created_at: datetime, entry_id: uuid.UUID) -> str:
    payload = json.dumps({"t": created_at.isoformat(), "id": str(entry_id)}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(raw_cursor: str) -> tuple[datetime, uuid.UUID]:
    pad = len(raw_cursor) % 4
    padded = raw_cursor + ("=" * (4 - pad)) if pad else raw_cursor
    data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    created_at = datetime.fromisoformat(data["t"])
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("cursor timestamp must include a timezone")
    return created_at, uuid.UUID(data["id"])


def _charge_entry_operation_id(
    session_id: uuid.UUID, operation_id: str
) -> str:
    """Build a globally unique ledger key for a session charge.

    Billing operation ids are unique per session, so the session id must be
    part of the ledger's globally unique operation key.
    """
    return f"{session_id}:{operation_id}:charge"


def _validate_operation_id(operation_id: str) -> None:
    if not isinstance(operation_id, str) or not operation_id:
        raise BillingValidationError("operation_id is required")
    if len(operation_id) > MAX_OPERATION_ID_LENGTH:
        raise BillingValidationError(
            f"operation_id exceeds {MAX_OPERATION_ID_LENGTH} characters"
        )


def _validate_bounded_int(
    name: str,
    value: int,
    *,
    minimum: int = 0,
    maximum: int = MAX_BIGINT,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BillingValidationError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise BillingValidationError(
            f"{name} must be between {minimum} and {maximum}"
        )


def _ensure_live_session(session: Session) -> None:
    if session.state in TERMINAL_SESSION_STATES:
        raise ConflictError("Session is terminal; cannot checkpoint or reserve")
    if session.state not in LIVE_SESSION_STATES:
        raise ConflictError(
            f"Session state {session.state!r} does not accept new billing authority"
        )


def _ensure_budget_owner(budget: CallBudget, session: Session) -> None:
    if budget.owner_user_id != session.owner_user_id:
        raise ConflictError("Call budget owner does not match the stored session owner")


def _normalize_stop_evidence(stop_evidence: Any) -> dict[str, Any] | None:
    if stop_evidence is None:
        return None
    if hasattr(stop_evidence, "model_dump"):
        return stop_evidence.model_dump(mode="json")
    if isinstance(stop_evidence, dict):
        return json.loads(json.dumps(stop_evidence, default=str))
    raise BillingValidationError("stop_evidence must be an object or null")


def _duration_ms(start: datetime | None, end: datetime | None) -> int | None:
    """Whole milliseconds between two instants, clamped at zero."""
    if start is None or end is None:
        return None
    delta = end - start
    return max(
        0,
        delta.days * 86_400_000
        + delta.seconds * 1_000
        + delta.microseconds // 1_000,
    )


# --- Core Service Functions ---------------------------------------------------

async def credit_wallet(
    db: AsyncSession,
    *,
    actor: str,
    user_id: uuid.UUID,
    amount_minor: int,
    reason: str,
    operation_id: str,
    kind: str = "credit",
    session_id: uuid.UUID | None = None,
    now: datetime | None = None,
    audit_actor_kind: str | None = None,
    audit_actor_id: uuid.UUID | None = None,
    audit_session_id: uuid.UUID | None = None,
) -> WalletView:
    """Credit or adjust a user's wallet balance within an atomic transaction.

    Enforces:
    - Advisory transaction lock on operation key to serialize simultaneous calls.
    - Fast-path and post-lock idempotency recheck (replay returns original WalletView, conflict 409).
    - Lock order: wallet row locked FOR UPDATE.
    - Balance invariant: posted_minor >= reserved_minor >= 0.
    - Atomic commit of balance change + ledger entry + audit log + idempotency record.
    """
    ts = now or datetime.now(timezone.utc)
    _validate_operation_id(operation_id)
    _validate_bounded_int(
        "amount_minor", amount_minor, minimum=MIN_BIGINT, maximum=MAX_BIGINT
    )
    if amount_minor == 0:
        raise BillingValidationError("amount_minor cannot be zero")
    if kind not in {"credit", "charge", "refund", "adjustment"}:
        raise BillingValidationError("kind must be credit, charge, refund or adjustment")
    if kind == "charge" and amount_minor >= 0:
        raise BillingValidationError("charge entries must be negative")
    if kind in {"credit", "refund"} and amount_minor <= 0:
        raise BillingValidationError(f"{kind} entries must be positive")
    if not isinstance(reason, str) or not reason.strip():
        raise BillingValidationError("reason is required")

    req_payload = {
        "action": "credit_wallet",
        "user_id": str(user_id),
        "amount_minor": amount_minor,
        "reason": reason,
        "operation_id": operation_id,
        "kind": kind,
        "session_id": str(session_id) if session_id is not None else None,
    }
    req_hash = _canonical_hash(req_payload)

    # 1. Fast-path check before lock
    op_filter = (
        BillingOperation.session_id.is_(None)
        if session_id is None
        else BillingOperation.session_id == session_id
    )
    stmt_op = select(BillingOperation).where(
        op_filter,
        BillingOperation.operation_id == operation_id,
    )
    existing_op = (await db.execute(stmt_op)).scalar_one_or_none()
    if existing_op is not None:
        if existing_op.request_hash != req_hash:
            raise IdempotencyConflictError("Conflicting payload under the same operation key")
        return decode_stored_response(WalletView, existing_op.response_body)
    # 2. Acquire transaction-level advisory lock on operation key to serialize concurrent replay
    op_key = f"{session_id or 'global'}:{operation_id}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:op_key))"), {"op_key": op_key})

    # 3. Lock wallet row FOR UPDATE
    stmt_wallet = (
        select(WalletAccount)
        .where(WalletAccount.user_id == user_id)
        .with_for_update()
    )
    wallet = (await db.execute(stmt_wallet)).scalar_one_or_none()
    if wallet is None:
        raise NotFoundError("Wallet account not found")

    # 4. RE-CHECK idempotency under lock (simultaneous request could have committed)
    existing_op_locked = (await db.execute(stmt_op)).scalar_one_or_none()
    if existing_op_locked is not None:
        if existing_op_locked.request_hash != req_hash:
            raise IdempotencyConflictError("Conflicting payload under the same operation key")
        return decode_stored_response(WalletView, existing_op_locked.response_body)

    if wallet.currency != "INR":
        raise BillingError(f"Unsupported wallet currency {wallet.currency!r}; INR required")

    new_posted = wallet.posted_minor + amount_minor
    if new_posted > MAX_BIGINT or new_posted < MIN_BIGINT:
        raise BillingValidationError("Adjustment would overflow the signed 64-bit balance")
    if new_posted < wallet.reserved_minor:
        raise ReservationHoldError(
            "Adjustment would reduce posted balance below outstanding reservations"
        )
    if new_posted < 0:
        raise ReservationHoldError("Adjustment would produce negative posted balance")

    wallet.posted_minor = new_posted
    wallet.version += 1
    wallet.updated_at = ts

    # 5. Append ledger entry
    entry = WalletEntry(
        id=uuid.uuid4(),
        user_id=user_id,
        kind=kind,
        amount_minor=amount_minor,
        session_id=session_id,
        operation_id=operation_id,
        actor=actor,
        reason=reason,
        created_at=ts,
    )
    db.add(entry)

    view = WalletView(
        user_id=wallet.user_id,
        currency=wallet.currency,  # type: ignore[arg-type]
        posted_minor=wallet.posted_minor,
        reserved_minor=wallet.reserved_minor,
        available_minor=wallet.posted_minor - wallet.reserved_minor,
        version=wallet.version,
        updated_at=wallet.updated_at,
    )

    # 6. Atomic audit record within the SAME transaction (if requested)
    if audit_actor_kind:
        await record_audit(
            db,
            actor_kind=audit_actor_kind,
            actor_id=audit_actor_id,
            session_id=audit_session_id,
            action="wallet.adjustment",
            target_type="user",
            target_id=user_id,
            reason=reason,
            result="success",
            change_summary={
                "amount_minor": amount_minor,
                "idempotency_key": operation_id,
                "posted_minor": view.posted_minor,
                "reserved_minor": view.reserved_minor,
            },
            created_at=ts,
        )

    # 7. Record operation
    op = BillingOperation(
        id=uuid.uuid4(),
        session_id=session_id,
        operation_id=operation_id,
        request_hash=req_hash,
        response_body=view.model_dump(mode="json"),
        status_code=200,
        created_at=ts,
    )
    db.add(op)
    await db.commit()
    return view

async def checkpoint_and_reserve(
    db: AsyncSession,
    *,
    runtime_principal: RuntimePrincipal,
    session_id: uuid.UUID,
    operation_id: str,
    expected_budget_version: int,
    cumulative_billable_ms: int,
    requested_future_ms: int,
    billing_slice_ms: int,
    permit_ttl_ms: int,
    now: datetime | None = None,
) -> BudgetPermit:
    """Process synchronous billable time checkpoint and cumulative reservation renewal.

    Follows accounting-contract.md:
    Measurement caveat: billing_slice_ms and permit_ttl_ms are deployment budgets
    calibrated to target database latency and network SLOs; there is no universal default.
    1. Authenticate/authorize assigned runner FIRST (before replay lookup or row locking).
    2. Acquire transaction-level advisory lock on session operation key.
    3. Check operation key/hash after authorization to prevent cross-runner cached response disclosure.
    4. Lock owner's wallet row first, then call-budget row.
    5. Re-verify runner identity and idempotency under lock.
    6. Validate proof bounds, settle delta charge, compute authorization, and commit atomic permit.
    """
    if billing_slice_ms <= 0:
        raise ValueError("billing_slice_ms must be positive")
    if permit_ttl_ms <= 0:
        raise ValueError("permit_ttl_ms must be positive")

    _validate_operation_id(operation_id)
    _validate_bounded_int(
        "expected_budget_version", expected_budget_version, minimum=1
    )
    _validate_bounded_int("cumulative_billable_ms", cumulative_billable_ms)
    _validate_bounded_int("requested_future_ms", requested_future_ms)

    ts = now or datetime.now(timezone.utc)

    req_payload = {
        "action": "checkpoint_and_reserve",
        "session_id": str(session_id),
        "operation_id": operation_id,
        "expected_budget_version": expected_budget_version,
        "cumulative_billable_ms": cumulative_billable_ms,
        "requested_future_ms": requested_future_ms,
    }
    req_hash = _canonical_hash(req_payload)

    # 1. Look up session first
    stmt_session = (
        select(Session)
        .where(Session.id == session_id)
        .execution_options(populate_existing=True)
    )
    session = (await db.execute(stmt_session)).scalar_one_or_none()
    if session is None:
        raise NotFoundError("Session not found")

    # 2. Authenticate/authorize assigned runner FIRST (fail closed if unassigned or mismatched)
    if not session.runner_id:
        raise ForbiddenError("Session has no assigned runner")
    if runtime_principal.runner_id != session.runner_id:
        raise ForbiddenError("Runner ID does not match assigned session runner")
    # Advisory lock on session operation key to serialize concurrent replay
    op_key = f"{session_id}:{operation_id}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:op_key))"), {"op_key": op_key})

    # Check operation key/hash BEFORE terminal status checks per contract line 46/55
    stmt_op = select(BillingOperation).where(
        BillingOperation.session_id == session_id,
        BillingOperation.operation_id == operation_id,
    )
    existing_op = (await db.execute(stmt_op)).scalar_one_or_none()
    if existing_op is not None and existing_op.request_hash != req_hash:
        raise IdempotencyConflictError("Conflicting payload under the same operation key")

    # New operations must be live. Replays also take the normal row locks below,
    # where runner and budget ownership are refreshed before returning the result.
    if existing_op is None:
        _ensure_live_session(session)
    wallet_owner_user_id = session.owner_user_id
    stmt_wallet = (
        select(WalletAccount)
        .where(WalletAccount.user_id == wallet_owner_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    wallet = (await db.execute(stmt_wallet)).scalar_one_or_none()
    if wallet is None:
        raise NotFoundError("Wallet account not found")

    stmt_budget = (
        select(CallBudget)
        .where(CallBudget.session_id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    budget = (await db.execute(stmt_budget)).scalar_one_or_none()
    if budget is None:
        raise ConflictError("Call budget not found for existing session")

    # Re-verify session runner under lock against concurrent reassignment
    stmt_session_locked = (
        select(Session)
        .where(Session.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    session_locked = (await db.execute(stmt_session_locked)).scalar_one_or_none()
    if session_locked is None or not session_locked.runner_id or session_locked.runner_id != runtime_principal.runner_id:
        raise ForbiddenError("Runner ID does not match assigned session runner")
    if session_locked.owner_user_id != wallet_owner_user_id:
        raise ConflictError("Session owner changed while billing rows were being locked")
    _ensure_budget_owner(budget, session_locked)
    # Re-check idempotency only after locked authorization and ownership checks.
    existing_op_locked = (await db.execute(stmt_op)).scalar_one_or_none()
    if existing_op_locked is not None:
        if existing_op_locked.request_hash != req_hash:
            raise IdempotencyConflictError("Conflicting payload under the same operation key")
        return decode_stored_response(BudgetPermit, existing_op_locked.response_body)

    # For new operations, verify session and budget are not closed.
    _ensure_live_session(session_locked)
    if budget.status == "CLOSED":
        raise BudgetClosedError("Call budget is closed")

    if budget.version != expected_budget_version:
        raise BudgetVersionMismatchError(
            f"Budget version mismatch: expected {expected_budget_version}, current {budget.version}"
        )

    # Validate proof bounds: old_proven_ms <= supplied_proven_ms <= old_authorized_ms
    if cumulative_billable_ms < budget.cumulative_proven_ms:
        raise InvalidTimeReportError(
            f"Billable time cannot decrease: {cumulative_billable_ms} < {budget.cumulative_proven_ms}"
        )
    if cumulative_billable_ms > budget.cumulative_authorized_ms:
        raise InvalidTimeReportError(
            f"Billable time exceeds authorized limit: {cumulative_billable_ms} > {budget.cumulative_authorized_ms}"
        )

    # 7. Settle cumulative delta charge
    new_proven_ms = cumulative_billable_ms
    try:
        new_cost = charge_minor(budget.pinned_rate, new_proven_ms)
    except ValueError as exc:
        raise BillingValidationError(str(exc)) from exc
    delta_charge = new_cost - budget.cumulative_charged_minor
    if delta_charge < 0:
        raise BillingError("Negative charge delta calculated")

    if delta_charge > 0:
        wallet.posted_minor -= delta_charge
        wallet.reserved_minor -= delta_charge
        budget.cumulative_charged_minor += delta_charge
        remaining_budget_reservation = budget.reserved_minor - delta_charge
        if remaining_budget_reservation < 0:
            raise BillingError("Charge exceeds the budget's remaining reservation")
        budget.reserved_minor = remaining_budget_reservation

        charge_entry = WalletEntry(
            id=uuid.uuid4(),
            user_id=wallet.user_id,
            kind="charge",
            amount_minor=-delta_charge,
            session_id=session_id,
            operation_id=_charge_entry_operation_id(session_id, operation_id),
            actor=f"runner:{runtime_principal.runner_id}",
            reason=f"Usage charge for session {session_id} up to {new_proven_ms}ms",
            created_at=ts,
        )
        db.add(charge_entry)
    else:
        remaining_budget_reservation = budget.reserved_minor

    # Synchronous durable meter checkpoint. Capture the immutable session,
    # config and tariff evidence alongside the proven cumulative time.
    checkpoint = MeterCheckpoint(
        id=uuid.uuid4(),
        session_id=session_id,
        operation_id=operation_id,
        cumulative_proven_ms=new_proven_ms,
        runner_id=runtime_principal.runner_id,
        budget_version=budget.version,
        pinned_rate=budget.pinned_rate,
        tariff_snapshot=session_locked.tariff_snapshot,
        session_version=session_locked.version,
        config_version_id=session_locked.agent_version_id,
        received_at=ts,
    )
    db.add(checkpoint)
    budget.cumulative_proven_ms = new_proven_ms

    # 8. Compute desired cumulative authorization
    capped_future_ms = min(requested_future_ms, billing_slice_ms)
    if new_proven_ms > MAX_BIGINT - capped_future_ms:
        raise BillingValidationError(
            "requested cumulative authorization exceeds the signed 64-bit range"
        )
    desired_ms = max(budget.cumulative_authorized_ms, new_proven_ms + capped_future_ms)

    available_wallet_minor = wallet.posted_minor - wallet.reserved_minor
    funds = budget.cumulative_charged_minor + remaining_budget_reservation + available_wallet_minor

    if budget.pinned_rate > 0:
        affordable_ms = (funds * 60_000) // budget.pinned_rate
    else:
        affordable_ms = desired_ms

    # No new authority may outlive the session's absolute hard deadline.
    limit_ms = _duration_ms(session_locked.created_at, session_locked.hard_end_deadline)
    if limit_ms is not None and new_proven_ms > limit_ms:
        raise InvalidTimeReportError(
            "Billable time exceeds the session hard-end deadline"
        )
    deadline_reached = (
        session_locked.hard_end_deadline is not None
        and ts >= session_locked.hard_end_deadline
    )
    if deadline_reached:
        # Settle proven time but issue no further authority once the deadline passes.
        authorized_ms = new_proven_ms
    else:
        authorized_ms = min(desired_ms, affordable_ms)
        if limit_ms is not None:
            authorized_ms = min(authorized_ms, limit_ms)
        authorized_ms = max(new_proven_ms, authorized_ms)
    if authorized_ms > MAX_BIGINT:
        raise BillingValidationError("authorized time exceeds the signed 64-bit range")

    # 9. Update reservations
    try:
        new_required_reservation = (
            charge_minor(budget.pinned_rate, authorized_ms)
            - charge_minor(budget.pinned_rate, new_proven_ms)
        )
    except ValueError as exc:
        raise BillingValidationError(str(exc)) from exc
    res_diff = new_required_reservation - remaining_budget_reservation

    new_wallet_reserved = wallet.reserved_minor + res_diff
    if new_wallet_reserved < 0 or new_wallet_reserved > MAX_BIGINT:
        raise BillingError("Reservation update would violate wallet bounds")
    budget.reserved_minor = new_required_reservation
    wallet.reserved_minor = new_wallet_reserved
    budget.cumulative_authorized_ms = authorized_ms

    funds_exhausted = (authorized_ms <= new_proven_ms)
    permit_id = uuid.uuid4()
    expires_at = ts + timedelta(milliseconds=permit_ttl_ms) if not funds_exhausted else ts
    if not funds_exhausted and session_locked.hard_end_deadline is not None:
        expires_at = min(expires_at, session_locked.hard_end_deadline)

    budget.permit_expiry = expires_at
    budget.version += 1
    wallet.version += 1
    budget.updated_at = ts
    wallet.updated_at = ts

    permit = BudgetPermit(
        session_id=session_id,
        budget_version=budget.version,
        proven_ms=budget.cumulative_proven_ms,
        authorized_through_ms=budget.cumulative_authorized_ms,
        reserved_minor=budget.reserved_minor,
        permit_id=permit_id,
        expires_at=expires_at,
        funds_exhausted=funds_exhausted,
    )

    op = BillingOperation(
        id=uuid.uuid4(),
        session_id=session_id,
        operation_id=operation_id,
        request_hash=req_hash,
        response_body=permit.model_dump(mode="json"),
        status_code=200,
        created_at=ts,
    )
    db.add(op)
    await db.commit()
    return permit

async def close_budget(
    db: AsyncSession,
    *,
    runtime_principal: RuntimePrincipal,
    session_id: uuid.UUID,
    operation_id: str,
    expected_budget_version: int,
    cumulative_billable_ms: int,
    stop_evidence: Any = None,
    now: datetime | None = None,
) -> BudgetView:
    """Settle final proven cumulative time and release all reservations atomically.

    Follows accounting-contract.md:
    - Lock order: wallet row first, then budget row.
    - Idempotent lost-close retries return recorded BudgetView.
    - Settle delta charge up to cumulative_billable_ms; release all remaining reservation.
    - Set budget status CLOSED.
    """
    ts = now or datetime.now(timezone.utc)
    _validate_operation_id(operation_id)
    _validate_bounded_int(
        "expected_budget_version", expected_budget_version, minimum=1
    )
    _validate_bounded_int("cumulative_billable_ms", cumulative_billable_ms)
    raw_evidence = _normalize_stop_evidence(stop_evidence)

    req_payload = {
        "action": "close_budget",
        "session_id": str(session_id),
        "operation_id": operation_id,
        "expected_budget_version": expected_budget_version,
        "cumulative_billable_ms": cumulative_billable_ms,
        "stop_evidence": raw_evidence,
    }
    req_hash = _canonical_hash(req_payload)

    # 1. Look up session first
    stmt_session = (
        select(Session)
        .where(Session.id == session_id)
        .execution_options(populate_existing=True)
    )
    session = (await db.execute(stmt_session)).scalar_one_or_none()
    if session is None:
        raise NotFoundError("Session not found")

    # 2. Authenticate/authorize assigned runner FIRST (fail closed if unassigned or mismatched)
    if not session.runner_id:
        raise ForbiddenError("Session has no assigned runner")
    if runtime_principal.runner_id != session.runner_id:
        raise ForbiddenError("Runner ID does not match assigned session runner")

    # 3. Advisory lock on session operation key to serialize concurrent replay
    op_key = f"{session_id}:{operation_id}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:op_key))"), {"op_key": op_key})

    # 4. Check operation key/hash AFTER runner authorization (prevents cross-runner disclosure)
    stmt_op = select(BillingOperation).where(
        BillingOperation.session_id == session_id,
        BillingOperation.operation_id == operation_id,
    )
    existing_op = (await db.execute(stmt_op)).scalar_one_or_none()
    if existing_op is not None and existing_op.request_hash != req_hash:
        raise IdempotencyConflictError("Conflicting payload under the same operation key")
    # 5. Lock wallet first, then budget
    wallet_owner_user_id = session.owner_user_id
    stmt_wallet = (
        select(WalletAccount)
        .where(WalletAccount.user_id == wallet_owner_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    wallet = (await db.execute(stmt_wallet)).scalar_one_or_none()
    if wallet is None:
        raise NotFoundError("Wallet account not found")

    stmt_budget = (
        select(CallBudget)
        .where(CallBudget.session_id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    budget = (await db.execute(stmt_budget)).scalar_one_or_none()
    if budget is None:
        raise ConflictError("Call budget not found for existing session")

    # Re-verify session runner under lock against concurrent reassignment
    stmt_session_locked = (
        select(Session)
        .where(Session.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    session_locked = (await db.execute(stmt_session_locked)).scalar_one_or_none()
    if session_locked is None or not session_locked.runner_id or session_locked.runner_id != runtime_principal.runner_id:
        raise ForbiddenError("Runner ID does not match assigned session runner")
    if session_locked.owner_user_id != wallet_owner_user_id:
        raise ConflictError("Session owner changed while billing rows were being locked")
    _ensure_budget_owner(budget, session_locked)

    # 6. RE-CHECK idempotency under lock (simultaneous request could have committed)
    existing_op_locked = (await db.execute(stmt_op)).scalar_one_or_none()
    if existing_op_locked is not None:
        if existing_op_locked.request_hash != req_hash:
            raise IdempotencyConflictError("Conflicting payload under the same operation key")
        return decode_stored_response(BudgetView, existing_op_locked.response_body)

    if budget.status == "CLOSED":
        if cumulative_billable_ms != budget.cumulative_proven_ms:
            raise ConflictError("Late charge after budget closure")
        return BudgetView(
            session_id=budget.session_id,
            owner_user_id=budget.owner_user_id,
            pinned_rate=budget.pinned_rate,
            proven_ms=budget.cumulative_proven_ms,
            authorized_through_ms=budget.cumulative_authorized_ms,
            charged_minor=budget.cumulative_charged_minor,
            reserved_minor=budget.reserved_minor,
            budget_version=budget.version,
            status="CLOSED",
            waived_unknown_usage=budget.waived_unknown_usage,
            stop_evidence=budget.stop_evidence,
            closed_at=budget.updated_at,
        )

    if budget.version != expected_budget_version:
        raise BudgetVersionMismatchError(
            f"Budget version mismatch: expected {expected_budget_version}, current {budget.version}"
        )

    if cumulative_billable_ms < budget.cumulative_proven_ms:
        raise InvalidTimeReportError("Billable time cannot decrease on close")
    if cumulative_billable_ms > budget.cumulative_authorized_ms:
        raise InvalidTimeReportError("Billable time exceeds authorized limit on close")

    # accounting-contract.md:67-71: Every close/expiry path needs confirmed stop OR expired permission.
    # Earlier-close trust seam:
    # Releasing reservations while an unexpired permit is live requires genuine authenticated
    # media stop attestation from the Task 6 runtime boundary. Because Task 6 stop attestation
    # is not yet implemented, caller-supplied stop_evidence is retained strictly as untrusted
    # record metadata, and close_budget fails closed on ALL live permits until permit expiry
    # (ts >= budget.permit_expiry). Client-supplied JSON or session state cannot stand in as
    # trustworthy proof to release live financial authority.
    if budget.permit_expiry is not None and ts < budget.permit_expiry:
        raise ConflictError(
            f"Cannot close active budget and release reservations while permit is still live (expires at {budget.permit_expiry}); "
            "genuine authenticated stop attestation is a Task 6 prerequisite"
        )

    budget.stop_evidence = raw_evidence
    # 4. Settle final proven cumulative time
    try:
        final_cost = charge_minor(budget.pinned_rate, cumulative_billable_ms)
    except ValueError as exc:
        raise BillingValidationError(str(exc)) from exc
    delta_charge = final_cost - budget.cumulative_charged_minor
    if delta_charge < 0:
        raise BillingError("Final charge would reduce a previously committed charge")
    if delta_charge > budget.reserved_minor:
        raise BillingError("Final charge exceeds the authorized reservation")
    if delta_charge > 0:
        wallet.posted_minor -= delta_charge
        budget.cumulative_charged_minor += delta_charge
        charge_entry = WalletEntry(
            id=uuid.uuid4(),
            user_id=wallet.user_id,
            kind="charge",
            amount_minor=-delta_charge,
            session_id=session_id,
            operation_id=_charge_entry_operation_id(session_id, operation_id),
            actor=f"runner:{runtime_principal.runner_id}",
            reason=f"Final charge for session {session_id} up to {cumulative_billable_ms}ms",
            created_at=ts,
        )
        db.add(charge_entry)

    # Release full remaining reservation
    new_wallet_reserved = wallet.reserved_minor - budget.reserved_minor
    if new_wallet_reserved < 0:
        raise BillingError("Reservation release would make the wallet reservation negative")
    wallet.reserved_minor = new_wallet_reserved
    budget.reserved_minor = 0
    budget.cumulative_proven_ms = cumulative_billable_ms
    budget.cumulative_authorized_ms = cumulative_billable_ms
    budget.status = "CLOSED"
    budget.version += 1
    wallet.version += 1
    budget.updated_at = ts
    wallet.updated_at = ts
    session_locked.state = "CLOSED"
    session_locked.ended_at = ts
    session_locked.version += 1
    # Write checkpoint
    checkpoint = MeterCheckpoint(
        id=uuid.uuid4(),
        session_id=session_id,
        operation_id=operation_id,
        cumulative_proven_ms=cumulative_billable_ms,
        runner_id=runtime_principal.runner_id,
        budget_version=budget.version,
        pinned_rate=budget.pinned_rate,
        tariff_snapshot=session_locked.tariff_snapshot,
        session_version=session_locked.version,
        config_version_id=session_locked.agent_version_id,
        received_at=ts,
    )
    db.add(checkpoint)

    view = BudgetView(
        session_id=budget.session_id,
        owner_user_id=budget.owner_user_id,
        pinned_rate=budget.pinned_rate,
        proven_ms=budget.cumulative_proven_ms,
        authorized_through_ms=budget.cumulative_authorized_ms,
        charged_minor=budget.cumulative_charged_minor,
        reserved_minor=budget.reserved_minor,
        budget_version=budget.version,
        status="CLOSED",
        waived_unknown_usage=False,
        stop_evidence=budget.stop_evidence,
        closed_at=ts,
    )

    op = BillingOperation(
        id=uuid.uuid4(),
        session_id=session_id,
        operation_id=operation_id,
        request_hash=req_hash,
        response_body=view.model_dump(mode="json"),
        status_code=200,
        created_at=ts,
    )
    db.add(op)
    await db.commit()
    return view


async def expire_stopped_budget(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    operation_id: str,
    now: datetime,
) -> BudgetView:
    """Recover and close an abandoned session budget after permit expiry.

    Follows accounting-contract.md:
    - Waits until permit is expired (now >= permit_expiry).
    - Settles only checkpoints already durably committed.
    - Releases remaining reservation.
    - Unproven elapsed time is waived (waived_unknown_usage=True).
    - Session marked terminal.
    """
    _validate_operation_id(operation_id)
    ts = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    req_payload = {
        "action": "expire_stopped_budget",
        "session_id": str(session_id),
        "operation_id": operation_id,
    }
    req_hash = _canonical_hash(req_payload)

    # 1. Resolve idempotency, but defer a matching replay until the current
    # session and budget ownership have been validated under row locks.
    stmt_op = select(BillingOperation).where(
        BillingOperation.session_id == session_id,
        BillingOperation.operation_id == operation_id,
    )
    existing_op = (await db.execute(stmt_op)).scalar_one_or_none()
    if existing_op is not None and existing_op.request_hash != req_hash:
        raise IdempotencyConflictError("Conflicting payload under the same operation key")

    # 2. Derive session
    stmt_session = (
        select(Session)
        .where(Session.id == session_id)
        .execution_options(populate_existing=True)
    )
    session = (await db.execute(stmt_session)).scalar_one_or_none()
    if session is None:
        raise NotFoundError("Session not found")

    # 3. Advisory lock on session operation key to serialize concurrent replay
    op_key = f"{session_id}:{operation_id}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:op_key))"), {"op_key": op_key})

    wallet_owner_user_id = session.owner_user_id
    stmt_wallet = (
        select(WalletAccount)
        .where(WalletAccount.user_id == wallet_owner_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    wallet = (await db.execute(stmt_wallet)).scalar_one_or_none()
    if wallet is None:
        raise NotFoundError("Wallet account not found")

    stmt_budget = (
        select(CallBudget)
        .where(CallBudget.session_id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    budget = (await db.execute(stmt_budget)).scalar_one_or_none()
    if budget is None:
        raise ConflictError("Call budget not found for existing session")

    stmt_session_locked = (
        select(Session)
        .where(Session.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    session_locked = (await db.execute(stmt_session_locked)).scalar_one_or_none()
    if session_locked is None:
        raise NotFoundError("Session not found")
    if session_locked.owner_user_id != wallet_owner_user_id:
        raise ConflictError("Session owner changed while billing rows were being locked")
    _ensure_budget_owner(budget, session_locked)
    # 5. RE-CHECK idempotency under lock (simultaneous request could have committed)
    existing_op_locked = (await db.execute(stmt_op)).scalar_one_or_none()
    if existing_op_locked is not None:
        if existing_op_locked.request_hash != req_hash:
            raise IdempotencyConflictError("Conflicting payload under the same operation key")
        return decode_stored_response(BudgetView, existing_op_locked.response_body)

    if budget.status == "CLOSED":
        return BudgetView(
            session_id=budget.session_id,
            owner_user_id=budget.owner_user_id,
            pinned_rate=budget.pinned_rate,
            proven_ms=budget.cumulative_proven_ms,
            authorized_through_ms=budget.cumulative_authorized_ms,
            charged_minor=budget.cumulative_charged_minor,
            reserved_minor=budget.reserved_minor,
            budget_version=budget.version,
            status="CLOSED",
            waived_unknown_usage=budget.waived_unknown_usage,
            stop_evidence=budget.stop_evidence,
            closed_at=budget.updated_at,
        )

    # 4. Require permit expiry before write-off
    if budget.permit_expiry is not None and ts < budget.permit_expiry:
        raise ConflictError(
            f"Permit has not expired yet; cannot write off budget before permit expiry ({budget.permit_expiry})"
        )

    # Release remaining reservation without charging unproven time
    new_wallet_reserved = wallet.reserved_minor - budget.reserved_minor
    if new_wallet_reserved < 0:
        raise BillingError("Reservation release would make the wallet reservation negative")
    wallet.reserved_minor = new_wallet_reserved
    budget.reserved_minor = 0
    budget.cumulative_authorized_ms = budget.cumulative_proven_ms
    budget.status = "CLOSED"
    budget.waived_unknown_usage = True
    raw_evidence = {"reason": "runner_lost_permit_expired"}
    budget.stop_evidence = raw_evidence
    budget.version += 1
    wallet.version += 1
    budget.updated_at = ts
    wallet.updated_at = ts
    # Mark session terminal
    session_locked.state = "EXPIRED"
    session_locked.ended_at = ts
    session_locked.failure_code = "RUNNER_LOST"
    session_locked.version += 1
    view = BudgetView(
        session_id=budget.session_id,
        owner_user_id=budget.owner_user_id,
        pinned_rate=budget.pinned_rate,
        proven_ms=budget.cumulative_proven_ms,
        authorized_through_ms=budget.cumulative_authorized_ms,
        charged_minor=budget.cumulative_charged_minor,
        reserved_minor=0,
        budget_version=budget.version,
        status="CLOSED",
        waived_unknown_usage=True,
        stop_evidence=raw_evidence,
        closed_at=ts,
    )

    op = BillingOperation(
        id=uuid.uuid4(),
        session_id=session_id,
        operation_id=operation_id,
        request_hash=req_hash,
        response_body=view.model_dump(mode="json"),
        status_code=200,
        created_at=ts,
    )
    db.add(op)
    await db.commit()
    return view


async def create_call_budget(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    operation_id: str,
    pinned_rate_minor_per_minute: int,
    now: datetime | None = None,
) -> BudgetView:
    """Initialize a fresh call budget for a new session with durable operation tracking.

    Follows accounting-contract.md:
    - Returns a BudgetView representing the initialized budget.
    - Owner identity is derived strictly from the stored session record, never from caller input.
    - Bounded input validation for operation_id and pinned_rate_minor_per_minute.
    - Serialized under transaction advisory lock on (session_id, operation_id).
    - Idempotent replay returns the original recorded BudgetView, reconstructed from
      BillingOperation.response_body, if locked session and budget ownership still match,
      even if the underlying budget has advanced.
    - Conflicting request payload under the same operation key raises IdempotencyConflictError.
    - A second different operation attempting to create a budget on the same session raises ConflictError.
    """
    _validate_operation_id(operation_id)
    _validate_bounded_int(
        "pinned_rate_minor_per_minute", pinned_rate_minor_per_minute
    )
    ts = now or datetime.now(timezone.utc)

    req_payload = {
        "session_id": str(session_id),
        "operation_id": operation_id,
        # Keep the stored hash key stable across the Python parameter rename.
        "pinned_rate": pinned_rate_minor_per_minute,
    }
    req_hash = _canonical_hash(req_payload)

    # 1. Check for a prior operation, but defer replay until the current session
    # owner and budget have been revalidated under the accounting row locks.
    stmt_op = select(BillingOperation).where(
        BillingOperation.session_id == session_id,
        BillingOperation.operation_id == operation_id,
    )
    existing_op = (await db.execute(stmt_op)).scalar_one_or_none()
    if existing_op is not None and existing_op.request_hash != req_hash:
        raise IdempotencyConflictError("Conflicting payload under the same operation key")

    # 2. Derive owner identity strictly from the stored session
    stmt_session = (
        select(Session)
        .where(Session.id == session_id)
        .execution_options(populate_existing=True)
    )
    session = (await db.execute(stmt_session)).scalar_one_or_none()
    if session is None:
        raise NotFoundError("Session not found")

    # 3. Advisory lock on session operation key to serialize concurrent replay
    op_key = f"{session_id}:{operation_id}"
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:op_key))"), {"op_key": op_key})

    # 4. Re-check the request hash after acquiring the operation advisory lock.
    existing_op_locked = (await db.execute(stmt_op)).scalar_one_or_none()
    if existing_op_locked is not None and existing_op_locked.request_hash != req_hash:
        raise IdempotencyConflictError("Conflicting payload under the same operation key")

    # 5. Lock owner wallet, budget, and session in the shared accounting order.
    stmt_wallet = (
        select(WalletAccount)
        .where(WalletAccount.user_id == session.owner_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (await db.execute(stmt_wallet)).scalar_one_or_none() is None:
        raise NotFoundError("Wallet account not found")

    stmt_budget_check = (
        select(CallBudget)
        .where(CallBudget.session_id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    budget_check = (await db.execute(stmt_budget_check)).scalar_one_or_none()

    stmt_session_locked = (
        select(Session)
        .where(Session.id == session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    session_locked = (await db.execute(stmt_session_locked)).scalar_one_or_none()
    if session_locked is None:
        raise NotFoundError("Session not found")
    if session_locked.owner_user_id != session.owner_user_id:
        raise ConflictError("Session owner changed during budget creation")
    # 6. Return an idempotent result only after row-locked owner validation.
    existing_op_locked = (await db.execute(stmt_op)).scalar_one_or_none()
    if existing_op_locked is not None:
        if existing_op_locked.request_hash != req_hash:
            raise IdempotencyConflictError("Conflicting payload under the same operation key")
        if (
            budget_check is None
            or budget_check.owner_user_id != session_locked.owner_user_id
        ):
            raise ConflictError("Stored call budget owner does not match session owner")
        return decode_stored_response(BudgetView, existing_op_locked.response_body)
    if budget_check is not None:
        raise ConflictError("Call budget already exists for this session under another operation")

    # 7. Create the new budget only after locks and owner validation.

    budget = CallBudget(
        session_id=session_id,
        owner_user_id=session_locked.owner_user_id,
        pinned_rate=pinned_rate_minor_per_minute,
        cumulative_proven_ms=0,
        cumulative_authorized_ms=0,
        cumulative_charged_minor=0,
        reserved_minor=0,
        permit_expiry=None,
        version=1,
        status="OPEN",
        waived_unknown_usage=False,
        stop_evidence=None,
        created_at=ts,
        updated_at=ts,
    )
    db.add(budget)

    view = BudgetView(
        session_id=budget.session_id,
        owner_user_id=budget.owner_user_id,
        pinned_rate=budget.pinned_rate,
        proven_ms=0,
        authorized_through_ms=0,
        charged_minor=0,
        reserved_minor=0,
        budget_version=1,
        status="OPEN",
        waived_unknown_usage=False,
        stop_evidence=None,
        closed_at=None,
    )

    # Record billing operation atomically with full serialized BudgetView
    op = BillingOperation(
        id=uuid.uuid4(),
        session_id=session_id,
        operation_id=operation_id,
        request_hash=req_hash,
        response_body=view.model_dump(mode="json"),
        status_code=200,
        created_at=ts,
    )
    db.add(op)
    await db.commit()
    return view

async def get_wallet_view(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
) -> WalletView:
    """Fetch user's current wallet balance view."""
    stmt = select(WalletAccount).where(WalletAccount.user_id == user_id)
    wallet = (await db.execute(stmt)).scalar_one_or_none()
    if wallet is None:
        raise NotFoundError("Wallet account not found")

    return WalletView(
        user_id=wallet.user_id,
        currency=wallet.currency,  # type: ignore[arg-type]
        posted_minor=wallet.posted_minor,
        reserved_minor=wallet.reserved_minor,
        available_minor=wallet.posted_minor - wallet.reserved_minor,
        version=wallet.version,
        updated_at=wallet.updated_at,
    )


async def get_wallet_entries(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> WalletEntriesPage:
    """Fetch user's wallet ledger entries with stable cursor pagination."""
    query = (
        select(WalletEntry)
        .where(WalletEntry.user_id == user_id)
        .order_by(WalletEntry.created_at.desc(), WalletEntry.id.desc())
        .limit(limit + 1)
    )

    if cursor:
        try:
            c_time, c_id = _decode_cursor(cursor)
            query = query.where(
                (WalletEntry.created_at < c_time)
                | ((WalletEntry.created_at == c_time) & (WalletEntry.id < c_id))
            )
        except Exception as e:
            raise BillingValidationError("Invalid pagination cursor") from e

    res = await db.execute(query)
    rows = list(res.scalars().all())

    next_cursor = None
    if len(rows) > limit:
        # The cursor points at the last row actually returned; the next page
        # resumes strictly after it, so no row is skipped or repeated.
        last_item = rows[limit - 1]
        next_cursor = _encode_cursor(last_item.created_at, last_item.id)
        rows = rows[:limit]

    items = [
        WalletEntryView(
            id=r.id,
            user_id=r.user_id,
            kind=r.kind,  # type: ignore[arg-type]
            amount_minor=r.amount_minor,
            session_id=r.session_id,
            operation_id=r.operation_id,
            actor=r.actor,
            reason=r.reason,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return WalletEntriesPage(items=items, next_cursor=next_cursor)
