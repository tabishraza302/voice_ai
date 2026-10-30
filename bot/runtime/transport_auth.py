"""Authenticated loopback client for the platform session boundary."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import TypeVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from bot.config import Settings
from platform_api.billing.schemas import (
    BudgetPermit,
    BudgetView,
    CheckpointRequest,
    CloseBudgetRequest,
)
from platform_api.sessions.schemas import (
    JoinConsumptionRequest,
    JoinConsumptionView,
    PeerReleaseRequest,
    PeerReleaseView,
    RuntimeConfigView,
    SessionStateView,
    SessionTransitionRequest,
)


_RUNNER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RuntimeConfigurationError(ValueError):
    """The optional platform bridge is not configured for this process."""


class RuntimePlatformError(RuntimeError):
    """A bounded request to the platform session API failed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RuntimePlatformProtocolError(RuntimePlatformError):
    """The platform returned a response that did not match its strict schema."""


@dataclass(frozen=True)
class PlatformRuntimeCredentials:
    """Exact local API root and runner identity used for internal requests."""

    api_base_url: str
    runner_id: str
    service_token: str
    web_origin: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "PlatformRuntimeCredentials":
        """Validate SaaS-only settings when the session bridge is actually used."""
        base_url = settings.platform_api_base_url
        runner_id = settings.platform_session_runner_id
        token = settings.platform_runtime_service_token
        web_origin = settings.platform_web_origin
        if not all(isinstance(value, str) and value.strip() for value in (base_url, runner_id, token, web_origin)):
            raise RuntimeConfigurationError(
                "Platform session runtime requires PLATFORM_API_BASE_URL, "
                "PLATFORM_SESSION_RUNNER_ID, PLATFORM_RUNTIME_SERVICE_TOKEN, "
                "and PLATFORM_WEB_ORIGIN"
            )
        base_url = _validate_loopback_api_root(base_url.strip())
        runner_id = runner_id.strip()
        if not _RUNNER_ID_RE.fullmatch(runner_id):
            raise RuntimeConfigurationError("PLATFORM_SESSION_RUNNER_ID is invalid")
        token = token.strip()
        if len(token.encode("utf-8")) < 32 or any(
            ch.isspace() or not ch.isprintable() for ch in token
        ):
            raise RuntimeConfigurationError(
                "PLATFORM_RUNTIME_SERVICE_TOKEN must contain at least 32 printable non-whitespace bytes"
            )
        web_origin = _validate_browser_origin(web_origin.strip())
        return cls(
            api_base_url=base_url,
            runner_id=runner_id,
            service_token=token,
            web_origin=web_origin,
        )


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_loopback_api_root(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not _is_loopback_host(parsed.hostname)
    ):
        raise RuntimeConfigurationError(
            "PLATFORM_API_BASE_URL must be an HTTP(S) loopback origin with no path, query, or credentials"
        )
    try:
        parsed.port
    except ValueError as exc:
        raise RuntimeConfigurationError("PLATFORM_API_BASE_URL has an invalid port") from exc
    return f"{parsed.scheme}://{parsed.netloc}"


def _validate_browser_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (parsed.scheme == "http" and not _is_loopback_host(parsed.hostname))
    ):
        raise RuntimeConfigurationError(
            "PLATFORM_WEB_ORIGIN must be one exact HTTPS origin (or loopback HTTP origin)"
        )
    try:
        parsed.port
    except ValueError as exc:
        raise RuntimeConfigurationError("PLATFORM_WEB_ORIGIN has an invalid port") from exc
    return f"{parsed.scheme}://{parsed.netloc}"


_Response = TypeVar("_Response", bound=BaseModel)


class PlatformRuntimeClient:
    """Use only exact, authenticated platform endpoints; never ambient proxies."""

    def __init__(
        self,
        credentials: PlatformRuntimeCredentials,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.credentials = credentials
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> "PlatformRuntimeClient":
        timeout_seconds = 5.0
        raw_timeout = settings.platform_session_runtime_timeout_seconds
        if raw_timeout is not None:
            try:
                timeout_seconds = float(raw_timeout)
            except ValueError as exc:
                raise RuntimeConfigurationError(
                    "PLATFORM_SESSION_RUNTIME_TIMEOUT_SECONDS must be a positive number"
                ) from exc
            if not 0 < timeout_seconds <= 60:
                raise RuntimeConfigurationError(
                    "PLATFORM_SESSION_RUNTIME_TIMEOUT_SECONDS must be between 0 and 60"
                )
        return cls(
            PlatformRuntimeCredentials.from_settings(settings),
            timeout_seconds=timeout_seconds,
        )

    async def runtime_config(self, session_id: str) -> RuntimeConfigView:
        response = await self._request(
            "GET",
            f"/internal/sessions/{session_id}/runtime-config",
            RuntimeConfigView,
        )
        return response

    async def consume_join(
        self,
        session_id: str,
        payload: JoinConsumptionRequest,
    ) -> JoinConsumptionView:
        return await self._request(
            "POST",
            f"/internal/sessions/{session_id}/join/consume",
            JoinConsumptionView,
            payload,
        )

    async def release_peer(
        self,
        session_id: str,
        payload: PeerReleaseRequest,
    ) -> PeerReleaseView:
        return await self._request(
            "POST",
            f"/internal/sessions/{session_id}/peer/release",
            PeerReleaseView,
            payload,
        )

    async def transition(
        self,
        session_id: str,
        payload: SessionTransitionRequest,
    ) -> SessionStateView:
        return await self._request(
            "POST",
            f"/internal/sessions/{session_id}/state",
            SessionStateView,
            payload,
        )

    async def checkpoint_budget(
        self,
        session_id: str,
        payload: CheckpointRequest,
    ) -> BudgetPermit:
        return await self._request(
            "POST",
            f"/internal/sessions/{session_id}/budget/checkpoint",
            BudgetPermit,
            payload,
        )

    async def close_budget(
        self,
        session_id: str,
        payload: CloseBudgetRequest,
    ) -> BudgetView:
        return await self._request(
            "POST",
            f"/internal/sessions/{session_id}/budget/close",
            BudgetView,
            payload,
        )

    async def _request(
        self,
        method: str,
        path: str,
        response_model: type[_Response],
        payload: BaseModel | None = None,
    ) -> _Response:
        headers = {
            "Authorization": f"Bearer {self.credentials.service_token}",
            "X-Runner-ID": self.credentials.runner_id,
            "Accept": "application/json",
        }
        content = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            content = payload.model_dump_json()
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=self.timeout_seconds,
            ) as client:
                response = await client.request(
                    method,
                    f"{self.credentials.api_base_url}{path}",
                    headers=headers,
                    content=content,
                )
        except httpx.TimeoutException as exc:
            raise RuntimePlatformError("Platform session API timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimePlatformError("Platform session API is unavailable") from exc
        if response.status_code != 200:
            raise RuntimePlatformError(
                "Platform session API rejected the runtime request",
                status_code=response.status_code,
            )
        try:
            return response_model.model_validate_json(response.content)
        except (ValidationError, ValueError) as exc:
            raise RuntimePlatformProtocolError(
                "Platform session API returned an invalid response"
            ) from exc


__all__ = [
    "PlatformRuntimeClient",
    "PlatformRuntimeCredentials",
    "RuntimeConfigurationError",
    "RuntimePlatformError",
    "RuntimePlatformProtocolError",
]
