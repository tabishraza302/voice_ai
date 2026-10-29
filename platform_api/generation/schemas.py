"""Typed owner API contracts for configured model generation jobs."""

from __future__ import annotations

from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from platform_api.agents.schemas import DiagnosticView
from platform_api.catalog import CatalogModelItem


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement: str = Field(..., min_length=1, max_length=8192)
    generation_model_id: str = Field(..., min_length=1, max_length=128)
    language: Literal["en", "hi"] = "en"
    token_budget: int = Field(..., ge=64, le=4096)

    @field_validator("requirement", "generation_model_id")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("value must not be blank")
        return trimmed


class AgentProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., max_length=1024)
    persona_prompt: str = Field(..., min_length=1, max_length=20000)
    config: dict[str, Any]

    @field_validator("name", "persona_prompt")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("value must not be blank")
        return trimmed


class GenerationModelOutput(AgentProposal):
    model_config = ConfigDict(extra="forbid")

    unknown_business_facts: list[str] = Field(..., max_length=100)

    @field_validator("unknown_business_facts")
    @classmethod
    def strip_unknown_facts(cls, facts: list[str]) -> list[str]:
        normalized = [fact.strip() for fact in facts]
        if any(not fact or len(fact) > 512 for fact in normalized):
            raise ValueError("unknown business fact entries must contain 1 to 512 characters")
        return normalized


class GenerationModelsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CatalogModelItem]


class GenerationQueuedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    status: Literal["QUEUED"]


class GenerationUsageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


class GenerationJobView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "QUEUED",
        "RUNNING",
        "CANCELLING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "EXPIRED",
    ]
    proposal: AgentProposal | None
    unknown_business_facts: list[str]
    validation_warnings: list[DiagnosticView]
    usage: GenerationUsageView | None


class GenerationCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerationCancelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    status: Literal[
        "QUEUED",
        "RUNNING",
        "CANCELLING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "EXPIRED",
    ]
