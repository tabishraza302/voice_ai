"""Empirical model side qualification probe.

Executes a bounded generation against the configured model endpoint and observes:
1. Exact pinned model identity (via provider endpoint inspection or configuration).
2. Protocol capabilities:
   - Typed state proposal and speech separation (sideband vs audio/preview).
     Requires reassembling streamed tool-call chunks into a complete valid StateProposal
     schema and checking for speech content separation; fails closed if incomplete or invalid.
   - Stop / cancel behavior (aborting an in-flight stream).
     Client abort observation alone does NOT establish server cancellation; server
     cancellation remains unsupported until verified by server-side event or token halt evidence.
   - Error handling behavior.
     Distinguishes verified model/HTTP error responses (e.g. 4xx from provider)
     from raw network exceptions.
3. Genuine measured timings:
   - Time to first token / delta.
   - Total duration.
   - Completion and prompt tokens (strictly nullable unknown if provider/usage omits them;
     never labeling character or chunk counts as tokens).

Per flow-contract.md §7-9:
- If typed state proposal separation is not supported by the provider/endpoint,
  the probe reports `metadata="none"` (or marks unsupported), rather than pretending
  or interpreting raw natural language endpoint responsiveness as capability proof.
- Never fabricates evidence.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


def validate_loopback_base_url(url: str) -> None:
    """Validate that the base URL targets localhost or a loopback IP literal.

    Per air-gap contract, non-loopback hosts are rejected before any request without
    performing DNS resolution and without network fallback.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError(f"Invalid model endpoint URL without host: {url!r}")

    if host == "localhost":
        return

    # Check if host is a loopback IP literal (IPv4 or IPv6)
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback:
            return
    except ValueError:
        pass

    raise ValueError(
        f"Air-gap violation: model endpoint host {host!r} is not localhost or loopback IP literal. "
        "External or non-loopback network calls are strictly rejected without DNS lookup."
    )
from bot.flow.schemas import StateProposal


