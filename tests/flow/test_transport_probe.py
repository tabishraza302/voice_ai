"""Tests for transport capability and evidence probe per flow-contract §8–9."""

from __future__ import annotations

import pytest

from bot.flow.qualification import AgentQualificationSettings, validate_capability_report
from bot.flow.schemas import AdapterCapabilities
from bot.flow.transport_probe import (
    ExpectedReceiptBinding,
    evaluate_transport_message_binding,
    is_loopback_host,
    probe_existing_webrtc_transport_behavior,
    probe_live_webrtc_loopback,
    validate_loopback_url,
)


def test_is_loopback_host():
    """Verify loopback host validation logic."""
    assert is_loopback_host("localhost")
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("127.0.0.2")
    assert is_loopback_host("::1")
    assert is_loopback_host("127.0.0.1:7860")
    assert is_loopback_host("[::1]:7860")

    assert not is_loopback_host("example.com")
    assert not is_loopback_host("192.168.1.100")
    assert not is_loopback_host("10.0.0.1")
    assert not is_loopback_host("8.8.8.8")
    assert not is_loopback_host("")


def test_validate_loopback_url_rejects_external_hosts():
    """Air-gap validation rejects external or non-loopback URLs before network call."""
    with pytest.raises(ValueError, match="Air-gap violation"):
        validate_loopback_url("http://example.com/api")

    with pytest.raises(ValueError, match="Air-gap violation"):
        validate_loopback_url("http://192.168.1.50:8000")

    with pytest.raises(ValueError, match="Air-gap violation"):
        validate_loopback_url("http://10.0.0.1:7860")

    with pytest.raises(ValueError, match="Invalid URL scheme"):
        validate_loopback_url("ftp://127.0.0.1:7860")

    # Loopback URLs succeed without error
    validate_loopback_url("http://127.0.0.1:7860")
    validate_loopback_url("http://localhost:8000")
    validate_loopback_url("http://[::1]:7860")


def test_evaluate_transport_message_binding_rejects_raw_transcript():
    """Unadapted TTSTextFrame (type='transcript') must be rejected as render evidence."""
    msg = {"type": "transcript", "role": "assistant", "text": "Some text"}
    valid, failures = evaluate_transport_message_binding(
        msg,
        expected_session_id="s1",
        expected_epoch="ep1",
        expected_fragment_id="f1",
        expected_content_hash="h1",
    )
    assert not valid
    assert any("TTSTextFrame" in f for f in failures)


def test_evaluate_transport_message_binding_rejects_rtvi_stopped_speaking():
    """RTVI 'bot-stopped-speaking' is pipeline state, not rendered audio marker."""
    msg = {"type": "bot-stopped-speaking"}
    valid, failures = evaluate_transport_message_binding(
        msg,
        expected_session_id="s1",
        expected_epoch="ep1",
        expected_fragment_id="f1",
        expected_content_hash="h1",
    )
    assert not valid
    assert any("bot-stopped-speaking" in f for f in failures)


def test_evaluate_transport_message_binding_accepts_exact_bound_marker():
    """A message bound to exact session, epoch, fragment, and hash is valid."""
    msg = {
        "type": "fragment_played",
        "session_id": "s1",
        "transport_epoch": "ep1",
        "fragment_id": "f1",
        "content_hash": "h1",
        "evidence_id": "ev_123",
        "capability_report_hash": "rep_123",
    }
    valid, failures = evaluate_transport_message_binding(
        msg,
        expected_session_id="s1",
        expected_epoch="ep1",
        expected_fragment_id="f1",
        expected_content_hash="h1",
    )
    assert valid
    assert len(failures) == 0


