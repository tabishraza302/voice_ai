"""Origin validation, media-type enforcement, and CSRF token derivation.

Follows auth-contract.md:
- CSRF token is unpadded base64url HMAC-SHA256(PLATFORM_AUTH_CSRF_KEY, b'csrf-v1\0' + session.id.bytes + session.token_digest).
- Constant-time verification with hmac.compare_digest.
- Origin must match PLATFORM_WEB_ORIGIN exactly on all unsafe browser methods.
- Content-Type must be application/json on mutations.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import TYPE_CHECKING
import uuid

from fastapi import Request

from platform_api.auth.errors import (
    CsrfInvalidError,
    OriginForbiddenError,
    UnsupportedMediaTypeError,
)
from platform_api.config import PlatformSettings

if TYPE_CHECKING:
    from platform_api.models.auth import AuthSession


def generate_csrf_token(csrf_key_b64: str, session_id: uuid.UUID, token_digest: bytes) -> str:
    """Derive deterministic CSRF token bound to session ID and token digest."""
    # Decode 32-byte base64url CSRF key
    pad = len(csrf_key_b64.strip()) % 4
    padded = csrf_key_b64.strip() + ("=" * (4 - pad) if pad else "")
    key_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))

    message = b"csrf-v1\0" + session_id.bytes + token_digest
    digest = hmac.new(key_bytes, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_csrf_token(
    csrf_key_b64: str,
    session_id: uuid.UUID,
    token_digest: bytes,
    candidate_token: str,
) -> bool:
    """Verify CSRF token in constant time."""
    if not candidate_token:
        return False
    expected = generate_csrf_token(csrf_key_b64, session_id, token_digest)
    return hmac.compare_digest(expected, candidate_token.strip())


def validate_origin(request: Request, settings: PlatformSettings) -> None:
    """Validate Origin header against PLATFORM_WEB_ORIGIN."""
    origin = request.headers.get("origin")
    sec_fetch_site = request.headers.get("sec-fetch-site")

    if sec_fetch_site == "cross-site":
        raise OriginForbiddenError("Cross-site requests are forbidden")

    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not origin or origin.lower() == "null":
            raise OriginForbiddenError("Origin header is required for state-changing requests")
        if origin != settings.web_origin:
            raise OriginForbiddenError("Origin header is not permitted")
    elif origin:
        # If Origin is present on a safe request (e.g. GET /auth/session), it must match exactly
        if origin != settings.web_origin:
            raise OriginForbiddenError("Origin header is not permitted")


def validate_content_type(request: Request) -> None:
    """Ensure Content-Type is application/json on mutations."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        content_type = request.headers.get("content-type", "")
        # Accept application/json, application/json; charset=utf-8, etc.
        media_type = content_type.split(";")[0].strip().lower()
        if media_type != "application/json":
            raise UnsupportedMediaTypeError("Content-Type must be application/json")
