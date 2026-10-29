"""Strict HTTP contracts for public deployments and anonymous guest sessions."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PublicDeploymentView(StrictModel):
    public_id: str
    enabled: bool
    created_at: datetime


class PublicDeploymentBootstrap(StrictModel):
    public_id: str
    agent_name: str
    agent_description: str
    disclosure_version: str
    disclosure_text: str
    csrf_token: str


class GuestAdmissionRequest(StrictModel):
    disclosure_version: str = Field(min_length=1, max_length=64)
    disclosure_acknowledged: bool


class GuestAdmissionView(StrictModel):
    session_id: uuid.UUID
    state: str
    signaling_url: str
    join_token: str
    join_expires_at: datetime
    hard_end_deadline: datetime
    csrf_token: str


class GuestReconnectRequest(StrictModel):
    pass


class GuestReconnectView(StrictModel):
    session_id: uuid.UUID
    state: str
    signaling_url: str
    join_token: str
    expires_at: datetime


class GuestSessionStatus(StrictModel):
    session_id: uuid.UUID
    state: str
    session_version: int = Field(ge=1)
    created_at: datetime
    hard_end_deadline: datetime
    ended_at: datetime | None


class GuestEndView(StrictModel):
    session_id: uuid.UUID
    state: str
    session_version: int = Field(ge=1)
    ended_at: datetime | None
    failure_code: str | None


__all__ = [
    "GuestAdmissionRequest",
    "GuestAdmissionView",
    "GuestEndView",
    "GuestReconnectRequest",
    "GuestReconnectView",
    "GuestSessionStatus",
    "PublicDeploymentBootstrap",
    "PublicDeploymentView",
]
