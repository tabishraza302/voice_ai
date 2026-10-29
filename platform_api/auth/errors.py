"""Platform API exception classes and error mapping.

Follows auth-contract.md exact status codes, error codes, and envelope structure.
"""

from __future__ import annotations

from typing import Any
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse


class PlatformError(Exception):
    """Base class for all platform API exceptions."""

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred"
    retryable: bool = False

    def __init__(
        self,
        message: str | None = None,
        fields: list[dict[str, str]] | None = None,
        retryable: bool | None = None,
        retry_after: int | None = None,
        expire_cookie: bool = False,
    ) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.fields = fields or []
        if retryable is not None:
            self.retryable = retryable
        self.retry_after = retry_after
        self.expire_cookie = expire_cookie

    def to_dict(self, request_id: uuid.UUID) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "request_id": str(request_id),
                "retryable": self.retryable,
                "fields": self.fields,
            }
        }


class TokenInvalidError(PlatformError):
    status_code = 400
    code = "TOKEN_INVALID"
    message = "Invalid or expired token"


class MfaInvalidError(PlatformError):
    status_code = 400
    code = "MFA_INVALID"
    message = "Invalid or expired authentication code"


class UnauthenticatedError(PlatformError):
    status_code = 401
    code = "UNAUTHENTICATED"
    message = "Authentication required"
    expire_cookie = True


class InvalidCredentialsError(PlatformError):
    status_code = 401
    code = "INVALID_CREDENTIALS"
    message = "Invalid credentials"
    expire_cookie = False
class ForbiddenError(PlatformError):
    status_code = 403
    code = "FORBIDDEN"
    message = "Forbidden"


class MfaRequiredError(PlatformError):
    status_code = 403
    code = "MFA_REQUIRED"
    message = "MFA verification required"


class AccountSuspendedError(PlatformError):
    status_code = 403
    code = "ACCOUNT_SUSPENDED"
    message = "Account is suspended"


class StepUpRequiredError(PlatformError):
    status_code = 403
    code = "STEP_UP_REQUIRED"
    message = "Fresh step-up authentication required"


class CsrfInvalidError(PlatformError):
    status_code = 403
    code = "CSRF_INVALID"
    message = "Invalid or missing CSRF token"


class OriginForbiddenError(PlatformError):
    status_code = 403
    code = "ORIGIN_FORBIDDEN"
    message = "Origin not allowed"


class NotFoundError(PlatformError):
    status_code = 404
    code = "NOT_FOUND"
    message = "Resource not found"


class LastAdminError(PlatformError):
    status_code = 409
    code = "LAST_ADMIN"
    message = "Cannot remove or suspend the last active administrator"


class MfaStateConflictError(PlatformError):
    status_code = 409
    code = "MFA_STATE_CONFLICT"
    message = "MFA operation not allowed in current session state"



class ConflictError(PlatformError):
    status_code = 409
    code = "CONFLICT"
    message = "Request conflicts with current state"


class RevisionConflictError(ConflictError):
    code = "REVISION_CONFLICT"
    message = "Revision conflict: resource was modified concurrently"


class IdempotencyConflictError(ConflictError):
    code = "IDEMPOTENCY_CONFLICT"
    message = "Idempotency key reused with different request payload"


class AuthoringHashMismatchError(ConflictError):
    code = "AUTHORING_HASH_STALE"
    message = "Draft authoring hash does not match expected state"

class RequestTooLargeError(PlatformError):
    status_code = 413
    code = "REQUEST_TOO_LARGE"
    message = "Request body exceeds size limit"


class UnsupportedMediaTypeError(PlatformError):
    status_code = 415
    code = "UNSUPPORTED_MEDIA_TYPE"
    message = "Unsupported media type; application/json required"


class ValidationError(PlatformError):
    status_code = 422
    code = "VALIDATION"
    message = "Validation error"


class RateLimitedError(PlatformError):
    status_code = 429
    code = "RATE_LIMITED"
    message = "Rate limit exceeded"
    retryable = True

    def __init__(self, message: str | None = None, retry_after: int = 1) -> None:
        super().__init__(message or "Rate limit exceeded", retry_after=retry_after)


class DependencyUnavailableError(PlatformError):
    status_code = 503
    code = "DEPENDENCY_UNAVAILABLE"
    message = "Database or external service temporarily unavailable"
    retryable = True

    def __init__(self, message: str | None = None, retry_after: int = 5) -> None:
        super().__init__(
            message or "Database or external service temporarily unavailable",
            retry_after=retry_after,
        )


def platform_error_response(
    exc: PlatformError,
    request_id: uuid.UUID,
    settings: Any = None,
) -> JSONResponse:
    """Create a JSONResponse from a PlatformError."""
    headers: dict[str, str] = {
        "Cache-Control": "no-store",
    }
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    response = JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict(request_id),
        headers=headers,
    )
    if getattr(exc, "expire_cookie", False) and settings is not None:
        from platform_api.auth.sessions import delete_session_cookie
        delete_session_cookie(response, settings)
    return response
