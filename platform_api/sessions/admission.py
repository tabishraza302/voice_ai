"""Fail-closed configuration, runtime authentication, and runner HTTP client."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import hmac
import ipaddress
from urllib.parse import urlsplit
from typing import Any
import uuid

import httpx
from fastapi import Request
from pydantic import ValidationError as PydanticValidationError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from platform_api.auth.dependencies import get_request_settings
from platform_api.auth.errors import (
    ConflictError,
    DependencyUnavailableError,
    ForbiddenError,
    UnauthenticatedError,
)
from platform_api.billing.service import RuntimePrincipal
from platform_api.config import PlatformSettings, _decode_b64url_32
from platform_api.sessions.schemas import (
    RuntimeRegistrationRequest,
    RuntimeReleaseAcknowledgement,
    RuntimeReleaseRequest,
    RuntimeReservation,
)


@dataclass(frozen=True)
class SessionAdmissionConfig:
    runner_id: str
    runner_url: str
    runtime_service_token: str
    runner_capacity: int
    owner_capacity: int
    signaling_url: str
    join_issuer: str
    join_private_key: Ed25519PrivateKey
    join_public_key: Ed25519PublicKey
    join_key_fingerprint: str
    join_ttl_seconds: int
    max_duration_ms: int
    setup_timeout_seconds: int
    reconnect_grace_seconds: int
    runtime_timeout_seconds: float


class RuntimeRegistrationRejectedError(ConflictError):
    """The configured runner definitively rejected registration before allocation."""


def _loopback_url(
    value: str | None,
    *,
    name: str,
    root_only: bool = False,
    require_port: bool = True,
) -> str:
    if value is None:
        raise DependencyUnavailableError(f"{name} is not configured")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise DependencyUnavailableError(f"{name} is invalid") from exc
    try:
        loopback = bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        loopback = host == "localhost"
    if (
        parsed.scheme not in {"http", "https"}
        or host is None
        or not loopback
        or (require_port and port is None)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (root_only and parsed.path not in {"", "/"})
    ):
        raise DependencyUnavailableError(
            f"{name} must target a loopback HTTP(S) endpoint"
        )
    return f"{parsed.scheme}://{parsed.netloc}" if root_only else value.rstrip("/")


def session_admission_config(settings: PlatformSettings) -> SessionAdmissionConfig:
    """Return the complete deployment contract or fail closed without defaults."""
    if not settings.enable_voice_test:
        raise DependencyUnavailableError("Voice-test admission is disabled")
    if settings.enable_public_calls or settings.enable_real_paid_calls:
        raise DependencyUnavailableError(
            "Public and real-paid session admission remain disabled"
        )
    return session_runtime_config(settings)


def public_session_admission_config(
    settings: PlatformSettings,
) -> SessionAdmissionConfig:
    """Require the explicit public-launch and transport qualifications."""
    if not settings.enable_public_calls or settings.enable_real_paid_calls:
        raise DependencyUnavailableError(
            "Public calls must be enabled and real-paid calls must remain disabled"
        )
    config = session_runtime_config(settings)
    value = settings.public_signaling_url
    if value is None:
        raise DependencyUnavailableError(
            "PLATFORM_PUBLIC_SIGNALING_URL is not configured"
        )
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError as exc:
        raise DependencyUnavailableError(
            "PLATFORM_PUBLIC_SIGNALING_URL is invalid"
        ) from exc
    host = parsed.hostname
    try:
        loopback = bool(host and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        loopback = host == "localhost"
    production = settings.env in {"staging", "production"}
    if (
        host is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path in {"", "/"}
        or len(value) > 512
        or (production and (parsed.scheme != "wss" or loopback))
        or (not production and (parsed.scheme not in {"ws", "wss"} or not loopback))
    ):
        raise DependencyUnavailableError(
            "Public signaling must be a qualified WSS endpoint outside development"
        )
    if production and (
        not settings.public_tls_qualified
        or not settings.public_turn_qualified
        or not settings.cookie_secure
        or not settings.trusted_proxy_cidrs
        or not settings.web_origin.startswith("https://")
    ):
        raise DependencyUnavailableError(
            "Public TLS, TURN, secure-cookie, and trusted-ingress qualifications are required"
        )
    return replace(config, signaling_url=value)


def session_runtime_config(settings: PlatformSettings) -> SessionAdmissionConfig:
    """Load runtime credentials independently of the new-admission kill switch."""

    required = {
        "PLATFORM_WEB_ORIGIN": settings.web_origin,
        "PLATFORM_SESSION_RUNNER_ID": settings.session_runner_id,
        "PLATFORM_SESSION_RUNNER_URL": settings.session_runner_url,
        "PLATFORM_RUNTIME_SERVICE_TOKEN": settings.runtime_service_token,
        "PLATFORM_SESSION_RUNNER_CAPACITY": settings.session_runner_capacity,
        "PLATFORM_SESSION_OWNER_CAPACITY": settings.session_owner_capacity,
        "PLATFORM_SESSION_SIGNALING_URL": settings.session_signaling_url,
        "PLATFORM_SESSION_JOIN_ISSUER": settings.session_join_issuer,
        "PLATFORM_SESSION_JOIN_PRIVATE_KEY_B64": settings.session_join_private_key_b64,
        "PLATFORM_SESSION_JOIN_PUBLIC_KEY_B64": settings.session_join_public_key_b64,
        "PLATFORM_SESSION_JOIN_TTL_SECONDS": settings.session_join_ttl_seconds,
        "PLATFORM_SESSION_MAX_DURATION_MS": settings.session_max_duration_ms,
        "PLATFORM_SESSION_SETUP_TIMEOUT_SECONDS": settings.session_setup_timeout_seconds,
        "PLATFORM_SESSION_RECONNECT_GRACE_SECONDS": settings.session_reconnect_grace_seconds,
        "PLATFORM_SESSION_RUNTIME_TIMEOUT_SECONDS": settings.session_runtime_timeout_seconds,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise DependencyUnavailableError(
            "Session admission deployment configuration is incomplete"
        )

    assert settings.runtime_service_token is not None
    assert settings.session_join_private_key_b64 is not None
    assert settings.session_join_public_key_b64 is not None
    assert settings.session_runner_url is not None
    assert settings.session_signaling_url is not None
    assert settings.web_origin is not None
    runner_url = _loopback_url(
        settings.session_runner_url,
        name="PLATFORM_SESSION_RUNNER_URL",
        root_only=True,
    )
    signaling_url = _loopback_url(
        settings.session_signaling_url,
        name="PLATFORM_SESSION_SIGNALING_URL",
    )
    parsed_origin = urlsplit(settings.web_origin)
    origin_parse_valid = True
    try:
        origin_host = parsed_origin.hostname
        _ = parsed_origin.port
        origin_loopback = bool(
            origin_host and ipaddress.ip_address(origin_host).is_loopback
        )
    except ValueError:
        origin_parse_valid = False
        origin_host = parsed_origin.hostname
        origin_loopback = origin_host == "localhost"
    if (
        not origin_parse_valid
        or parsed_origin.scheme not in {"http", "https"}
        or not origin_host
        or parsed_origin.username is not None
        or parsed_origin.password is not None
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
        or (parsed_origin.scheme == "http" and not origin_loopback)
    ):
        raise DependencyUnavailableError(
            "PLATFORM_WEB_ORIGIN must be an exact HTTPS origin or a loopback HTTP origin"
        )
    origin_url = f"{parsed_origin.scheme}://{parsed_origin.netloc}"
    if origin_url != settings.web_origin:
        raise DependencyUnavailableError(
            "PLATFORM_WEB_ORIGIN must be an exact origin without a trailing slash or path"
        )
    runtime_service_token = settings.runtime_service_token.get_secret_value()
    if (
        len(runtime_service_token.encode("utf-8")) < 32
        or any(
            not char.isprintable() or char.isspace()
            for char in runtime_service_token
        )
    ):
        raise DependencyUnavailableError(
            "PLATFORM_RUNTIME_SERVICE_TOKEN must contain at least 32 printable non-whitespace bytes"
        )
    private_bytes = _decode_b64url_32(
        settings.session_join_private_key_b64.get_secret_value()
    )
    public_bytes = _decode_b64url_32(
        settings.session_join_public_key_b64.get_secret_value()
    )
    private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
    public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
    derived_public = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    if not hmac.compare_digest(derived_public, public_bytes):
        raise DependencyUnavailableError("Session signing key pair is inconsistent")

    return SessionAdmissionConfig(
        runner_id=settings.session_runner_id,
        runner_url=runner_url,
        runtime_service_token=runtime_service_token,
        runner_capacity=settings.session_runner_capacity,
        owner_capacity=settings.session_owner_capacity,
        signaling_url=signaling_url,
        join_issuer=settings.session_join_issuer,
        join_private_key=private_key,
        join_public_key=public_key,
        join_key_fingerprint=hashlib.sha256(public_bytes).hexdigest(),
        join_ttl_seconds=settings.session_join_ttl_seconds,
        max_duration_ms=settings.session_max_duration_ms,
        setup_timeout_seconds=settings.session_setup_timeout_seconds,
        reconnect_grace_seconds=settings.session_reconnect_grace_seconds,
        runtime_timeout_seconds=settings.session_runtime_timeout_seconds,
    )


def get_runtime_principal(request: Request) -> RuntimePrincipal:
    """Authenticate the configured runtime service and its sole assigned runner."""
    config = session_runtime_config(get_request_settings(request))
    authorization = request.headers.get("authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not token
        or " " in token
        or not hmac.compare_digest(token, config.runtime_service_token)
    ):
        raise UnauthenticatedError("Runtime service authentication required")
    if not hmac.compare_digest(
        request.headers.get("x-runner-id", ""), config.runner_id
    ):
        raise ForbiddenError("Runtime runner does not match configured assignment")
    return RuntimePrincipal(runner_id=config.runner_id, service_id="voice-runtime")




def _runtime_headers(config: SessionAdmissionConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.runtime_service_token}",
        "X-Runner-ID": config.runner_id,
        "Accept": "application/json",
    }


async def register_runtime(
    config: SessionAdmissionConfig,
    command: RuntimeRegistrationRequest,
) -> RuntimeReservation:
    """Register one persisted session; retries use the same registration ID."""
    if command.runner_id != config.runner_id:
        raise ConflictError("Session runner does not match configured runner")
    url = f"{config.runner_url}/internal/runtime/sessions/{command.session_id}/register"
    try:
        async with httpx.AsyncClient(
            timeout=config.runtime_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                url,
                json=command.model_dump(mode="json"),
                headers=_runtime_headers(config),
            )
    except httpx.RequestError as exc:
        raise DependencyUnavailableError(
            "Configured runtime registration is unavailable",
        ) from exc

    if response.status_code in {400, 401, 403, 404, 422}:
        raise RuntimeRegistrationRejectedError(
            "Configured runtime rejected session registration before allocation"
        )
    if response.status_code not in {200, 201}:
        raise DependencyUnavailableError(
            "Configured runtime registration outcome is unresolved",
        )
    try:
        reservation = RuntimeReservation.model_validate_json(response.content)
    except PydanticValidationError as exc:
        raise DependencyUnavailableError(
            "Configured runtime returned an invalid reservation acknowledgement",
        ) from exc
    if (
        reservation.session_id != command.session_id
        or reservation.runner_id != command.runner_id
        or reservation.registration_id != command.registration_id
        or reservation.expires_at > command.connect_deadline
    ):
        raise DependencyUnavailableError(
            "Configured runtime reservation does not match the requested session",
        )
    return reservation


async def release_runtime(
    config: SessionAdmissionConfig,
    command: RuntimeReleaseRequest,
) -> RuntimeReleaseAcknowledgement:
    """Release only after an explicit, fully bound runner acknowledgement."""
    if command.runner_id != config.runner_id:
        raise ConflictError("Session runner does not match configured runner")
    url = f"{config.runner_url}/internal/runtime/sessions/{command.session_id}/release"
    try:
        async with httpx.AsyncClient(
            timeout=config.runtime_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(
                url,
                json=command.model_dump(mode="json"),
                headers=_runtime_headers(config),
            )
    except httpx.RequestError as exc:
        raise DependencyUnavailableError(
            "Configured runtime release outcome is unresolved",
        ) from exc
    if response.status_code != 200:
        raise DependencyUnavailableError(
            "Configured runtime did not confirm session release",
        )
    try:
        acknowledgement = RuntimeReleaseAcknowledgement.model_validate_json(
            response.content
        )
    except PydanticValidationError as exc:
        raise DependencyUnavailableError(
            "Configured runtime returned an invalid release acknowledgement",
        ) from exc
    if (
        acknowledgement.session_id != command.session_id
        or acknowledgement.runner_id != command.runner_id
        or acknowledgement.registration_id != command.registration_id
        or acknowledgement.session_version != command.session_version
    ):
        raise DependencyUnavailableError(
            "Configured runtime release acknowledgement is not session-bound",
        )
    return acknowledgement


__all__ = [
    "SessionAdmissionConfig",
    "RuntimeRegistrationRejectedError",
    "get_runtime_principal",
    "register_runtime",
    "release_runtime",
    "session_admission_config",
    "session_runtime_config",
    "public_session_admission_config",
]
