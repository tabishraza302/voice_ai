"""Strict wire models for bounded, owner-scoped Flow QA jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ScenarioKind = Literal[
    "branch",
    "refusal",
    "clarification_exit",
    "correction",
    "out_of_order",
    "multi_answer",
    "partial_info",
    "dependency_invalidation",
    "detour",
    "contradiction",
    "missing_business_fact",
    "early_termination",
    "prompt_injection",
]
ScenarioStatus = Literal["PASS", "FAIL", "INCONCLUSIVE", "ERROR"]
QAJobStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "EXPIRED"]


class ExpectedEdge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    from_step_id: str = Field(min_length=1, max_length=64)
    transition_index: int = Field(ge=0, le=31)
    to_step_id: str = Field(min_length=1, max_length=64)
    when: dict[str, Any]


class QAScenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario_id: str = Field(min_length=1, max_length=80)
    case_key: str = Field(min_length=1, max_length=256)
    kind: ScenarioKind
    expected_edge: ExpectedEdge | None = None
    target_step_id: str | None = Field(default=None, max_length=64)
    target_fields: list[str] = Field(default_factory=list, max_length=32)
    task: str = Field(min_length=1, max_length=2048)


class QAJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: uuid.UUID
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_count: int = Field(default=8, ge=1, le=64)
    scenario_ids: list[str] | None = Field(default=None, min_length=1, max_length=64)
    max_turns: int = Field(default=12, ge=1, le=50)
    max_tokens: int = Field(default=2048, ge=64, le=16384)
    max_runtime_seconds: int = Field(default=120, ge=1, le=300)
    utterance_model_id: str = Field(min_length=1, max_length=128)
    judge_model_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("scenario_ids")
    @classmethod
    def unique_scenario_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("scenario_ids must be unique")
        return value

    @model_validator(mode="after")
    def one_scenario_selection(self) -> QAJobCreate:
        if self.scenario_ids is not None and len(self.scenario_ids) > self.scenario_count:
            raise ValueError("scenario_ids cannot exceed scenario_count")
        return self


class QAJobQueued(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    status: Literal["QUEUED"]




class QASuggestionApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)


class QASuggestionDismissed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestion_id: str
    status: Literal["DISMISSED"]
class QAJobCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    status: QAJobStatus


class QACatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenarios: list[QAScenario]
    scenario_count: int
    omitted_scenario_count: int
    flow_hash: str


class QAJobView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    agent_id: uuid.UUID
    snapshot_kind: Literal["draft", "published"]
    snapshot_hash: str
    status: QAJobStatus
    config_hash: str
    rubric_version: Literal["flow-qa-rubric/1"]
    models: dict[str, str | None]
    scenarios: list[dict[str, Any]]
    results: list[dict[str, Any]]
    suggestions: list[dict[str, Any]]
    coverage: dict[str, Any]
    budget: dict[str, int]
    usage: dict[str, int | None] | None
    failure_code: str | None
    runtime_seconds: float | None
    artifacts: list[dict[str, Any]]
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class QASuggestionApplied(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestion_id: str
    draft_revision: int
    authoring_hash: str
    applied: Literal[True]
