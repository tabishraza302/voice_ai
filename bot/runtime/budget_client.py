"""Permit checks and explicit checkpoint/close calls for verified usage only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from platform_api.billing.schemas import (
    BudgetPermit,
    BudgetView,
    CheckpointRequest,
    CloseBudgetRequest,
    RuntimeStopAttestation,
)
from platform_api.sessions.schemas import RuntimeBudgetSnapshot

from bot.runtime.transport_auth import PlatformRuntimeClient


class BudgetPrerequisiteError(RuntimeError):
    """A session does not have a live funded permit or verified usage source."""


class VerifiedUsageMeter(Protocol):
    """A meter backed only by trusted, fragment-correlated render receipts."""

    async def cumulative_billable_ms(self) -> int: ...


@dataclass(frozen=True)
class BudgetPermitWindow:
    """Detached permit identity and wall-clock stops; not a usage counter."""

    session_id: UUID
    budget_version: int
    permit_id: UUID
    proven_ms: int
    authorized_through_ms: int
    reserved_minor: int
    expires_at: datetime
    hard_end_deadline: datetime


def validate_initial_permit(
    budget: RuntimeBudgetSnapshot,
    *,
    session_id: UUID,
    hard_end_deadline: datetime,
    now: datetime | None = None,
) -> BudgetPermitWindow:
    """Validate a live first permit without treating wall time as billable usage."""
    instant = now or datetime.now(timezone.utc)
    permit = budget.permit
    if (
        instant.tzinfo is None
        or hard_end_deadline.tzinfo is None
        or permit.expires_at.tzinfo is None
    ):
        raise BudgetPrerequisiteError("Budget and session deadlines must be timezone-aware")
    if (
        permit.session_id != session_id
        or permit.budget_version != budget.budget_version
        or permit.authorized_through_ms != budget.authorized_through_ms
        or permit.reserved_minor != budget.reserved_minor
        or permit.funds_exhausted
        or permit.authorized_through_ms <= permit.proven_ms
        or permit.expires_at <= instant
        or hard_end_deadline <= instant
    ):
        raise BudgetPrerequisiteError("Session does not have a live funded permit")
    return BudgetPermitWindow(
        session_id=permit.session_id,
        budget_version=permit.budget_version,
        permit_id=permit.permit_id,
        proven_ms=permit.proven_ms,
        authorized_through_ms=permit.authorized_through_ms,
        reserved_minor=permit.reserved_minor,
        expires_at=permit.expires_at,
        hard_end_deadline=hard_end_deadline,
    )


class RuntimeBudgetClient:
    """Send only cumulative usage supplied by a trusted playback meter."""

    def __init__(self, platform: PlatformRuntimeClient) -> None:
        self._platform = platform

    async def checkpoint(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        expected_budget_version: int,
        cumulative_billable_ms: int,
        requested_future_ms: int,
    ) -> BudgetPermit:
        _validate_usage(cumulative_billable_ms)
        if type(expected_budget_version) is not int or expected_budget_version < 1:
            raise BudgetPrerequisiteError("Budget version must be a positive integer")
        if type(requested_future_ms) is not int or requested_future_ms < 0:
            raise BudgetPrerequisiteError("Requested permit duration must be a nonnegative integer")
        request = CheckpointRequest(
            operation_id=operation_id,
            expected_budget_version=expected_budget_version,
            cumulative_billable_ms=cumulative_billable_ms,
            requested_future_ms=requested_future_ms,
        )
        permit = await self._platform.checkpoint_budget(str(session_id), request)
        if (
            permit.session_id != session_id
            or permit.budget_version != expected_budget_version + 1
            or permit.proven_ms != cumulative_billable_ms
        ):
            raise BudgetPrerequisiteError("Platform returned a stale or misbound budget permit")
        return permit

    async def close(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        expected_budget_version: int,
        cumulative_billable_ms: int,
        stop_attestation: RuntimeStopAttestation,
    ) -> BudgetView:
        _validate_usage(cumulative_billable_ms)
        if stop_attestation.session_id != session_id:
            raise BudgetPrerequisiteError("Stop attestation belongs to a different session")
        if stop_attestation.source != "voice-runtime":
            raise BudgetPrerequisiteError("Runtime close requires a voice-runtime stop attestation")
        if stop_attestation.runner_id != self._platform.credentials.runner_id:
            raise BudgetPrerequisiteError("Stop attestation belongs to a different runner")
        request = CloseBudgetRequest(
            operation_id=operation_id,
            expected_budget_version=expected_budget_version,
            cumulative_billable_ms=cumulative_billable_ms,
            stop_attestation=stop_attestation,
        )
        budget = await self._platform.close_budget(str(session_id), request)
        if (
            budget.session_id != session_id
            or budget.status != "CLOSED"
            or budget.budget_version != expected_budget_version + 1
            or budget.proven_ms != cumulative_billable_ms
        ):
            raise BudgetPrerequisiteError("Platform did not confirm the expected budget closure")
        return budget


def _validate_usage(value: int) -> None:
    if type(value) is not int or value < 0:
        raise BudgetPrerequisiteError(
            "Cumulative billable usage must be a verified nonnegative integer"
        )


__all__ = [
    "BudgetPermitWindow",
    "BudgetPrerequisiteError",
    "RuntimeBudgetClient",
    "VerifiedUsageMeter",
    "validate_initial_permit",
]
