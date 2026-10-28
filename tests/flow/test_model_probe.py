"""Focused behavioral tests for empirical model side qualification probe.

Verifies:
1. Pinned model identity inspection (Ollama show/tags or fallback).
2. Protocol observation:
   - Tool calling must reassemble chunked deltas and validate full StateProposal schema.
   - Partial/incomplete tool call arguments fail closed (metadata="none").
   - Emitting both valid StateProposal and speech content -> 'separate_stream'.
   - Emitting only valid StateProposal without speech -> 'complete_tool'.
   - Emitting only raw prose -> 'none' metadata (unsupported separation).
3. Genuine timing measurement capture:
   - Preserves nullable unknown token counts when provider omits usage tokens (no char count fallback).
4. Cancellation observation:
   - Stream abort is labeled as client abort observed only.
   - Server cancellation is left unsupported unless verified.
5. Error handling observation:
   - Rejection requires verified HTTP status error (e.g. 404), not raw transport errors.
6. Honest reporting: fail or mark unsupported when evidence is unavailable,
   never interpreting endpoint responsiveness as capability proof.
"""

from __future__ import annotations

import json
from typing import Any
import httpx
import pytest

from bot.flow.model_probe import (
    ModelTiming,
    ProbeObservation,
    assemble_tool_calls,
    inspect_model_identity,
    inspect_pinned_model,
    normalize_model_digest,
    probe_model_qualification,
    probe_stream_generation,
    validate_loopback_base_url,
)


@pytest.mark.asyncio
async def test_inspect_pinned_model_from_ollama_tags_exact_full_digest():
    """Exact 64-character sha256 digest from /api/tags must be preserved without truncation."""
    full_digest = "sha256:70079032470f3f619e8cf702e75e5cb5b26391d4e414c5c2a0d9bcf69a1945d8"
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen3.5:4b", "digest": full_digest}]},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pinned = await inspect_pinned_model(
            base_url="http://localhost:11434/v1",
            model_name="qwen3.5:4b",
            client=client,
        )
        assert pinned == f"qwen3.5:4b@{full_digest}"


@pytest.mark.asyncio
async def test_inspect_pinned_model_from_ollama_show_full_digest():
    """Full digest returned by /api/show is preserved when tags does not list model."""
    full_digest = "sha256:abcd1234ef567890abcdef1234567890abcdef1234567890abcdef1234567890"
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(
                200,
                json={"digest": full_digest, "details": {"format": "gguf"}},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pinned = await inspect_pinned_model(
            base_url="http://localhost:11434/v1",
            model_name="qwen3.5:4b",
            client=client,
        )
        assert pinned == f"qwen3.5:4b@{full_digest}"


@pytest.mark.asyncio
async def test_inspect_pinned_model_missing_digest_returns_unpinned():
    """Missing digest or format-only details must return unpinned, never fabricated revision."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3.5:4b", "digest": ""}]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {"format": "gguf"}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pinned = await inspect_pinned_model(
            base_url="http://localhost:11434/v1",
            model_name="qwen3.5:4b",
            client=client,
        )
        assert pinned == "qwen3.5:4b@unpinned"


def test_normalize_model_digest_requires_full_sha256():
    """Only a full 64-hex content digest counts as pinned."""
    full = "a" * 64
    assert normalize_model_digest(full) == f"sha256:{full}"
    assert normalize_model_digest(f"sha256:{full}") == f"sha256:{full}"
    assert normalize_model_digest(f"SHA256:{full.upper()}") == f"sha256:{full}"
    assert normalize_model_digest("sha256:abc123") is None
    assert normalize_model_digest("gguf") is None
    assert normalize_model_digest("") is None
    assert normalize_model_digest(None) is None


@pytest.mark.asyncio
async def test_inspect_model_identity_short_digest_is_unpinned():
    """A truncated digest must not be reported as a pinned revision."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen3.5:4b", "digest": "sha256:abc123"}]},
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        identity = await inspect_model_identity(
            base_url="http://localhost:11434/v1",
            model_name="qwen3.5:4b",
            client=client,
        )
    assert identity.provider == "ollama"
    assert identity.digest_verified is False
    assert identity.revision == "qwen3.5:4b@unpinned"