class ModelTiming(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    time_to_first_token_ms: float | None = None
    total_duration_ms: float = 0.0
    completion_tokens: int | None = None
    prompt_tokens: int | None = None


class ProbeObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    base_url: str
    configured_model: str
    pinned_model_revision: str
    revision_pinned: bool = False
    connected: bool
    supports_separate_stream: bool
    supports_complete_tool: bool
    metadata_strategy: Literal["separate_stream", "complete_tool", "none"]
    client_abort_observed: bool
    server_cancellation_verified: bool
    error_handling_verified: bool
    timings: ModelTiming
    raw_response_preview: str = ""
    error_message: str | None = None
    unsupported_reasons: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ModelIdentity:
    """Observed model identity and whether it is pinned to a full content digest."""

    model: str
    revision: str
    provider: Literal["ollama", "openai_compatible"]
    digest_verified: bool


def normalize_model_digest(digest: Any) -> str | None:
    """Return a canonical ``sha256:<64 lowercase hex>`` digest, or None.

    A truncated digest, a format/provider label (e.g. ``gguf`` or ``@v1``), an
    empty string or a non-string value is not a pinned identity.
    """
    if not isinstance(digest, str):
        return None
    clean = digest.strip().lower()
    if clean.startswith("sha256:"):
        clean = clean[len("sha256:") :]
    if not re.fullmatch(r"[0-9a-f]{64}", clean):
        return None
    return f"sha256:{clean}"


async def inspect_model_identity(
    base_url: str,
    model_name: str,
    timeout: float = 5.0,
    client: httpx.AsyncClient | None = None,
) -> ModelIdentity:
    """Inspect model identity from an Ollama or OpenAI-compatible endpoint.

    Ollama's ``/api/tags`` and ``/api/show`` are queried without following
    redirects. Only an exact model-name match plus a full 64-hex digest is
    reported as pinned; anything else is ``<model>@unpinned`` with
    ``digest_verified=False``. No provider or revision string is invented.
    """
    validate_loopback_base_url(base_url)
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    own_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
        own_client = True

    ollama_responded = False
    try:
        # /api/tags lists exact model names and full digests.
        try:
            resp = await client.get(f"{root}/api/tags")
            if resp.status_code == 200:
                ollama_responded = True
                data = resp.json()
                for m in data.get("models", []):
                    name = m.get("name") or m.get("model")
                    if name == model_name or (name and name.split(":") == [model_name, "latest"]):
                        digest = normalize_model_digest(m.get("digest"))
                        if digest:
                            return ModelIdentity(
                                model=model_name,
                                revision=f"{model_name}@{digest}",
                                provider="ollama",
                                digest_verified=True,
                            )
        except Exception:
            pass

        # /api/show is the native fallback when tags did not list the model.
        try:
            resp = await client.post(f"{root}/api/show", json={"model": model_name})
            if resp.status_code == 200:
                ollama_responded = True
                data = resp.json()
                digest = normalize_model_digest(data.get("digest"))
                if digest:
                    return ModelIdentity(
                        model=model_name,
                        revision=f"{model_name}@{digest}",
                        provider="ollama",
                        digest_verified=True,
                    )
        except Exception:
            pass
    finally:
        if own_client:
            await client.aclose()

    return ModelIdentity(
        model=model_name,
        revision=f"{model_name}@unpinned",
        provider="ollama" if ollama_responded else "openai_compatible",
        digest_verified=False,
    )


async def inspect_pinned_model(
    base_url: str,
    model_name: str,
    timeout: float = 5.0,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Compatibility wrapper returning only the observed revision string."""
    identity = await inspect_model_identity(
        base_url=base_url,
        model_name=model_name,
        timeout=timeout,
        client=client,
    )
    return identity.revision

def assemble_tool_calls(raw_chunks: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Assemble streamed tool call deltas into complete tool call structures."""
    calls_by_index: dict[int, dict[str, Any]] = {}
    for chunk in raw_chunks:
        for tc in chunk:
            idx = tc.get("index", 0)
            if idx not in calls_by_index:
                calls_by_index[idx] = {
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": tc.get("function", {}).get("arguments", ""),
                    },
                }
            else:
                existing = calls_by_index[idx]
                if tc.get("id"):
                    existing["id"] += tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    existing["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    existing["function"]["arguments"] += fn["arguments"]

    return [calls_by_index[k] for k in sorted(calls_by_index.keys())]


async def probe_stream_generation(
    base_url: str,
    model: str,
    prompt: str,
    tools: list[dict[str, Any]] | None = None,
    extra_body: dict[str, Any] | None = None,
    timeout: float = 15.0,
    cancel_after_first_token: bool = False,
    client: httpx.AsyncClient | None = None,
) -> tuple[dict[str, Any], ModelTiming]:
    """Execute a single bounded generation stream and measure actual timings."""
    validate_loopback_base_url(base_url)
    url = f"{base_url.rstrip('/')}/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a qualification test probe."},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "temperature": 0.0,
        "max_tokens": 150,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if extra_body:
        payload.update(extra_body)

    started = time.monotonic()
    first_token_time: float | None = None
    accumulated_content = []
    accumulated_tool_chunks: list[list[dict[str, Any]]] = []
    completion_tokens: int | None = None
    prompt_tokens: int | None = None
    client_aborted = False

    own_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)
        own_client = True

    try:
        req = client.build_request("POST", url, json=payload)
        resp = await client.send(req, stream=True)
        if resp.status_code != 200:
            err_text = await resp.aread()
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {err_text.decode('utf-8', errors='replace')}",
                request=req,
                response=resp,
            )

        async for line in resp.aiter_lines():
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:].strip()
            if data_str == "[DONE]":
                break

            chunk = json.loads(data_str)
            choices = chunk.get("choices") or []
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content")
                if content:
                    if first_token_time is None:
                        first_token_time = (time.monotonic() - started) * 1000.0
                    accumulated_content.append(content)
                if "tool_calls" in delta:
                    if first_token_time is None:
                        first_token_time = (time.monotonic() - started) * 1000.0
                    accumulated_tool_chunks.append(delta["tool_calls"])

            usage = chunk.get("usage")
            if usage:
                completion_tokens = usage.get("completion_tokens")
                prompt_tokens = usage.get("prompt_tokens")

            if cancel_after_first_token and first_token_time is not None:
                client_aborted = True
                await resp.aclose()
                break

    finally:
        total_duration = (time.monotonic() - started) * 1000.0
        if own_client:
            await client.aclose()

    timing = ModelTiming(
        time_to_first_token_ms=round(first_token_time, 2) if first_token_time is not None else None,
        total_duration_ms=round(total_duration, 2),
        completion_tokens=completion_tokens,
        prompt_tokens=prompt_tokens,
    )

    assembled_calls = assemble_tool_calls(accumulated_tool_chunks)

    result = {
        "content": "".join(accumulated_content),
        "tool_calls": assembled_calls,
        "client_aborted": client_aborted,
    }
    return result, timing