def test_evaluate_transport_message_binding_rejects_mismatched_epoch_or_hash():
    """Stale or forged epoch/hash must fail binding evaluation."""
    msg = {
        "type": "fragment_played",
        "session_id": "s1",
        "transport_epoch": "stale_epoch",
        "fragment_id": "f1",
        "content_hash": "wrong_hash",
        "evidence_id": "ev_123",
    }
    valid, failures = evaluate_transport_message_binding(
        msg,
        expected_session_id="s1",
        expected_epoch="ep1",
        expected_fragment_id="f1",
        expected_content_hash="h1",
    )
    assert not valid
    assert any("transport_epoch mismatch" in f for f in failures)
    assert any("content_hash mismatch" in f for f in failures)


def test_probe_existing_webrtc_transport_without_model_identity_omits_combined_report():
    """A transport-only probe does not invent model or locale identities; report remains None."""
    actual_messages = [
        {"type": "transcript", "role": "assistant", "text": "Hello"},
        {"type": "bot-stopped-speaking"},
    ]
    result = probe_existing_webrtc_transport_behavior(
        observed_messages=actual_messages,
        has_audio_track=True,
    )

    assert not result.supported
    assert result.playback_capability == "unqualified"
    assert result.capabilities is None
    assert result.report is None

    # Transport evidence is preserved
    assert not result.evidence.has_exact_fragment_binding
    assert not result.evidence.has_audio_element_render_ack
    assert result.evidence.has_audio_out_track is True
    assert result.evidence.observed_latency_ms is None
    assert len(result.evidence.unsupported_reasons) > 0


def test_probe_existing_webrtc_transport_with_explicit_identity_binds_report_with_not_run_cases():
    """When exact model, agent settings, and locale identity are supplied, report is bound and cases are NOT RUN."""
    result = probe_existing_webrtc_transport_behavior(
        observed_messages=[{"type": "transcript", "role": "assistant", "text": "Hello"}],
        has_audio_track=True,
        adapter_version="1.0.0",
        model_revision="qwen3.5:4b@sha256:abc123def456",
        agent_settings=AgentQualificationSettings(
            llm_provider="ollama",
            llm_model="qwen3.5:4b",
            stt_provider="mlx-audio",
            stt_model="mlx-community/nemotron-3.5-asr-streaming-0.6b",
            tts_provider="mlx-audio",
            tts_model="mlx-community/Kokoro-82M-4bit",
            tts_voice_en="af_heart",
            tts_voice_hi="hf_alpha",
        ),
        locales=["en", "hi"],
        source_revision="test-commit-sha",
        profile_hash="profile:test_hash",
    )

    assert not result.supported
    assert result.playback_capability == "unqualified"
    assert result.capabilities is not None
    assert result.capabilities.model_revision == "qwen3.5:4b@sha256:abc123def456"
    assert result.capabilities.locales == ["en", "hi"]

    report = result.report
    assert report is not None
    assert report.protocol_version == 4
    assert report.agent_settings.llm_provider == "ollama"
    assert report.playback == "unqualified"
    assert report.model_revision == "qwen3.5:4b@sha256:abc123def456"
    assert report.locales == ["en", "hi"]
    assert len(report.cases) == 7
    assert {c.scenario_class for c in report.cases} == {
        "normal_en_hi",
        "adversarial_gating",
        "barge_in",
        "playback_faults",
        "stop_cancellation",
        "chat_durable_commit",
        "full_path_matrix",
    }
    assert all(c.error_message and c.error_message.startswith("NOT RUN:") for c in report.cases)

    diags = validate_capability_report(report.model_dump())
    assert any(d.code == "QUALIFICATION_FAILED" for d in diags)


