"""Strict request and response contracts for durable text-only chat tests."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bot.flow.schemas import CaptureUpdate, SessionState


class ChatTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    test_id: uuid.UUID
    expected_turn_sequence: int = Field(..., ge=1)
    text: str = Field(..., min_length=1, max_length=8192)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class ChatModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(..., min_length=1, max_length=8192)
    updates: list[CaptureUpdate] = Field(default_factory=list, max_length=32)

    @field_validator("reply")
    @classmethod
    def reject_blank_reply(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reply must not be blank")
        return value


class ChatTraceDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    path: str
    severity: str
    message: str | None = None


class ChatTraceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_sequence: int
    event_type: str
    step_id: str | None
    state_version: int
    outcome: Literal["in_progress", "completed", "partial", "aborted", "failed"]
    diagnostics: list[ChatTraceDiagnostic] = Field(default_factory=list)


class ChatTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_id: uuid.UUID
    turn_sequence: int
    reply: str
    flow_state: SessionState
    trace: list[ChatTraceItem]
    outcome: Literal["in_progress", "completed", "partial", "aborted", "failed"]


class ChatMessageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    turn_sequence: int
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime


class ChatRecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_id: uuid.UUID
    snapshot_hash: str
    turn_sequence: int
    pending_turn_sequence: int | None
    messages: list[ChatMessageView]
    flow_state: SessionState
    trace: list[ChatTraceItem]
    outcome: Literal["in_progress", "completed", "partial", "aborted", "failed"]
