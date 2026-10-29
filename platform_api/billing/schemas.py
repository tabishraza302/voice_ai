"""Pydantic wire models, request schemas, and views for billing.

Follows accounting-contract.md:
- Requests and responses strictly forbid unknown fields (extra='forbid').
- Integer amounts and milliseconds; no floats or type coercion.
- Exact names matching accounting contract and verification plan.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator
MAX_BIGINT = (1 << 63) - 1
MIN_BIGINT = -(1 << 63)


class StrictModel(BaseModel):
    """Base model that strictly forbids unexpected fields and type coercion."""

    model_config = ConfigDict(extra="forbid", strict=True)


def decode_stored_response(model_cls: type[Any], data: dict[str, Any] | str) -> Any:
    """Decode stored JSON response body using JSON-mode validation.

    Parses ISO datetimes and UUID strings safely without relaxing strict request validation.
    """
    if isinstance(data, str):
        return model_cls.model_validate_json(data)
    return model_cls.model_validate_json(json.dumps(data, default=str))
# --- Output Views -------------------------------------------------------------


class WalletView(StrictModel):
    """Current posted, reserved, and available wallet balance."""

    user_id: uuid.UUID
    currency: Literal["INR"] = "INR"
    posted_minor: int
    reserved_minor: int
    available_minor: int
    version: int
    updated_at: datetime


class WalletEntryView(StrictModel):
    """Single ledger entry on a wallet."""

    id: uuid.UUID
    user_id: uuid.UUID
    kind: Literal["credit", "charge", "refund", "adjustment"]
    amount_minor: int
    session_id: uuid.UUID | None = None
    operation_id: str
    actor: str
    reason: str
    created_at: datetime


class WalletEntriesPage(StrictModel):
    """Paginated list of wallet ledger entries."""

    items: list[WalletEntryView]
    next_cursor: str | None = None


class BudgetPermit(StrictModel):
    """Response returned to runtime for a checkpoint and reservation grant."""

    session_id: uuid.UUID
    budget_version: int
    proven_ms: int
    authorized_through_ms: int
    reserved_minor: int
    permit_id: uuid.UUID
    expires_at: datetime
    funds_exhausted: bool


class BudgetView(StrictModel):
    """Call budget view returned upon budget closure or expiration."""

    session_id: uuid.UUID
    owner_user_id: uuid.UUID
    pinned_rate: int
    proven_ms: int
    authorized_through_ms: int
    charged_minor: int
    reserved_minor: int
    budget_version: int
    status: Literal["OPEN", "CLOSED"]
    waived_unknown_usage: bool = False
    stop_evidence: Any | None = None
    closed_at: datetime | None = None


# --- Input Requests -----------------------------------------------------------


class CheckpointRequest(StrictModel):
    """Runtime checkpoint and reservation request."""

    operation_id: str = Field(..., min_length=1, max_length=128)
    expected_budget_version: int = Field(..., ge=1, le=MAX_BIGINT)
    cumulative_billable_ms: int = Field(..., ge=0, le=MAX_BIGINT)
    requested_future_ms: int = Field(..., ge=0, le=MAX_BIGINT)

    @field_validator(
        "expected_budget_version",
        "cumulative_billable_ms",
        "requested_future_ms",
        mode="before",
    )
    @classmethod
    def reject_non_integers(cls, v: Any) -> Any:
        if type(v) is not int:
            raise ValueError("Integer value required; strings, floats, and booleans are forbidden")
        return v

class StopEvidence(StrictModel):
    """Trustworthy stop evidence bound to session, runner, and termination reason."""

    session_id: uuid.UUID | None = None
    runner_id: str | None = None
    reason: str
    stopped_at: datetime | None = None


class CloseBudgetRequest(StrictModel):
    """Runtime budget settlement and closure request."""

    operation_id: str = Field(..., min_length=1, max_length=128)
    expected_budget_version: int = Field(..., ge=1, le=MAX_BIGINT)
    cumulative_billable_ms: int = Field(..., ge=0, le=MAX_BIGINT)
    stop_evidence: dict[str, Any] | StopEvidence | None = None
    @field_validator(
        "expected_budget_version",
        "cumulative_billable_ms",
        mode="before",
    )
    @classmethod
    def reject_non_integers(cls, v: Any) -> Any:
        if type(v) is not int:
            raise ValueError("Integer value required; strings, floats, and booleans are forbidden")
        return v

class AdminWalletAdjustmentRequest(StrictModel):
    """Admin request to credit or adjust a user's wallet balance."""

    amount_minor: int = Field(..., ge=MIN_BIGINT, le=MAX_BIGINT)
    reason: str = Field(..., min_length=10, max_length=500)

    @field_validator("amount_minor", mode="before")
    @classmethod
    def validate_amount_before(cls, v: Any) -> Any:
        if type(v) is not int:
            raise ValueError("Integer paise required; strings, floats, and booleans are forbidden")
        if v == 0:
            raise ValueError("Adjustment amount_minor cannot be zero")
        return v

    @field_validator("amount_minor")
    @classmethod
    def validate_amount_after(cls, v: int) -> int:
        if v == 0:
            raise ValueError("Adjustment amount_minor cannot be zero")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        trimmed = v.strip()
        if len(trimmed) < 10 or len(trimmed) > 500:
            raise ValueError("Reason must be between 10 and 500 characters")
        return trimmed
__all__ = [
    "StrictModel",
    "WalletView",
    "WalletEntryView",
    "WalletEntriesPage",
    "BudgetPermit",
    "BudgetView",
    "CheckpointRequest",
    "CloseBudgetRequest",
    "StopEvidence",
    "AdminWalletAdjustmentRequest",
    "decode_stored_response",
]