def test_probe_existing_webrtc_transport_canonicalizes_sink_latency_measurement():
    """When sink latency is provided, it is stored as canonical measurement without float serialization error."""
    result = probe_existing_webrtc_transport_behavior(
        observed_messages=[{"type": "transcript", "role": "assistant", "text": "Hello"}],
        has_audio_track=True,
        measured_sink_latency_ms=12.5,
        adapter_version="1.0.0",
        model_revision="qwen3.5:4b@sha256:abc123def456",
        agent_settings=AgentQualificationSettings(
            llm_provider="ollama",
            llm_model="qwen3.5:4b",
            stt_provider="mlx-audio",
            stt_model="mlx-community/nemotron-3.5-asr-streaming-0.6b",
            tts_provider="mlx-audio",
            tts_model="mlx-community/Kokoro-82M-4bit",
            tts_voice_en="af_heart",
            tts_voice_hi="hf_alpha",
        ),
        locales=["en", "hi"],
        source_revision="test-commit-sha",
        profile_hash="profile:test_hash",
    )
    assert result.report is not None
    assert result.report.protocol_version == 4
    assert result.report.measurements.get("sink_latency_ms") == "12.5"


def test_transcript_frame_never_counts_as_a_data_channel_receipt():
    """TTSTextFrame/transcript text is not a played receipt and cannot qualify voice."""
    result = probe_existing_webrtc_transport_behavior(
        observed_messages=[
            {"type": "transcript", "role": "assistant", "text": "Required disclosure"},
            {"type": "bot-stopped-speaking"},
        ],
        has_audio_track=True,
    )

    assert result.evidence.data_channel_receipt_observed is False
    assert result.evidence.has_audio_element_render_ack is False
    assert result.evidence.has_exact_fragment_binding is False
    assert result.playback_capability == "unqualified"
    assert not result.supported


def test_bound_fragment_receipt_is_recorded_but_still_not_render_evidence():
    """A bound data-channel receipt is recorded, yet it is not a browser render ack."""
    message = {
        "type": "fragment_played",
        "session_id": "session:1",
        "transport_epoch": "epoch:1",
        "fragment_id": "frag:1",
        "content_hash": "hash:1",
        "evidence_id": "evid:1",
        "capability_report_hash": "report:1",
    }
    result = probe_existing_webrtc_transport_behavior(
        observed_messages=[message],
        has_audio_track=True,
        expected_receipt_binding=ExpectedReceiptBinding(
            session_id="session:1",
            transport_epoch="epoch:1",
            fragment_id="frag:1",
            content_hash="hash:1",
        ),
    )

    assert result.evidence.data_channel_receipt_observed is True
    assert result.evidence.has_exact_fragment_binding is True
    assert result.evidence.has_exact_epoch_binding is True
    assert result.evidence.has_exact_hash_binding is True
    # A transport receipt is not proof that a browser rendered the audio.
    assert result.evidence.has_audio_element_render_ack is False
    assert result.playback_capability == "unqualified"
    assert not result.supported
    assert any("not a browser render-boundary" in r for r in result.evidence.unsupported_reasons)


def test_receipt_without_expected_binding_is_only_observed():
    """Without caller-supplied expectations, a receipt cannot be called an exact binding."""
    message = {
        "type": "fragment_played",
        "session_id": "session:1",
        "transport_epoch": "epoch:1",
        "fragment_id": "frag:1",
        "content_hash": "hash:1",
        "evidence_id": "evid:1",
        "capability_report_hash": "report:1",
    }
    result = probe_existing_webrtc_transport_behavior(
        observed_messages=[message],
        has_audio_track=True,
    )

    assert result.evidence.data_channel_receipt_observed is True
    assert result.evidence.has_exact_fragment_binding is False
    assert any("no expected" in r for r in result.evidence.unsupported_reasons)


def test_mismatched_receipt_binding_is_rejected():
    """A stale/forged receipt must not be counted as a bound receipt."""
    message = {
        "type": "fragment_played",
        "session_id": "session:1",
        "transport_epoch": "stale:epoch",
        "fragment_id": "frag:1",
        "content_hash": "wrong:hash",
        "evidence_id": "evid:1",
        "capability_report_hash": "report:1",
    }
    result = probe_existing_webrtc_transport_behavior(
        observed_messages=[message],
        has_audio_track=True,
        expected_receipt_binding=ExpectedReceiptBinding(
            session_id="session:1",
            transport_epoch="epoch:1",
            fragment_id="frag:1",
            content_hash="hash:1",
        ),
    )

    assert result.evidence.data_channel_receipt_observed is True
    assert result.evidence.has_exact_fragment_binding is False
    assert any("did not match the expected exact binding" in r for r in result.evidence.unsupported_reasons)