@pytest.mark.asyncio
async def test_inspect_model_identity_openai_compatible_without_ollama_endpoints():
    """A non-Ollama endpoint is recorded as openai_compatible, never as ollama."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        identity = await inspect_model_identity(
            base_url="http://localhost:8000/v1",
            model_name="local-model",
            client=client,
        )
    assert identity.provider == "openai_compatible"
    assert identity.digest_verified is False
    assert identity.revision == "local-model@unpinned"


@pytest.mark.asyncio
async def test_probe_model_qualification_marks_unpinned_revision_unsupported():
    """An unpinned model tag is recorded as an unsupported tuple, not silently accepted."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen3.5:4b", "digest": "sha256:abc123"}]},
            )
        if "chat/completions" in request.url.path:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("model") == "non_existent_invalid_model_for_probe_test_9999":
                return httpx.Response(404, json={"error": "model not found"})
            return httpx.Response(
                200,
                content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n',
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        obs = await probe_model_qualification(
            base_url="http://localhost:11434/v1",
            model="qwen3.5:4b",
            client=client,
        )
    assert obs.revision_pinned is False
    assert obs.pinned_model_revision == "qwen3.5:4b@unpinned"
    assert any("not pinned to a full sha256 digest" in r for r in obs.unsupported_reasons)


@pytest.mark.asyncio
async def test_probe_model_qualification_requires_explicit_endpoint_and_model():
    """There is no implicit development-port or default-model fallback."""
    with pytest.raises(TypeError):
        await probe_model_qualification()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="base_url must be explicitly provided"):
        await probe_model_qualification(base_url="", model="qwen3.5:4b")
    with pytest.raises(ValueError, match="model must be explicitly provided"):
        await probe_model_qualification(base_url="http://127.0.0.1:11434/v1", model="")


