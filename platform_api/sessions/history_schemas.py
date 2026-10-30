"""Strict response schemas for owner conversation history and usage."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionHistoryItem(StrictModel):
    session_id: uuid.UUID
    agent_version_id: uuid.UUID | None
    kind: Literal["voice", "chat", "voice_test"]
    state: str
    session_version: int
    connect_deadline: datetime | None
    hard_end_deadline: datetime | None
    created_at: datetime
    ended_at: datetime | None
    failure_code: str | None


class OwnerSessionsPage(StrictModel):
    items: list[SessionHistoryItem]
    next_cursor: str | None


class SessionBillingView(StrictModel):
    """Budget facts; estimate is only the current authorization ceiling."""

    budget_status: Literal["OPEN", "CLOSED"]
    charge_status: Literal["provisional", "final"]
    pinned_rate_minor_per_minute: int
    billed_ms: int
    authorized_ms: int
    authorized_estimate_minor: int | None
    charged_minor: int
    reserved_minor: int
    budget_version: int
    waived_unknown_usage: bool


class OwnerUsageItem(StrictModel):
    session: SessionHistoryItem
    billing: SessionBillingView | None


class OwnerUsagePage(StrictModel):
    items: list[OwnerUsageItem]
    next_cursor: str | None


class ConfigSnapshotView(StrictModel):
    session_id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str
    agent_version_id: uuid.UUID
    agent_version: int
    flow_hash: str
    artifact_hash: str
    authoring_hash: str
    immutable_config: dict[str, Any]
    tariff_snapshot: dict[str, Any] | None
    published_at: datetime


class TranscriptSegmentView(StrictModel):
    sequence: int
    event_id: uuid.UUID
    speaker: str
    final_text: str
    start_ms: int
    end_ms: int
    kind: str
    corrected: bool

class TranscriptGapView(StrictModel):
    after_sequence: int
    before_sequence: int
    reason: str


class TranscriptPageView(StrictModel):
    session_id: uuid.UUID
    transcript_revision: int
    policy_revision: int
    visible_to_owner: bool
    segments: list[TranscriptSegmentView]
    has_gaps: bool
    gaps: list[TranscriptGapView]
    processing: bool
    next_cursor: str | None