def test_probe_existing_webrtc_transport_empty_messages():
    """Empty observed messages indicates no markers or data-channel messages were observed."""
    result = probe_existing_webrtc_transport_behavior(
        observed_messages=[],
        has_audio_track=False,
    )

    assert not result.supported
    assert result.playback_capability == "unqualified"
    assert result.evidence.has_audio_out_track is False
    assert result.evidence.data_channel_messages == []
    assert any("No transport data-channel messages" in r for r in result.evidence.unsupported_reasons)


@pytest.mark.asyncio
async def test_probe_live_webrtc_loopback_unreachable_fails_closed():
    """Live loopback probe against unreachable explicit port fails closed without synthetic fallback."""
    result = await probe_live_webrtc_loopback(
        base_url="http://127.0.0.1:59999",
        timeout_seconds=0.5,
    )

    assert not result.supported
    assert result.playback_capability == "unqualified"
    assert result.capabilities is None
    assert result.report is None
    assert result.evidence.data_channel_messages == []
    assert any("Live probe connection failed" in r for r in result.evidence.unsupported_reasons)


@pytest.mark.asyncio
async def test_probe_live_webrtc_loopback_airgap_rejects_external_hosts():
    """Air-gap enforcement: probe_live_webrtc_loopback rejects external host before network request."""
    with pytest.raises(ValueError, match="Air-gap violation"):
        await probe_live_webrtc_loopback(base_url="http://8.8.8.8:7860")

    with pytest.raises(ValueError, match="Air-gap violation"):
        await probe_live_webrtc_loopback(base_url="http://external.domain.com:7860")


@pytest.mark.asyncio
async def test_probe_live_webrtc_loopback_requires_explicit_endpoint():
    """Live probe rejects empty endpoint URL."""
    with pytest.raises(ValueError, match="base_url must be explicitly provided"):
        await probe_live_webrtc_loopback(base_url="")


@pytest.mark.asyncio
async def test_probe_live_webrtc_loopback_rejects_redirects():
    """Air-gap enforcement: redirects (HTTP 301/302) are not followed to target server.

    Starts two real isolated loopback HTTP servers on ephemeral ports:
    Server 1 returns a 302 redirect to Server 2.
    Asserts that the probe executes against Server 1, Server 2 NEVER receives any request,
    and the probe result remains unsupported and unqualified.
    """
    from aiohttp import web

    server2_received = False

    async def handle_server2(request):
        nonlocal server2_received
        server2_received = True
        return web.json_response([{"id": "agent_server2"}])

    app2 = web.Application()
    app2.router.add_get("/api/agents", handle_server2)
    runner2 = web.AppRunner(app2)
    await runner2.setup()
    site2 = web.TCPSite(runner2, "127.0.0.1", 0)
    await site2.start()
    port2 = site2._server.sockets[0].getsockname()[1]

    async def handle_server1(request):
        raise web.HTTPFound(f"http://127.0.0.1:{port2}/api/agents")

    app1 = web.Application()
    app1.router.add_get("/api/agents", handle_server1)
    runner1 = web.AppRunner(app1)
    await runner1.setup()
    site1 = web.TCPSite(runner1, "127.0.0.1", 0)
    await site1.start()
    port1 = site1._server.sockets[0].getsockname()[1]

    try:
        result = await probe_live_webrtc_loopback(
            base_url=f"http://127.0.0.1:{port1}",
            timeout_seconds=2.0,
        )
        # Server 2 must never have received any redirected request
        assert not server2_received
        # Result must fail closed as unsupported / unqualified
        assert not result.supported
        assert result.playback_capability == "unqualified"
    finally:
        await runner1.cleanup()
        await runner2.cleanup()