async def probe_model_qualification(
    *,
    base_url: str,
    model: str,
    reasoning_effort: str = "none",
    keep_alive: str = "5m",
    timeout: float = 15.0,
    client: httpx.AsyncClient | None = None,
) -> ProbeObservation:
    """Run model side empirical qualification probe against an explicit endpoint.

    ``base_url`` and ``model`` are required: there is no development-port or
    default-model fallback. Fails or marks unsupported if typed state proposal
    and speech separation cannot be observed. Never fabricates evidence or
    confuses raw endpoint responsiveness with capability proof.
    """
    if not base_url or not base_url.strip():
        raise ValueError("base_url must be explicitly provided for the model probe")
    if not model or not model.strip():
        raise ValueError("model must be explicitly provided for the model probe")
    unsupported_reasons: list[str] = []
    extra_body: dict[str, Any] = {}
    if reasoning_effort:
        extra_body["reasoning_effort"] = reasoning_effort
    if keep_alive:
        extra_body["keep_alive"] = keep_alive

    validate_loopback_base_url(base_url)
    # 1. Pinned identity
    identity = await inspect_model_identity(
        base_url, model, timeout=min(5.0, timeout), client=client
    )
    pinned_revision = identity.revision
    if not identity.digest_verified:
        unsupported_reasons.append(
            "Model revision is not pinned to a full sha256 digest; "
            "an unpinned tag cannot be an advertised qualification tuple"
        )

    # 2. Check connection and normal bounded generation
    connected = False
    gen_result: dict[str, Any] = {}
    gen_timing = ModelTiming()
    error_msg: str | None = None
    preview = ""

    try:
        gen_result, gen_timing = await probe_stream_generation(
            base_url=base_url,
            model=model,
            prompt="Hello, reply with one short sentence.",
            extra_body=extra_body,
            timeout=timeout,
            client=client,
        )
        connected = True
        preview = gen_result.get("content", "")
    except Exception as exc:
        connected = False
        error_msg = f"Generation failed: {type(exc).__name__}: {exc}"
        unsupported_reasons.append(f"Endpoint unreachable or generation error: {exc}")

    # 3. Check for typed state proposal separation via tools
    supports_separate_stream = False
    supports_complete_tool = False
    metadata_strategy: Literal["separate_stream", "complete_tool", "none"] = "none"

    if connected:
        # StateProposal JSON schema tool definition
        state_proposal_tool = {
            "type": "function",
            "function": {
                "name": "state_proposal",
                "description": "Propose state capture updates separately from speech",
                "parameters": StateProposal.model_json_schema(),
            },
        }
        try:
            tool_res, _ = await probe_stream_generation(
                base_url=base_url,
                model=model,
                prompt="Call state_proposal with a valid StateProposal payload for employment_type=salaried",
                tools=[state_proposal_tool],
                extra_body=extra_body,
                timeout=timeout,
                client=client,
            )
            raw_calls = tool_res.get("tool_calls", [])
            valid_proposal_found = False
            for tc in raw_calls:
                fn = tc.get("function", {})
                if fn.get("name") == "state_proposal":
                    raw_args = fn.get("arguments", "")
                    try:
                        parsed_args = json.loads(raw_args)
                        StateProposal.model_validate(parsed_args)
                        valid_proposal_found = True
                        break
                    except (json.JSONDecodeError, ValidationError) as val_err:
                        unsupported_reasons.append(
                            f"Model emitted state_proposal tool call but argument schema is invalid: {val_err}"
                        )

            if valid_proposal_found:
                supports_complete_tool = True
                speech_content = tool_res.get("content", "").strip()
                if speech_content:
                    supports_separate_stream = True
                    metadata_strategy = "separate_stream"
                else:
                    metadata_strategy = "complete_tool"
            else:
                unsupported_reasons.append(
                    "Model did not emit a complete, schema-valid state_proposal tool call; "
                    "cannot separate typed state proposal from speech"
                )
        except Exception as exc:
            unsupported_reasons.append(f"Tool calling probe error: {exc}")

    if metadata_strategy == "none":
        unsupported_reasons.append(
            "Protocol does not provide verified sideband / separate state proposal streaming; "
            "metadata capability marked 'none'"
        )

    # 4. Probe cancellation behavior (client stream abort)
    client_abort_observed = False
    server_cancellation_verified = False  # Left unsupported until server-side cancellation event is proven
    if connected:
        try:
            cancel_res, _ = await probe_stream_generation(
                base_url=base_url,
                model=model,
                prompt="Count from 1 to 100 with words slowly.",
                extra_body=extra_body,
                timeout=timeout,
                cancel_after_first_token=True,
                client=client,
            )
            if cancel_res.get("client_aborted"):
                client_abort_observed = True
                # Closing stream locally only observes client abort; server-side cancellation cannot be assumed
                unsupported_reasons.append(
                    "Client stream abort observed, but server-side cancellation not verified by protocol event"
                )
        except Exception as exc:
            unsupported_reasons.append(f"Cancellation probe error: {exc}")

    # 5. Probe error handling behavior (verify expected 4xx/5xx model error response)
    error_handling_verified = False
    try:
        await probe_stream_generation(
            base_url=base_url,
            model="non_existent_invalid_model_for_probe_test_9999",
            prompt="ping",
            timeout=min(5.0, timeout),
            client=client,
        )
        unsupported_reasons.append("Invalid model request unexpectedly succeeded without error")
    except httpx.HTTPStatusError as http_err:
        # Verified provider HTTP error (e.g. 404 or 400 from model provider)
        if http_err.response.status_code in (400, 404, 422):
            error_handling_verified = True
        else:
            unsupported_reasons.append(
                f"Unexpected HTTP status {http_err.response.status_code} for invalid model probe"
            )
    except Exception as exc:
        unsupported_reasons.append(
            f"Network/transport exception {type(exc).__name__} does not prove verified model error handling: {exc}"
        )

    return ProbeObservation(
        provider=identity.provider,
        base_url=base_url,
        configured_model=model,
        pinned_model_revision=pinned_revision,
        revision_pinned=identity.digest_verified,
        connected=connected,
        supports_separate_stream=supports_separate_stream,
        supports_complete_tool=supports_complete_tool,
        metadata_strategy=metadata_strategy,
        client_abort_observed=client_abort_observed,
        server_cancellation_verified=server_cancellation_verified,
        error_handling_verified=error_handling_verified,
        timings=gen_timing,
        raw_response_preview=preview[:200],
        error_message=error_msg,
        unsupported_reasons=unsupported_reasons,
    )
