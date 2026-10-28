"""Transport qualification probe and empirical evidence inspector.

Per flow-contract.md §8–9:
- Inspects real existing transport / browser / Pipecat behavior.
- Determines whether the transport can provide independent rendered marker
  acknowledgements bound to exact session/epoch/fragment/hash.
- Unadapted TTSTextFrame, client-ready, and synthetic timers are NOT proof of
  actual speaker rendering.
- Returns unsupported (`playback='unqualified'`) if marker correlation or
  render evidence is missing; never fabricates a pass or simulates timers.
- Preserves actual observed transport data and fail-closed semantics.
- Does not invent model identities, adapter versions, or locale capabilities.
- When generating a full CapabilityReport, requires caller-supplied real
  model identity (`model_revision`), adapter metadata, and locales; otherwise
  omits combined report until model and transport observations bind them.
- Scenario cases that were not executed against an active qualified pipeline
  are labeled as unexercised / not run rather than implied exercised failures.
- Air-gap contract: probe_live_webrtc_loopback restricts base_url strictly to
  localhost or loopback IP literals (127.0.0.1, ::1, or IPv4/IPv6 loopback ranges)
  without resolving arbitrary DNS names, avoiding any external network access.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
from typing import Any, Literal
from urllib.parse import urlparse

from bot.flow.qualification import (
    CapabilityReport,
    ScenarioResult,
    canonicalize_measurement,
)
from bot.flow.schemas import AdapterCapabilities


def is_loopback_host(host: str) -> bool:
    """Verify that a hostname or IP string is strictly a local loopback address.

    Accepts 'localhost', '127.0.0.1', '::1', or any IP address where ip.is_loopback is True.
    Rejects any non-loopback IPs or external domain names (e.g. example.com, internal hostnames)
    to enforce air-gap / local-only communication without performing external DNS queries.
    """
    clean = host.strip().lower()
    if not clean:
        return False
    # Strip port if present in host string
    if clean.startswith("[") and "]" in clean:
        clean = clean[1:clean.index("]")]
    elif ":" in clean:
        parts = clean.split(":")
        if len(parts) == 2:
            clean = parts[0]

    if clean == "localhost":
        return True

    try:
        ip = ipaddress.ip_address(clean)
        return ip.is_loopback
    except ValueError:
        return False


def validate_loopback_url(url: str) -> None:
    """Validate that a URL strictly targets a local loopback address."""
    if not url or not url.strip():
        raise ValueError("base_url must be explicitly provided for live WebRTC probe")

    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme '{parsed.scheme}'; expected http or https")

    hostname = parsed.hostname
    if not hostname or not is_loopback_host(hostname):
        raise ValueError(
            f"Air-gap violation: base_url must target a loopback host (localhost, 127.0.0.1, ::1); got '{hostname}'"
        )


@dataclass(frozen=True)
class ExpectedReceiptBinding:
    """Caller-supplied expected identity for a rendered-fragment receipt.

    Without this, a data-channel ``fragment_played`` message is only recorded
    as an observed receipt; it cannot be called an exact binding because the
    probe has nothing to compare its session/epoch/fragment/hash fields to.
    """

    session_id: str
    transport_epoch: str
    fragment_id: str
    content_hash: str


@dataclass(frozen=True)
class TransportProbeEvidence:
    """Observed empirical evidence from probing a transport channel."""

    adapter_id: str
    transport_name: str
    data_channel_messages: list[dict[str, Any]] = field(default_factory=list)
    has_audio_out_track: bool = False
    has_audio_element_render_ack: bool = False
    data_channel_receipt_observed: bool = False
    has_exact_fragment_binding: bool = False
    has_exact_epoch_binding: bool = False
    has_exact_hash_binding: bool = False
    observed_latency_ms: float | None = None
    playback_boundary_description: str = ""
    unsupported_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TransportProbeResult:
    """Qualification verdict for the probed transport."""

    supported: bool
    playback_capability: Literal["qualified_fragment_completion", "unqualified"]
    capabilities: AdapterCapabilities | None
    evidence: TransportProbeEvidence
    report: CapabilityReport | None = None


def evaluate_transport_message_binding(
    message: dict[str, Any],
    *,
    expected_session_id: str,
    expected_epoch: str,
    expected_fragment_id: str,
    expected_content_hash: str,
) -> tuple[bool, list[str]]:
    """Determine whether a received transport message provides bound render evidence.

    Under flow-contract.md §8:
    A valid render receipt must be bound to exact session_id, transport_epoch,
    fragment_id, and content_hash.
    """
    failures: list[str] = []

    msg_type = message.get("type")
    if msg_type != "fragment_played":
        if msg_type == "transcript":
            failures.append("transcript / TTSTextFrame is a pipeline-stage event, not rendered audio")
        elif msg_type in ("bot-llm-text", "bot-transcription", "bot-output"):
            failures.append(f"RTVI event '{msg_type}' is generated/preview text, not rendered audio")
        elif msg_type == "bot-stopped-speaking":
            failures.append("bot-stopped-speaking is a sender/pipeline state, not rendered audio")
        else:
            failures.append(f"message type '{msg_type}' does not represent rendered marker evidence")
        return False, failures

    # Check bindings
    if message.get("session_id") != expected_session_id:
        failures.append(f"session_id mismatch: expected '{expected_session_id}', got '{message.get('session_id')}'")

    if message.get("transport_epoch") != expected_epoch:
        failures.append(f"transport_epoch mismatch: expected '{expected_epoch}', got '{message.get('transport_epoch')}'")

    if message.get("fragment_id") != expected_fragment_id:
        failures.append(f"fragment_id mismatch: expected '{expected_fragment_id}', got '{message.get('fragment_id')}'")

    if message.get("content_hash") != expected_content_hash:
        failures.append(f"content_hash mismatch: expected '{expected_content_hash}', got '{message.get('content_hash')}'")

    if not message.get("evidence_id"):
        failures.append("missing evidence_id in played marker")

    if not message.get("capability_report_hash"):
        failures.append("missing capability_report_hash in played marker")

    return len(failures) == 0, failures


def probe_existing_webrtc_transport_behavior(
    *,
    adapter_id: str = "small_webrtc_transport",
    observed_messages: list[dict[str, Any]] | None = None,
    has_audio_track: bool = False,
    measured_sink_latency_ms: float | None = None,
    expected_receipt_binding: ExpectedReceiptBinding | None = None,
    adapter_version: str | None = None,
    model_revision: str | None = None,
    metadata_strategy: Literal["separate_stream", "complete_tool", "none"] = "none",
    speech_gating: bool = False,
    cancel_generation: bool = False,
    locales: list[Literal["en", "hi"]] | None = None,
    source_revision: str | None = None,
    profile_hash: str | None = None,
) -> TransportProbeResult:
    """Inspect transport behavior against caller-supplied actual observed records.

    Requires:
    - actual `observed_messages` (empty list means none observed; never synthetic fallback).
    - actual `has_audio_track` observation.
    - `measured_sink_latency_ms` is None unless measured at the physical or browser render sink.
    - `expected_receipt_binding` when the caller knows the exact
      session/epoch/fragment/hash that should have been acknowledged. Without
      it, a receipt is only *observed*, never called an exact binding.
    - Caller may optionally provide bound full-system identity (`model_revision`, `locales`,
      `adapter_version`, `source_revision`, `profile_hash`) to construct a full `CapabilityReport`.
      If omitted, `report` and `capabilities` remain None, avoiding invented model or locale values.

    Analyzes whether the observed events contain bound rendered markers:
    1. If `observed_messages` contains unadapted TTSTextFrame (`{"type": "transcript", ...}`),
       or RTVI events (`bot-stopped-speaking`, etc.), they are rejected as unproven.
    2. A data-channel ``fragment_played`` message is recorded as a transport
       receipt. It is not a browser render acknowledgment, and it is only
       checked against exact bindings when the caller supplies them.
    3. Returns `playback='unqualified'`: no required scenario has been run,
       and this probe never substitutes a timer, transcript frame or
       data-channel receipt for rendered-audio evidence.
    """
    messages = list(observed_messages) if observed_messages is not None else []
    unsupported_reasons: list[str] = []

    receipt_messages = [
        msg for msg in messages if isinstance(msg, dict) and msg.get("type") == "fragment_played"
    ]
    data_channel_receipt_observed = bool(receipt_messages)

    bound_receipts = 0
    binding_failures: list[str] = []
    if receipt_messages and expected_receipt_binding is not None:
        for msg in receipt_messages:
            bound, failures = evaluate_transport_message_binding(
                msg,
                expected_session_id=expected_receipt_binding.session_id,
                expected_epoch=expected_receipt_binding.transport_epoch,
                expected_fragment_id=expected_receipt_binding.fragment_id,
                expected_content_hash=expected_receipt_binding.content_hash,
            )
            if bound:
                bound_receipts += 1
            else:
                binding_failures.extend(failures)

    if not messages:
        unsupported_reasons.append("No transport data-channel messages were observed from transport.")
    elif not receipt_messages:
        unsupported_reasons.append(
            "Observed data-channel messages contain no fragment_played receipt; "
            "transcript/TTSTextFrame and stopped-speaking events are not rendered-audio evidence."
        )
    elif expected_receipt_binding is None:
        unsupported_reasons.append(
            "A fragment_played receipt was observed, but no expected "
            "(session_id, transport_epoch, fragment_id, content_hash) binding was "
            "supplied, so it cannot be called an exact binding."
        )
    elif bound_receipts == 0:
        unsupported_reasons.append(
            "Observed fragment_played receipts did not match the expected exact binding: "
            + "; ".join(binding_failures)
        )

    if data_channel_receipt_observed:
        unsupported_reasons.append(
            "A data-channel fragment_played receipt is a transport receipt, not a "
            "browser render-boundary acknowledgment; no audio-element render evidence was observed."
        )

    if has_audio_track:
        unsupported_reasons.append(
            "Live WebRTC remote audio track conveys audio samples, but transport lacks "
            "speaker render timestamp or sample correlation protocol."
        )
    else:
        unsupported_reasons.append("No active WebRTC audio output track was observed.")

    unsupported_reasons.append(
        "Required qualification scenarios were not executed by this probe; "
        "playback remains unqualified."
    )

    evidence = TransportProbeEvidence(
        adapter_id=adapter_id,
        transport_name="SmallWebRTCTransport",
        data_channel_messages=messages,
        has_audio_out_track=has_audio_track,
        has_audio_element_render_ack=False,
        data_channel_receipt_observed=data_channel_receipt_observed,
        has_exact_fragment_binding=bound_receipts > 0,
        has_exact_epoch_binding=bound_receipts > 0,
        has_exact_hash_binding=bound_receipts > 0,
        observed_latency_ms=measured_sink_latency_ms,
        playback_boundary_description=(
            "Transport emits pipeline TTS text frames and a WebRTC audio stream; "
            "a data-channel fragment_played receipt is recorded separately from "
            "a browser render-boundary acknowledgment."
        ),
        unsupported_reasons=unsupported_reasons,
    )

    capabilities: AdapterCapabilities | None = None
    report: CapabilityReport | None = None

    # Only construct a combined CapabilityReport when real model identity, locales,
    # and profile info are explicitly supplied by the caller/harness.
    if (
        model_revision is not None
        and adapter_version is not None
        and locales is not None
        and source_revision is not None
        and profile_hash is not None
    ):
        measurements: dict[str, Any] = {
            "data_channel_message_count": len(messages),
            "bound_fragment_receipt_count": bound_receipts,
        }
        if measured_sink_latency_ms is not None:
            measurements["sink_latency_ms"] = canonicalize_measurement(measured_sink_latency_ms)

        not_run_evidence = {
            "status": "NOT_RUN",
            "reason": "No independent scenario runner or rendered-audio evidence is available.",
        }

        # Label unexecuted scenarios as NOT RUN rather than implying exercised failures.
        # Covers the 7 required scenario classes from flow-contract §9:218–224.
        cases = [
            ScenarioResult(
                scenario_id="transport_fragment_render_ack",
                scenario_class="normal_en_hi",
                passed=False,
                measurements=measurements,
                raw_evidence=not_run_evidence,
                trace_id="probe:webrtc:marker_ack",
                error_message="NOT RUN: Transport lacks independent rendered marker protocol to execute case",
            ),
            ScenarioResult(
                scenario_id="transport_adversarial_gating",
                scenario_class="adversarial_gating",
                passed=False,
                measurements=measurements,
                raw_evidence=not_run_evidence,
                trace_id="probe:webrtc:adversarial_gating",
                error_message="NOT RUN: Gate adversarial speech unexercised without active model/transport runner",
            ),
            ScenarioResult(
                scenario_id="transport_barge_in_cancellation",
                scenario_class="barge_in",
                passed=False,
                measurements=measurements,
                raw_evidence=not_run_evidence,
                trace_id="probe:webrtc:barge_in",
                error_message="NOT RUN: Output flush verification unexercised without rendered sample markers",
            ),
            ScenarioResult(
                scenario_id="transport_playback_faults",
                scenario_class="playback_faults",
                passed=False,
                measurements=measurements,
                raw_evidence=not_run_evidence,
                trace_id="probe:webrtc:playback_faults",
                error_message="NOT RUN: Stalled/muted output fault handling unexercised without render evidence",
            ),
            ScenarioResult(
                scenario_id="transport_stop_cancellation",
                scenario_class="stop_cancellation",
                passed=False,
                measurements=measurements,
                raw_evidence=not_run_evidence,
                trace_id="probe:webrtc:stop_cancellation",
                error_message="NOT RUN: Stop cancellation unexercised without active voice pipeline",
            ),
            ScenarioResult(
                scenario_id="transport_chat_durable_commit",
                scenario_class="chat_durable_commit",
                passed=False,
                measurements=measurements,
                raw_evidence=not_run_evidence,
                trace_id="probe:webrtc:chat_commit",
                error_message="NOT RUN: Chat durable commit parity unexercised without runner persistence",
            ),
            ScenarioResult(
                scenario_id="transport_full_path_matrix",
                scenario_class="full_path_matrix",
                passed=False,
                measurements=measurements,
                raw_evidence=not_run_evidence,
                trace_id="probe:webrtc:full_matrix",
                error_message="NOT RUN: Full path matrix unexercised without qualified model and transport",
            ),
        ]

        raw_evidence_bytes = json.dumps(
            {
                "adapter_id": adapter_id,
                "messages": messages,
                "has_audio_track": has_audio_track,
                "data_channel_receipt_observed": data_channel_receipt_observed,
                "bound_receipts": bound_receipts,
            },
            sort_keys=True,
        ).encode("utf-8")
        evidence_hash = hashlib.sha256(raw_evidence_bytes).hexdigest()

        rep = CapabilityReport(
            protocol_version=1,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            model_revision=model_revision,
            metadata=metadata_strategy,
            speech_gating=speech_gating,
            cancel_generation=cancel_generation,
            playback="unqualified",
            locales=locales,
            profile_hash=profile_hash,
            source_revision=source_revision,
            fixture_hashes={"transport_evidence": evidence_hash},
            cases=cases,
            measurements=measurements,
            raw_evidence={
                "transport_evidence_sha256": evidence_hash,
                "data_channel_receipt_observed": data_channel_receipt_observed,
                "bound_fragment_receipt_count": bound_receipts,
            },
            playback_boundary_description=evidence.playback_boundary_description,
            limitations=unsupported_reasons,
        )
        report_hash = rep.compute_hash()
        report = rep.model_copy(update={"report_hash": report_hash})

        capabilities = AdapterCapabilities(
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            model_revision=model_revision,
            metadata=metadata_strategy,
            speech_gating=speech_gating,
            cancel_generation=cancel_generation,
            playback="unqualified",
            locales=locales,
            report_hash=report_hash,
        )

    return TransportProbeResult(
        supported=False,
        playback_capability="unqualified",
        capabilities=capabilities,
        evidence=evidence,
        report=report,
    )


async def probe_live_webrtc_loopback(
    *,
    base_url: str,
    timeout_seconds: float = 5.0,
    adapter_id: str = "small_webrtc_transport",
    adapter_version: str | None = None,
    model_revision: str | None = None,
    metadata_strategy: Literal["separate_stream", "complete_tool", "none"] = "none",
    speech_gating: bool = False,
    cancel_generation: bool = False,
    locales: list[Literal["en", "hi"]] | None = None,
    source_revision: str | None = None,
    profile_hash: str | None = None,
) -> TransportProbeResult:
    """Attempt an empirical connection probe to observe live WebRTC data channel events.

    Requires an explicit `base_url` target.
    Enforces air-gap policy: `base_url` must strictly target a local loopback address
    (127.0.0.1, ::1, or localhost literal), rejecting any external or non-loopback
    addresses before initiating network activity.
    Fails closed on connection failure or errors without synthesizing fallback events.
    """
    validate_loopback_url(base_url)

    try:
        import aiohttp
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
        from aiortc.mediastreams import AudioStreamTrack
    except ImportError:
        return probe_existing_webrtc_transport_behavior(
            adapter_id=adapter_id,
            observed_messages=[],
            has_audio_track=False,
            measured_sink_latency_ms=None,
            adapter_version=adapter_version,
            model_revision=model_revision,
            metadata_strategy=metadata_strategy,
            speech_gating=speech_gating,
            cancel_generation=cancel_generation,
            locales=locales,
            source_revision=source_revision,
            profile_hash=profile_hash,
        )

    collected_messages: list[dict[str, Any]] = []
    audio_track_observed = False

    try:
        peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        silent_input = AudioStreamTrack()
        peer.addTrack(silent_input)
        channel = peer.createDataChannel("chat", ordered=True)

        @channel.on("message")
        def on_msg(message: str | bytes) -> None:
            if isinstance(message, str):
                try:
                    payload = json.loads(message)
                    if isinstance(payload, dict):
                        collected_messages.append(payload)
                except Exception:
                    pass

        @peer.on("track")
        def on_track(track: Any) -> None:
            nonlocal audio_track_observed
            if getattr(track, "kind", None) == "audio":
                audio_track_observed = True

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_seconds)) as session:
            async with session.get(f"{base_url}/api/agents", allow_redirects=False) as resp:
                if resp.status != 200:
                    await peer.close()
                    res = probe_existing_webrtc_transport_behavior(
                        adapter_id=adapter_id,
                        observed_messages=[],
                        has_audio_track=False,
                        adapter_version=adapter_version,
                        model_revision=model_revision,
                        metadata_strategy=metadata_strategy,
                        speech_gating=speech_gating,
                        cancel_generation=cancel_generation,
                        locales=locales,
                        source_revision=source_revision,
                        profile_hash=profile_hash,
                    )
                    reasons = list(res.evidence.unsupported_reasons) + [
                        f"Live probe endpoint returned status {resp.status}"
                    ]
                    new_evidence = TransportProbeEvidence(
                        adapter_id=res.evidence.adapter_id,
                        transport_name=res.evidence.transport_name,
                        data_channel_messages=[],
                        has_audio_out_track=False,
                        unsupported_reasons=reasons,
                    )
                    return TransportProbeResult(
                        supported=False,
                        playback_capability="unqualified",
                        capabilities=res.capabilities,
                        evidence=new_evidence,
                        report=res.report,
                    )

                agents = await resp.json()
                agent_id = agents[0]["id"] if agents else None

            offer = await peer.createOffer()
            await peer.setLocalDescription(offer)
            while peer.iceGatheringState != "complete":
                await asyncio.sleep(0.02)

            async with session.post(
                f"{base_url}/api/offer",
                json={
                    "sdp": peer.localDescription.sdp,
                    "type": peer.localDescription.type,
                    "request_data": {"agent_id": agent_id} if agent_id else {},
                },
                allow_redirects=False,
            ) as resp:
                if resp.status == 200:
                    answer = await resp.json()
                    await peer.setRemoteDescription(
                        RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
                    )
                    # Bounded wait to collect actual handshake / initial frames
                    await asyncio.sleep(0.2)
                else:
                    await peer.close()
                    res = probe_existing_webrtc_transport_behavior(
                        adapter_id=adapter_id,
                        observed_messages=[],
                        has_audio_track=False,
                        adapter_version=adapter_version,
                        model_revision=model_revision,
                        metadata_strategy=metadata_strategy,
                        speech_gating=speech_gating,
                        cancel_generation=cancel_generation,
                        locales=locales,
                        source_revision=source_revision,
                        profile_hash=profile_hash,
                    )
                    reasons = list(res.evidence.unsupported_reasons) + [
                        f"Live probe offer exchange failed with status {resp.status}"
                    ]
                    new_evidence = TransportProbeEvidence(
                        adapter_id=res.evidence.adapter_id,
                        transport_name=res.evidence.transport_name,
                        data_channel_messages=[],
                        has_audio_out_track=False,
                        unsupported_reasons=reasons,
                    )
                    return TransportProbeResult(
                        supported=False,
                        playback_capability="unqualified",
                        capabilities=res.capabilities,
                        evidence=new_evidence,
                        report=res.report,
                    )

        await peer.close()
    except Exception as exc:
        res = probe_existing_webrtc_transport_behavior(
            adapter_id=adapter_id,
            observed_messages=collected_messages,
            has_audio_track=audio_track_observed,
            adapter_version=adapter_version,
            model_revision=model_revision,
            metadata_strategy=metadata_strategy,
            speech_gating=speech_gating,
            cancel_generation=cancel_generation,
            locales=locales,
            source_revision=source_revision,
            profile_hash=profile_hash,
        )
        reasons = list(res.evidence.unsupported_reasons) + [f"Live probe connection failed: {exc}"]
        new_evidence = TransportProbeEvidence(
            adapter_id=res.evidence.adapter_id,
            transport_name=res.evidence.transport_name,
            data_channel_messages=collected_messages,
            has_audio_out_track=audio_track_observed,
            unsupported_reasons=reasons,
        )
        return TransportProbeResult(
            supported=False,
            playback_capability="unqualified",
            capabilities=res.capabilities,
            evidence=new_evidence,
            report=res.report,
        )

    return probe_existing_webrtc_transport_behavior(
        adapter_id=adapter_id,
        observed_messages=collected_messages,
        has_audio_track=audio_track_observed,
        adapter_version=adapter_version,
        model_revision=model_revision,
        locales=locales,
        source_revision=source_revision,
        profile_hash=profile_hash,
    )