@pytest.mark.asyncio
async def test_inspect_pinned_model_unreachable_endpoint_returns_unpinned():
    """When endpoint cannot be reached, returns unpinned."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Failed to connect")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pinned = await inspect_pinned_model(
            base_url="http://localhost:11434/v1",
            model_name="qwen3.5:4b",
            client=client,
        )
        assert pinned == "qwen3.5:4b@unpinned"

@pytest.mark.asyncio
async def test_probe_stream_generation_measures_timings_and_usage():
    sse_lines = [
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" world."}}]}\n\n',
        b'data: {"choices":[],"usage":{"completion_tokens":2,"prompt_tokens":10}}\n\n',
        b'data: [DONE]\n\n',
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"".join(sse_lines))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res, timing = await probe_stream_generation(
            base_url="http://localhost:11434/v1",
            model="qwen3.5:4b",
            prompt="hi",
            client=client,
        )

        assert res["content"] == "Hello world."
        assert timing.time_to_first_token_ms is not None
        assert timing.time_to_first_token_ms >= 0.0
        assert timing.total_duration_ms >= timing.time_to_first_token_ms
        assert timing.completion_tokens == 2
        assert timing.prompt_tokens == 10


@pytest.mark.asyncio
async def test_probe_stream_generation_timing_null_tokens_when_usage_missing():
    """Completion tokens must be None if provider omits usage; never fallback to char count."""
    sse_lines = [
        b'data: {"choices":[{"delta":{"content":"Hello without usage"}}]}\n\n',
        b'data: [DONE]\n\n',
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"".join(sse_lines))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res, timing = await probe_stream_generation(
            base_url="http://localhost:11434/v1",
            model="qwen3.5:4b",
            prompt="hi",
            client=client,
        )

        assert res["content"] == "Hello without usage"
        assert timing.completion_tokens is None
        assert timing.prompt_tokens is None


@pytest.mark.asyncio
async def test_probe_stream_generation_client_abort_does_not_claim_server_cancellation():
    sse_lines = [
        b'data: {"choices":[{"delta":{"content":"One"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" two"}}]}\n\n',
        b'data: [DONE]\n\n',
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"".join(sse_lines))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        res, timing = await probe_stream_generation(
            base_url="http://localhost:11434/v1",
            model="qwen3.5:4b",
            prompt="hi",
            cancel_after_first_token=True,
            client=client,
        )

        assert res["client_aborted"] is True
        assert res["content"] == "One"
        assert timing.time_to_first_token_ms is not None


@pytest.mark.asyncio
async def test_assemble_tool_calls_merges_streamed_chunks():
    chunks = [
        [{"index": 0, "id": "call_1", "function": {"name": "state_", "arguments": '{"proposal_id":'}}]
        ,
        [{"index": 0, "function": {"name": "proposal", "arguments": '"p1"}'}}]
    ]
    assembled = assemble_tool_calls(chunks)
    assert len(assembled) == 1
    assert assembled[0]["id"] == "call_1"
    assert assembled[0]["function"]["name"] == "state_proposal"
    assert assembled[0]["function"]["arguments"] == '{"proposal_id":"p1"}'


@pytest.mark.asyncio
async def test_probe_fails_closed_on_incomplete_or_invalid_tool_call():
    """Incomplete JSON or invalid schema in state_proposal tool call must not qualify separation."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "chat/completions" in request.url.path:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("tools"):
                # Invalid schema: missing required fields for StateProposal
                sse = (
                    b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"state_proposal","arguments":"{\\"broken\\": true}"}}]}}]}\n\n'
                    b'data: [DONE]\n\n'
                )
                return httpx.Response(200, content=sse)
            else:
                return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n')
        if "non_existent_invalid_model" in request.url.path:
            return httpx.Response(404, json={"error": "model not found"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        obs: ProbeObservation = await probe_model_qualification(
            base_url="http://localhost:11434/v1",
            model="qwen3.5:4b",
            client=client,
        )

        assert obs.supports_complete_tool is False
        assert obs.supports_separate_stream is False
        assert obs.metadata_strategy == "none"
        assert any("schema is invalid" in r for r in obs.unsupported_reasons)


@pytest.mark.asyncio
async def test_probe_detects_separate_stream_when_valid_proposal_and_content_emitted():
    valid_proposal_json = json.dumps({
        "proposal_id": "prop_1",
        "session_id": "sess_1",
        "turn_id": "turn_1",
        "generation_id": "gen_1",
        "base_state_version": 1,
        "updates": [
            {
                "op": "propose",
                "field": "employment_type",
                "value": "salaried",
                "evidence": {
                    "source_turn_id": "turn_1",
                    "quote": "I am salaried",
                },
            }
        ],
    })
    def handler(request: httpx.Request) -> httpx.Response:
        if "chat/completions" in request.url.path:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("model") == "non_existent_invalid_model_for_probe_test_9999":
                return httpx.Response(404, json={"error": "model not found"})
            if body.get("tools"):
                escaped_args = valid_proposal_json.replace('"', '\\"')
                sse = (
                    f'data: {{"choices":[{{"delta":{{"content":"I am setting your employment type.","tool_calls":[{{"function":{{"name":"state_proposal","arguments":"{escaped_args}"}}}}]}}}}]}}\n\n'
                    f'data: [DONE]\n\n'
                ).encode("utf-8")
                return httpx.Response(200, content=sse)
            else:
                return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\ndata: [DONE]\n\n')
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        obs: ProbeObservation = await probe_model_qualification(
            base_url="http://localhost:11434/v1",
            model="qwen3.5:4b",
            client=client,
        )

        assert obs.connected is True
        assert obs.supports_complete_tool is True
        assert obs.supports_separate_stream is True
        assert obs.metadata_strategy == "separate_stream"
        assert obs.client_abort_observed is True
        assert obs.server_cancellation_verified is False
        assert obs.error_handling_verified is True


@pytest.mark.asyncio
async def test_probe_marks_metadata_none_when_no_tool_emitted():
    """If model only produces raw prose and no structured proposal, metadata must be 'none'."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "chat/completions" in request.url.path:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("model") == "non_existent_invalid_model_for_probe_test_9999":
                return httpx.Response(404, json={"error": "model not found"})
            sse = (
                b'data: {"choices":[{"delta":{"content":"I am a normal reply without tool calls."}}]}\n\n'
                b'data: [DONE]\n\n'
            )
            return httpx.Response(200, content=sse)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        obs: ProbeObservation = await probe_model_qualification(
            base_url="http://localhost:11434/v1",
            model="qwen3.5:4b",
            client=client,
        )

        assert obs.connected is True
        assert obs.supports_separate_stream is False
        assert obs.supports_complete_tool is False
        assert obs.metadata_strategy == "none"
        assert any("metadata capability marked 'none'" in r for r in obs.unsupported_reasons)


@pytest.mark.asyncio
async def test_probe_rejects_external_non_loopback_endpoint_without_http():
    """External hosts must be rejected before any network request or DNS lookup."""
    external_urls = [
        "https://api.openai.com/v1",
        "http://192.168.1.50:11434/v1",
        "http://example.com:8000/v1",
        "http://10.0.0.1:11434/v1",
    ]

    http_requests_made: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        http_requests_made.append(str(request.url))
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        for ext_url in external_urls:
            # Must raise ValueError before making any HTTP request
            with pytest.raises(ValueError, match="Air-gap violation"):
                validate_loopback_base_url(ext_url)

            with pytest.raises(ValueError, match="Air-gap violation"):
                await inspect_pinned_model(ext_url, "qwen3.5:4b", client=client)

            with pytest.raises(ValueError, match="Air-gap violation"):
                await probe_stream_generation(ext_url, "qwen3.5:4b", prompt="hi", client=client)

            with pytest.raises(ValueError, match="Air-gap violation"):
                await probe_model_qualification(base_url=ext_url, model="qwen3.5:4b", client=client)

    # Assert zero network requests reached transport
    assert len(http_requests_made) == 0
