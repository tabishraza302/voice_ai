"""Strict wire contracts for owner admission and the local runtime boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator

from platform_api.billing.schemas import BudgetPermit


class StrictModel(BaseModel):
    """Reject unknown fields and Python-side type coercion at API boundaries."""

    model_config = ConfigDict(extra="forbid", strict=True)


class SessionAdmissionRequest(StrictModel):
    """Request one owner-only voice-test session for an immutable agent version."""

    agent_version_id: uuid.UUID


class JoinDetails(StrictModel):
    """The one-time browser credential returned after registration and funding."""

    session_id: uuid.UUID
    runner_id: str
    config_version_id: uuid.UUID
    signaling_url: str
    join_token: str
    expires_at: datetime


class RegisteredSession(StrictModel):
    """Owner-safe admission result; neither public nor real-paid mode is enabled."""

    session_id: uuid.UUID
    owner_user_id: uuid.UUID
    participant_id: uuid.UUID
    config_version_id: uuid.UUID
    kind: Literal["voice_test"]
    state: Literal["REGISTERED"]
    session_version: int = Field(ge=1)
    runner_id: str
    connect_deadline: datetime
    hard_end_deadline: datetime
    join_details: JoinDetails
    public: Literal[False] = False
    paid: Literal[False] = False


class OwnerSessionView(StrictModel):
    """Safe owner-scoped view of one voice-test session."""

    session_id: uuid.UUID
    agent_version_id: uuid.UUID
    kind: Literal["voice_test"]
    state: str
    session_version: int = Field(ge=1)
    connect_deadline: datetime
    hard_end_deadline: datetime
    created_at: datetime
    ended_at: datetime | None
    failure_code: str | None



class RuntimeRegistrationRequest(StrictModel):
    """Idempotent command to reserve the exact session on its configured runner."""

    session_id: uuid.UUID
    runner_id: str
    registration_id: uuid.UUID
    config_version_id: uuid.UUID
    session_version: int = Field(ge=1)
    connect_deadline: datetime
    hard_end_deadline: datetime


class RuntimeReservation(StrictModel):
    """Runner acknowledgement that one session slot is reserved."""

    session_id: uuid.UUID
    runner_id: str
    registration_id: uuid.UUID
    expires_at: datetime


class RuntimeReleaseRequest(StrictModel):
    """Idempotent command to stop one runner session at an exact session version."""

    session_id: uuid.UUID
    runner_id: str
    registration_id: uuid.UUID
    session_version: int = Field(ge=1)


class RuntimeReleaseAcknowledgement(StrictModel):
    """Runner-confirmed release; no capacity is freed without this response."""

    session_id: uuid.UUID
    runner_id: str
    registration_id: uuid.UUID
    session_version: int = Field(ge=1)
    released: Literal[True]

class RuntimeBudgetSnapshot(StrictModel):
    """Current open budget facts needed to enforce the first funded permit."""

    pinned_rate_minor_per_minute: int = Field(ge=0)
    budget_version: int = Field(ge=1)
    reserved_minor: int = Field(ge=0)
    authorized_through_ms: int = Field(ge=0)
    permit: BudgetPermit


class RuntimeConfigView(StrictModel):
    """Immutable registered-session snapshot; no mutable agent/default fallback."""

    session_id: uuid.UUID
    owner_user_id: uuid.UUID
    participant_id: uuid.UUID
    auth_session_id: uuid.UUID
    origin: str
    runner_id: str
    registration_id: uuid.UUID
    config_version_id: uuid.UUID
    session_version: int = Field(ge=1)
    state: str
    connect_deadline: datetime
    hard_end_deadline: datetime
    tariff_snapshot: dict[str, Any]
    budget: RuntimeBudgetSnapshot
    immutable_config: dict[str, Any]
    authoring_hash: str
    flow_hash: str
    artifact_hash: str
    model_hash: str
    voice_hash: str
    settings_hash: str


class JoinConsumptionRequest(StrictModel):
    """Runtime-reported browser handshake to atomically consume a join JTI."""

    join_token: str = Field(min_length=1, max_length=8192)
    peer_id: uuid.UUID
    origin: str = Field(min_length=1, max_length=512)


class JoinConsumptionView(StrictModel):
    """Accepted single peer, bound to the registered runner and session."""

    session_id: uuid.UUID
    peer_id: uuid.UUID
    runner_id: str
    session_version: int = Field(ge=1)
    state: Literal["CONNECTING", "RECONNECTING"]
    reconnect: bool


class PeerReleaseRequest(StrictModel):
    """Runtime report that the assigned peer is gone; supports bounded reconnect."""

    peer_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=64)


class PeerReleaseView(StrictModel):
    session_id: uuid.UUID
    peer_id: uuid.UUID
    released: Literal[True]
    reconnect_until: datetime | None


class SessionTransitionRequest(StrictModel):
    """Compare-and-swap a runtime-owned state transition."""

    registration_id: uuid.UUID
    expected_version: int = Field(ge=1)
    target_state: Literal[
        "INITIALIZING",
        "READY",
        "ACTIVE",
        "RECONNECTING",
        "ENDING",
        "COMPLETED",
        "FAILED",
        "EXPIRED",
    ]
    failure_code: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_failure_code(self) -> "SessionTransitionRequest":
        if self.target_state in {"FAILED", "EXPIRED"} and not self.failure_code:
            raise ValueError("FAILED and EXPIRED transitions require failure_code")
        if (
            self.failure_code is not None
            and self.target_state not in {"ENDING", "FAILED", "EXPIRED"}
        ):
            raise ValueError(
                "failure_code is only valid for ending or terminal failure states"
            )
        return self


class SessionStateView(StrictModel):
    session_id: uuid.UUID
    state: str
    session_version: int = Field(ge=1)
    ended_at: datetime | None
    failure_code: str | None


__all__ = [
    "JoinConsumptionRequest",
    "JoinConsumptionView",
    "JoinDetails",
    "PeerReleaseRequest",
    "PeerReleaseView",
    "RegisteredSession",
    "RuntimeBudgetSnapshot",
    "RuntimeConfigView",
    "RuntimeRegistrationRequest",
    "RuntimeReleaseAcknowledgement",
    "RuntimeReleaseRequest",
    "RuntimeReservation",
    "SessionAdmissionRequest",
    "SessionStateView",
    "SessionTransitionRequest",
    "OwnerSessionView",
]
