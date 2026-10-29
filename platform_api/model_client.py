"""Bounded OpenAI-compatible completion client for operator-owned endpoints."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

import httpx

from platform_api.config import PlatformSettings


class ModelClientError(RuntimeError):
    """A configured model endpoint failed or returned an unusable envelope."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "MODEL_REQUEST_FAILED",
        public_message: str = "The configured model request failed.",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = public_message


@dataclass(frozen=True, slots=True)
class ModelUsage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class ModelCompletion:
    content: str
    usage: ModelUsage | None


class OpenAICompatibleModelClient:
    """Call only explicit, operator-configured runtime and generation endpoints."""

    _MAX_RESPONSE_BYTES = 1_048_576

    def __init__(self, settings: PlatformSettings) -> None:
        self._settings = settings
        self._clients: dict[str, httpx.AsyncClient] = {}

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    async def complete(
        self,
        endpoint: Literal["runtime", "generation"],
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
        max_tokens_parameter: str,
    ) -> ModelCompletion:
        base_url, api_key, timeout_seconds = self._endpoint_settings(endpoint)
        body: dict[str, object] = {
            "model": model,
            "messages": messages,
            max_tokens_parameter: max_tokens,
            "temperature": temperature,
        }
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort

        headers = {"Accept": "application/json"}
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            client = self._client(endpoint, timeout_seconds)
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                json=body,
                headers=headers,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    status_code = response.status_code
                    raise ModelClientError(
                        f"configured model endpoint returned HTTP {status_code}",
                        code=f"MODEL_HTTP_{status_code}",
                        public_message=(
                            f"The configured model endpoint returned HTTP {status_code}."
                        ),
                    )
                response_bytes = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(response_bytes) + len(chunk) > self._MAX_RESPONSE_BYTES:
                        raise ModelClientError(
                            "configured model response exceeded its size limit",
                            code="MODEL_RESPONSE_TOO_LARGE",
                            public_message=(
                                "The configured model endpoint returned an oversized response."
                            ),
                        )
                    response_bytes.extend(chunk)
        except ModelClientError:
            raise
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise ModelClientError(
                "configured model endpoint request failed",
                code="MODEL_ENDPOINT_UNREACHABLE",
                public_message="The configured model endpoint could not complete the request.",
            ) from exc

        try:
            payload = json.loads(response_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise ModelClientError(
                "configured model endpoint returned invalid JSON",
                code="MODEL_RESPONSE_INVALID_JSON",
                public_message=(
                    "The configured model endpoint returned an invalid response."
                ),
            ) from exc

        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelClientError(
                "configured model endpoint omitted completion content",
                code="MODEL_RESPONSE_MISSING_CONTENT",
                public_message=(
                    "The configured model endpoint omitted its completion."
                ),
            ) from exc
        if not isinstance(content, str):
            raise ModelClientError(
                "configured model endpoint returned non-text completion content",
                code="MODEL_RESPONSE_NON_TEXT",
                public_message=(
                    "The configured model endpoint returned a non-text completion."
                ),
            )

        return ModelCompletion(content=content, usage=self._parse_usage(payload.get("usage")))

    def _endpoint_settings(
        self, endpoint: Literal["runtime", "generation"]
    ) -> tuple[str, str | None, int]:
        if endpoint == "runtime":
            base_url = self._settings.runtime_llm_base_url
            api_key = self._settings.runtime_llm_api_key
            timeout = self._settings.runtime_llm_timeout_seconds
        elif endpoint == "generation":
            base_url = self._settings.generation_llm_base_url
            api_key = self._settings.generation_llm_api_key
            timeout = self._settings.generation_llm_timeout_seconds
        else:
            raise ModelClientError("unknown configured model endpoint")
        if base_url is None:
            raise ModelClientError("configured model endpoint is unavailable")
        return base_url.rstrip("/"), api_key, timeout

    def _client(self, endpoint: str, timeout_seconds: int) -> httpx.AsyncClient:
        client = self._clients.get(endpoint)
        if client is None:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_seconds),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                follow_redirects=False,
            )
            self._clients[endpoint] = client
        return client

    @staticmethod
    def _parse_usage(raw_usage: object) -> ModelUsage | None:
        if not isinstance(raw_usage, dict):
            return None

        def token_count(name: str) -> int | None:
            value = raw_usage.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        prompt_tokens = token_count("prompt_tokens")
        completion_tokens = token_count("completion_tokens")
        total_tokens = token_count("total_tokens")
        if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
            total_tokens = prompt_tokens + completion_tokens
        if prompt_tokens is None and completion_tokens is None and total_tokens is None:
            return None
        return ModelUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
