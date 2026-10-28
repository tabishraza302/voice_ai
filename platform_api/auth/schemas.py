"""Exact API wire models, request schemas, and error responses.

Follows auth-contract.md:
- Requests and responses forbid unknown fields (extra='forbid').
- Direct response shapes (no 'data' envelope).
- Error shape: { error: { code, message, request_id, retryable, fields } }.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid
import zoneinfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    """Base model that strictly forbids unexpected fields."""

    model_config = ConfigDict(extra="forbid")


# --- Wire Output Models (exact contract names) --------------------------------


class UserView(StrictModel):
    """Safe user metadata view; never leaks password hashes or secrets."""

    id: uuid.UUID
    email: str
    display_name: str
    role: Literal["user", "admin"]
    status: Literal["active", "suspended"]
    email_verified_at: datetime
    preferred_language: Literal["en", "hi"]
    timezone: str
    low_balance_notifications: bool
    mfa_enabled: bool
    created_at: datetime
    updated_at: datetime


class SessionResponse(StrictModel):
    """Current session state and CSRF token."""

    state: Literal[
        "anonymous",
        "mfa_required",
        "mfa_enrollment_required",
        "authenticated",
    ]
    user: UserView | None = None
    session_id: uuid.UUID
    expires_at: datetime
    csrf_token: str
    step_up_expires_at: datetime | None = None


class Accepted(StrictModel):
    """Generic 202 response for anti-enumeration routes."""

    status: Literal["accepted"] = "accepted"


class SessionItem(StrictModel):
    """User-visible active session summary."""

    id: uuid.UUID
    current: bool
    user_agent_label: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


class SessionList(StrictModel):
    """List of active authenticated sessions for current user."""

    items: list[SessionItem]


class MfaSetup(StrictModel):
    """One-time TOTP enrollment response containing setup secret."""

    secret: str
    provisioning_uri: str
    expires_at: datetime


class UserPage(StrictModel):
    """Paginated user listings for administrative audit."""

    items: list[UserView]
    next_cursor: str | None = None


class FieldError(StrictModel):
    """Field-specific validation error detail."""

    field: str
    code: str


class ErrorDetail(StrictModel):
    """Structured error envelope payload."""

    code: str
    message: str
    request_id: uuid.UUID
    retryable: bool = False
    fields: list[FieldError] = Field(default_factory=list)


class ApiError(StrictModel):
    """Standard platform API error envelope."""

    error: ErrorDetail


# --- Request Input Models -----------------------------------------------------


class RegisterRequest(StrictModel):
    """Signup initiation request."""

    email: str
    display_name: str


class VerifyEmailRequest(StrictModel):
    """Account verification and password selection."""

    token: str
    password: str


class LoginRequest(StrictModel):
    """Credential submission."""

    email: str
    password: str


class ResetPasswordRequest(StrictModel):
    """Password reset request initiation."""

    email: str


class ResetPasswordConfirmRequest(StrictModel):
    """Password reset confirmation with token."""

    token: str
    password: str


class ChangePasswordRequest(StrictModel):
    """Authenticated password change."""

    current_password: str
    new_password: str


class MfaEnrollRequest(StrictModel):
    """Admin TOTP enrollment initiation requiring current password."""

    current_password: str


class MfaConfirmRequest(StrictModel):
    """Confirmation of pending TOTP enrollment."""

    code: str


class MfaVerifyRequest(StrictModel):
    """Submission of TOTP code for mfa_required session."""

    code: str


class StepUpRequest(StrictModel):
    """Fresh step-up authentication for privileged mutations."""

    password: str
    code: str


class UpdateProfileRequest(StrictModel):
    """User profile preference updates."""

    display_name: str | None = None
    preferred_language: Literal["en", "hi"] | None = None
    timezone: str | None = None
    low_balance_notifications: bool | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                zoneinfo.ZoneInfo(v)
            except Exception as e:
                raise ValueError(f"Invalid timezone: {v}") from e
        return v


class AdminSuspendRequest(StrictModel):
    """Administrative user suspension request."""

    reason: str


class AdminRestoreRequest(StrictModel):
    """Administrative user restoration request."""

    reason: str
