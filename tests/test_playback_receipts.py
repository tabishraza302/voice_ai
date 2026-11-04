from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pipecat.frames.frames import (
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from bot.flow import compile_flow, playback_receipts
from bot.flow.schemas import (
    AdapterCapabilities,
    FragmentPlayedOutputEvent,
    FragmentQueuedOutputEvent,
    LocaleChangedEvent,
)
from bot.flow_runtime import FlowRuntime
from bot.flow.playback_receipts import (
    MAX_ACTIVE_OUTPUT_WRITES,
    MAX_CANCELLED_RANGES,
    MAX_TRACKED_FRAGMENTS,
    PlaybackBinding,
    PlaybackCancellation,
    PlaybackReceiptVerifier,
    VerifiedPlaybackReceipt,
    PcmSampleRange,
    RenderedFrameRange,
    SmallWebRTCPlaybackCursor,
    merge_playback_cancellations,
)


REPORT_HASH = "a" * 64


def _qualified_compiled_flow(*, welcome: bool):
    if welcome:
        fixture_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "flow"
            / "valid-credit-card-en-hi.json"
        )
        flow = json.loads(fixture_path.read_text(encoding="utf-8"))["flow"]
    else:
        flow = {
            "schema_version": 1,
            "entry_step_id": "intro",
            "exit_step_id": "end",
            "steps": [
                {
                    "id": "intro",
                    "title": "Introduction",
                    "type": "inform",
                    "instruction": "Introduce the service.",
                    "messages": [
                        {"id": "hello", "en": "Hello.", "hi": "नमस्ते।"}
                    ],
                    "transitions": [
                        {"when": {"operator": "completed"}, "to": "end"}
                    ],
                },
                {
                    "id": "end",
                    "title": "Finish",
                    "type": "end",
                    "instruction": "Close politely.",
                    "required": False,
                    "messages": [
                        {"id": "goodbye", "en": "Goodbye.", "hi": "नमस्ते।"}
                    ],
                    "transitions": [],
                },
            ],
        }
    capabilities = AdapterCapabilities(
        adapter_id="webrtc_browser_transport",
        adapter_version="1.0.0",
        model_revision="pinned/1",
        metadata="separate_stream",
        speech_gating=True,
        cancel_generation=True,
        playback="qualified_fragment_completion",
        locales=["en", "hi"],
        report_hash=REPORT_HASH,
    )
    return compile_flow(flow, capabilities=capabilities)


class _AtomicPlaybackStore:
    def __init__(self) -> None:
        self.attempts: list[tuple[Any, Any, tuple[Any, ...]]] = []
        self.commits: list[tuple[Any, Any, tuple[Any, ...]]] = []
        self.fail_played_commit = False

    async def commit(
        self,
        event: Any,
        reduction: Any,
        *,
        verified_receipts: tuple[Any, ...] = (),
    ) -> None:
        record = (event, reduction, tuple(verified_receipts))
        self.attempts.append(record)
        if (
            isinstance(event, FragmentPlayedOutputEvent)
            and self.fail_played_commit
        ):
            raise RuntimeError("receipt persistence failed")
        self.commits.append(record)


def make_binding(**updates: str) -> PlaybackBinding:
    values = {
        "session_id": "session-1",
        "transport_epoch": "epoch-1",
        "turn_id": "turn-1",
        "generation_id": "generation-1",
        "manifest_id": "manifest-1",
        "fragment_id": "fragment-1",
        "content_hash": "b" * 64,
        "capability_report_hash": REPORT_HASH,
    }
    values.update(updates)
    return PlaybackBinding(**values)


def make_queue_event(
    binding: PlaybackBinding, event_id: str = "queue-1"
) -> FragmentQueuedOutputEvent:
    return FragmentQueuedOutputEvent(
        event_id=event_id,
        session_id=binding.session_id,
        transport_epoch=binding.transport_epoch,
        turn_id=binding.turn_id,
        generation_id=binding.generation_id,
        manifest_id=binding.manifest_id,
        fragment_id=binding.fragment_id,
    )


def make_verifier() -> PlaybackReceiptVerifier:
    verifier = PlaybackReceiptVerifier(
        session_id="session-1",
        transport_epoch="epoch-1",
        active_peer_id="peer-1",
        capability_report_hash=REPORT_HASH,
    )
    verifier.activate_generation("turn-1", "generation-1")
    return verifier


def register_queue(
    verifier: PlaybackReceiptVerifier,
    binding: PlaybackBinding | None = None,
    *,
    event_id: str = "queue-1",
    media_samples: PcmSampleRange = PcmSampleRange(240, 480, 24000),
    commit: bool = True,
) -> tuple[PlaybackBinding, FragmentQueuedOutputEvent]:
    binding = binding or make_binding()
    queue_event = make_queue_event(binding, event_id)
    verifier.register_queued_fragment(queue_event, binding, media_samples)
    if commit:
        assert verifier.verify_event(queue_event)
        assert verifier.confirm_queued_fragment(queue_event)
    return binding, queue_event


def make_wire(
    binding: PlaybackBinding,
    *,
    media_samples: PcmSampleRange = PcmSampleRange(240, 480, 24000),
    rendered_frames: RenderedFrameRange = RenderedFrameRange(480, 960, 48000),
    evidence_id: str = "receipt-1",
) -> dict[str, Any]:
    return {
        "type": "flow-playback-receipt",
        "version": 1,
        "binding": binding.to_message(),
        "media_samples": media_samples.to_message(),
        "rendered_frames": rendered_frames.to_message(),
        "evidence_id": evidence_id,
    }


def accept_and_commit(
    verifier: PlaybackReceiptVerifier, wire: object
) -> object:
    receipt = verifier.accept_render_receipt(wire, sender_peer_id="peer-1")
    assert receipt is not None
    assert verifier.verify_event(receipt.event)
    assert verifier.verified_receipt_for_event(receipt.event) is receipt
    assert verifier.confirm_event_committed(receipt.event)
    return receipt


def test_binding_and_ranges_reject_invalid_types_and_bounds() -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        make_binding(content_hash="A" * 64)
    with pytest.raises(ValueError, match="safe integer"):
        PcmSampleRange(True, 480, 24000)
    with pytest.raises(ValueError, match="sample_end"):
        PcmSampleRange(480, 480, 24000)
    with pytest.raises(ValueError, match="sample_rate"):
        PcmSampleRange(0, 1, 7999)


def test_receipt_requires_exact_registered_queue_to_be_committed() -> None:
    verifier = make_verifier()
    binding, queue_event = register_queue(verifier, commit=False)
    wire = make_wire(binding)

    assert verifier.accept_render_receipt(wire, sender_peer_id="peer-1") is None
    assert verifier.verify_event(queue_event)
    assert verifier.accept_render_receipt(wire, sender_peer_id="peer-1") is None
    assert verifier.confirm_queued_fragment(queue_event)
    assert verifier.accept_render_receipt(wire, sender_peer_id="peer-1") is not None


def test_receipt_is_bound_to_active_authenticated_peer_and_exact_queue_tuple() -> None:
    verifier = make_verifier()
    binding, _ = register_queue(verifier)

    assert verifier.accept_render_receipt(
        make_wire(binding), sender_peer_id="wrong-peer"
    ) is None
    for field_name in (
        "session_id",
        "transport_epoch",
        "turn_id",
        "generation_id",
        "manifest_id",
        "fragment_id",
        "content_hash",
        "capability_report_hash",
    ):
        value = "0" * 64 if field_name.endswith("_hash") else f"wrong-{field_name}"
        mismatch = make_binding(**{field_name: value})
        assert verifier.accept_render_receipt(
            make_wire(mismatch, evidence_id=f"mismatch-{field_name}"),
            sender_peer_id="peer-1",
        ) is None

    assert verifier.accept_render_receipt(
        make_wire(binding), sender_peer_id="peer-1"
    ) is not None


def test_evidence_id_cannot_be_reused_for_another_fragment() -> None:
    verifier = make_verifier()
    binding, _ = register_queue(verifier)
    other_binding = make_binding(fragment_id="fragment-2")
    register_queue(verifier, other_binding, event_id="queue-2")

    assert (
        verifier.accept_render_receipt(
            make_wire(binding, evidence_id="reused-evidence"),
            sender_peer_id="peer-1",
        )
        is not None
    )
    assert (
        verifier.accept_render_receipt(
            make_wire(other_binding, evidence_id="reused-evidence"),
            sender_peer_id="peer-1",
        )
        is None
    )


def test_receipt_rejects_unknown_fields_duplicate_json_keys_and_oversize_input() -> None:
    verifier = make_verifier()
    binding, _ = register_queue(verifier)
    wire = make_wire(binding)
    wire["peer_id"] = "peer-1"
    assert verifier.accept_render_receipt(wire, sender_peer_id="peer-1") is None

    duplicate_key_json = (
        '{"type":"flow-playback-receipt","version":1,"version":1}'
    )
    assert verifier.accept_render_receipt(
        duplicate_key_json, sender_peer_id="peer-1"
    ) is None
    assert verifier.accept_render_receipt(
        "x" * (playback_receipts.MAX_PLAYBACK_CONTROL_BYTES + 1),
        sender_peer_id="peer-1",
    ) is None


def test_receipt_rejects_invalid_wire_shapes_and_duration_mismatch() -> None:
    verifier = make_verifier()
    binding, _ = register_queue(verifier)
    good = make_wire(binding)
    invalids = [
        {**good, "version": True},
        {**good, "extra": 1},
        {**good, "evidence_id": " "},
        {**good, "media_samples": {"sample_start": 0, "sample_end": 240}},
        {
            **good,
            "rendered_frames": RenderedFrameRange(480, 959, 48000).to_message(),
        },
    ]
    for wire in invalids:
        assert verifier.accept_render_receipt(wire, sender_peer_id="peer-1") is None


def test_marker_uses_exact_v1_shape_and_discarded_queue_ids_cannot_replay() -> None:
    verifier = make_verifier()
    binding = make_binding()
    queue_event = make_queue_event(binding)
    media_samples = PcmSampleRange(240, 480, 24000)
    marker = verifier.register_queued_fragment(queue_event, binding, media_samples)

    assert marker.to_message() == {
        "type": "flow-playback-marker",
        "version": 1,
        "binding": binding.to_message(),
        "media_samples": media_samples.to_message(),
    }
    verifier.discard_queued_fragment(queue_event)
    with pytest.raises(ValueError, match="queue event ID was already used"):
        verifier.register_queued_fragment(queue_event, binding, media_samples)


def test_receipt_is_single_use_and_exposed_only_after_durable_commit_confirmation() -> None:
    verifier = make_verifier()
    binding, _ = register_queue(verifier)
    wire = make_wire(binding)

    receipt = verifier.accept_render_receipt(wire, sender_peer_id="peer-1")
    assert receipt is not None
    assert isinstance(receipt.event, FragmentPlayedOutputEvent)
    assert verifier.committed_receipts() == ()
    assert verifier.verified_receipt_for_event(receipt.event) is None
    assert verifier.accept_render_receipt(wire, sender_peer_id="peer-1") is None
    assert verifier.verify_event(receipt.event)
    assert verifier.verified_receipt_for_event(receipt.event) is receipt
    assert not verifier.verify_event(receipt.event)
    assert verifier.committed_receipts() == ()
    assert verifier.confirm_event_committed(receipt.event)
    assert verifier.committed_receipts() == (receipt,)
    assert not verifier.confirm_event_committed(receipt.event)
    assert verifier.verified_receipt_for_event(receipt.event) is None


def test_forged_or_mismatched_played_event_cannot_consume_receipt() -> None:
    verifier = make_verifier()
    binding, _ = register_queue(verifier)
    receipt = verifier.accept_render_receipt(
        make_wire(binding), sender_peer_id="peer-1"
    )
    assert receipt is not None
    forged = receipt.event.model_copy(update={"evidence_id": "forged"})

    assert not verifier.verify_event(forged)
    assert verifier.verify_event(receipt.event)
    assert verifier.confirm_event_committed(receipt.event)


def test_cancel_invalidates_uncommitted_ranges_and_stale_receipts() -> None:
    verifier = make_verifier()
    binding, _ = register_queue(verifier)
    wire = make_wire(binding)
    pending = verifier.accept_render_receipt(wire, sender_peer_id="peer-1")
    assert pending is not None

    cancellation = verifier.cancel_generation("generation-1")
    assert cancellation is not None
    assert cancellation.media_ranges == (PcmSampleRange(240, 480, 24000),)
    assert cancellation.to_message() == {
        "type": "flow-playback-cancel",
        "version": 1,
        "session_id": "session-1",
        "transport_epoch": "epoch-1",
        "turn_id": "turn-1",
        "generation_id": "generation-1",
        "media_ranges": [PcmSampleRange(240, 480, 24000).to_message()],
    }
    assert not verifier.verify_event(pending.event)
    assert verifier.accept_render_receipt(wire, sender_peer_id="peer-1") is None
    assert verifier.committed_receipts() == ()

    verifier.activate_generation("turn-2", "generation-2")
    assert verifier.accept_render_receipt(wire, sender_peer_id="peer-1") is None
    with pytest.raises(ValueError, match="cancelled generation"):
        verifier.activate_generation("turn-1", "generation-1")


def test_cancel_preserves_receipt_already_confirmed_committed() -> None:
    verifier = make_verifier()
    binding, _ = register_queue(verifier)
    receipt = accept_and_commit(verifier, make_wire(binding))

    assert verifier.cancel_generation("generation-1") is not None
    assert verifier.committed_receipts() == (receipt,)


def test_generation_and_fragment_ledgers_fail_closed_at_configured_bounds() -> None:
    verifier = make_verifier()
    for index in range(MAX_TRACKED_FRAGMENTS):
        binding = make_binding(fragment_id=f"fragment-{index}")
        queue_event = make_queue_event(binding, event_id=f"queue-{index}")
        verifier.register_queued_fragment(
            queue_event, binding, PcmSampleRange(index * 240, (index + 1) * 240, 24000)
        )
    overflow_binding = make_binding(fragment_id="overflow")
    with pytest.raises(RuntimeError, match="bounded playback ledger"):
        verifier.register_queued_fragment(
            make_queue_event(overflow_binding, event_id="queue-overflow"),
            overflow_binding,
            PcmSampleRange(99999, 100239, 24000),
        )


def test_cancellation_merge_requires_same_binding_and_bounded_ranges() -> None:
    first_range = PcmSampleRange(0, 240, 24000)
    second_range = PcmSampleRange(240, 480, 24000)
    first = PlaybackCancellation(
        session_id="session-1",
        transport_epoch="epoch-1",
        turn_id="turn-1",
        generation_id="generation-1",
        media_ranges=(first_range,),
    )
    second = PlaybackCancellation(
        session_id="session-1",
        transport_epoch="epoch-1",
        turn_id="turn-1",
        generation_id="generation-1",
        media_ranges=(first_range, second_range),
    )

    merged = merge_playback_cancellations(first, second)
    assert merged is not None
    assert merged.media_ranges == (first_range, second_range)
    stale = PlaybackCancellation(
        session_id="session-1",
        transport_epoch="epoch-2",
        turn_id="turn-1",
        generation_id="generation-1",
        media_ranges=(),
    )
    with pytest.raises(ValueError, match="same session"):
        merge_playback_cancellations(first, stale)
    with pytest.raises(ValueError, match="more than"):
        PlaybackCancellation(
            session_id="session-1",
            transport_epoch="epoch-1",
            turn_id="turn-1",
            generation_id="generation-1",
            media_ranges=tuple(
                PcmSampleRange(index * 240, index * 240 + 120, 24000)
                for index in range(MAX_CANCELLED_RANGES + 1)
            ),
        )


@dataclass
class _FakeTrack:
    _sample_rate: int = 24000
    _timestamp: int = 0
    _samples_per_10ms: int = 240
    _bytes_per_10ms: int = 480
    _chunk_queue: deque[Any] = field(default_factory=deque)


@dataclass
class _FakeFrame:
    audio: bytes
    sample_rate: int
    num_channels: int
    metadata: dict[str, Any] = field(default_factory=dict)


class _FakeClient:
    def __init__(self) -> None:
        self._audio_output_track: _FakeTrack | None = _FakeTrack()
        self._voice_playback_cursor = None
        self.fail_write = False
        self.closed = False
        self.write_calls = 0
        self.write_started: asyncio.Event | None = None
        self.release_write: asyncio.Event | None = None

    async def write_audio_frame(self, frame: _FakeFrame) -> bool:
        self.write_calls += 1
        if self.write_started is not None:
            self.write_started.set()
        if self.release_write is not None:
            await self.release_write.wait()
        if self.closed:
            return False
        if self.fail_write:
            return False
        track = self._audio_output_track
        if track is None:
            return False
        if len(frame.audio) % track._bytes_per_10ms:
            return False
        count = len(frame.audio) // track._bytes_per_10ms
        track._chunk_queue.extend(
            [(bytes(track._bytes_per_10ms), None)] * count
        )
        while track._chunk_queue:
            track._chunk_queue.popleft()
            track._timestamp += track._samples_per_10ms
        return True


class _FakeTransport:
    def __init__(self) -> None:
        self._client = _FakeClient()
        self.closed = False


def _bound_frame(binding: PlaybackBinding, *, samples: int = 240) -> _FakeFrame:
    return _FakeFrame(
        audio=bytes(samples * 2),
        sample_rate=24000,
        num_channels=1,
        metadata={"flow_fragment": binding.to_message()},
    )


def test_cursor_records_exact_smallwebrtc_pts_after_successful_output_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.13.0")
    transport = _FakeTransport()
    track = transport._client._audio_output_track
    assert track is not None
    track._timestamp = 2400
    track._chunk_queue.extend([(bytes(480), None), (bytes(480), None)])
    cursor = SmallWebRTCPlaybackCursor.install(transport)
    binding = make_binding()
    cursor.begin_fragment(binding)

    assert asyncio.run(transport._client.write_audio_frame(_bound_frame(binding)))
    assert cursor.finish_fragment(binding) == PcmSampleRange(2880, 3120, 24000)
    cursor.close()


def test_cursor_never_registers_a_range_for_a_failed_output_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.13.0")
    transport = _FakeTransport()
    transport._client.fail_write = True
    cursor = SmallWebRTCPlaybackCursor.install(transport)
    binding = make_binding()
    cursor.begin_fragment(binding)

    assert not asyncio.run(
        transport._client.write_audio_frame(_bound_frame(binding))
    )
    assert cursor.finish_fragment(binding) is None
    cursor.close()


def test_cursor_invalidates_interleaved_unbound_audio_and_bad_track_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.13.0")
    transport = _FakeTransport()
    cursor = SmallWebRTCPlaybackCursor.install(transport)
    binding = make_binding()
    cursor.begin_fragment(binding)
    unbound = _FakeFrame(bytes(480), 24000, 1)
    assert asyncio.run(transport._client.write_audio_frame(unbound))
    assert asyncio.run(
        transport._client.write_audio_frame(_bound_frame(binding))
    )
    assert cursor.finish_fragment(binding) is None

    binding = make_binding(fragment_id="bad-rate")
    cursor.begin_fragment(binding)
    bad = _bound_frame(binding)
    bad.sample_rate = 48000
    with pytest.raises(RuntimeError, match="cannot capture"):
        asyncio.run(transport._client.write_audio_frame(bad))
    assert cursor.finish_fragment(binding) is None
    assert not cursor.supported
    cursor.close()


def test_cursor_cancel_returns_only_observed_partial_fragment_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.13.0")
    transport = _FakeTransport()
    cursor = SmallWebRTCPlaybackCursor.install(transport)
    binding = make_binding()
    cursor.begin_fragment(binding)
    assert asyncio.run(
        transport._client.write_audio_frame(_bound_frame(binding, samples=240))
    )

    cancellation = cursor.cancel_generation("generation-1")
    assert cancellation is not None
    assert cancellation.media_ranges == (PcmSampleRange(0, 240, 24000),)
    assert cursor.finish_fragment(binding) is None
    cursor.close()


def test_cursor_rejects_unpinned_version_and_restores_writer_on_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeTransport()
    original_writer = transport._client.write_audio_frame
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.14.0")
    with pytest.raises(RuntimeError, match="supports pipecat-ai 1.13.0"):
        SmallWebRTCPlaybackCursor.install(transport)
    assert transport._client.write_audio_frame.__func__ is original_writer.__func__
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.13.0")
    cursor = SmallWebRTCPlaybackCursor.install(transport)
    cursor.close()
    assert transport._client.write_audio_frame.__func__ is original_writer.__func__


@pytest.mark.asyncio
async def test_cursor_halt_blocks_all_future_writes_without_restoring_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.13.0")
    transport = _FakeTransport()
    client = transport._client
    cursor = SmallWebRTCPlaybackCursor.install(transport)
    binding = make_binding()
    cursor.begin_fragment(binding)
    installed_writer = client.write_audio_frame

    cursor.halt()
    assert cursor.halted
    cursor.close()
    assert client.write_audio_frame is installed_writer

    with pytest.raises(RuntimeError, match="halted"):
        await client.write_audio_frame(_bound_frame(binding))
    with pytest.raises(RuntimeError, match="halted"):
        await client.write_audio_frame(_FakeFrame(bytes(480), 24000, 1))
    assert client.write_calls == 0
    await cursor.wait_for_inflight_writes()


@pytest.mark.asyncio
async def test_halt_cancels_inflight_writer_and_blocks_later_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.13.0")
    transport = _FakeTransport()
    client = transport._client
    client.write_started = asyncio.Event()
    client.release_write = asyncio.Event()
    cursor = SmallWebRTCPlaybackCursor.install(transport)
    binding = make_binding()
    cursor.begin_fragment(binding)

    write = asyncio.create_task(
        client.write_audio_frame(_bound_frame(binding))
    )
    await client.write_started.wait()
    cursor.halt()
    with pytest.raises(asyncio.CancelledError):
        await write
    await cursor.wait_for_inflight_writes()
    with pytest.raises(RuntimeError, match="halted"):
        await client.write_audio_frame(_FakeFrame(bytes(480), 24000, 1))
    assert client.write_calls == 1

    cancellation = cursor.cancel_generation(binding.generation_id)
    assert cancellation is not None
    assert cancellation.media_ranges == ()


@pytest.mark.asyncio
async def test_cursor_bounds_active_writers_and_halts_on_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.13.0")
    transport = _FakeTransport()
    client = transport._client
    client.write_started = asyncio.Event()
    client.release_write = asyncio.Event()
    cursor = SmallWebRTCPlaybackCursor.install(transport)
    unbound = _FakeFrame(bytes(480), 24000, 1)
    writes = [
        asyncio.create_task(client.write_audio_frame(unbound))
        for _ in range(MAX_ACTIVE_OUTPUT_WRITES)
    ]
    await client.write_started.wait()
    while client.write_calls < MAX_ACTIVE_OUTPUT_WRITES:
        await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="limit exceeded"):
        await client.write_audio_frame(unbound)
    assert cursor.halted
    assert len(cursor._active_write_tasks) == MAX_ACTIVE_OUTPUT_WRITES
    await cursor.wait_for_inflight_writes()
    results = await asyncio.gather(*writes, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert client.write_calls == MAX_ACTIVE_OUTPUT_WRITES


@pytest.mark.asyncio
async def test_flow_interruption_halts_cursor_and_aborts_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.13.0")
    transport = _FakeTransport()
    client = transport._client
    client.write_started = asyncio.Event()
    client.release_write = asyncio.Event()
    store = _AtomicPlaybackStore()
    verifier = PlaybackReceiptVerifier(
        session_id="session-1",
        transport_epoch="epoch-1",
        active_peer_id="peer-1",
        capability_report_hash=REPORT_HASH,
    )
    cursor = SmallWebRTCPlaybackCursor.install(transport)
    runtime = FlowRuntime(
        _qualified_compiled_flow(welcome=True),
        "session-1",
        "epoch-1",
        event_store=store,
        session_managed=True,
    )

    async def abort_transport() -> bool:
        transport.closed = True
        client.closed = True
        client.release_write.set()
        return True

    runtime.configure_playback(
        cursor=cursor,
        verifier=verifier,
        marker_sender=lambda _: asyncio.sleep(0),
        abort_transport=abort_transport,
    )
    fragment = (await runtime.start())[0]
    await runtime.record_generated_fragment(fragment)
    assert await runtime.observe_audio_frame(fragment)
    binding = await runtime.begin_playback_fragment(fragment)
    assert binding is not None

    write = asyncio.create_task(
        client.write_audio_frame(_bound_frame(binding))
    )
    await client.write_started.wait()
    track = client._audio_output_track
    assert track is not None
    await runtime.cancel_playback_generation(fragment.generation_id)
    assert transport.closed
    assert write.done()
    with pytest.raises(asyncio.CancelledError):
        await write
    assert track._timestamp == 0
    with pytest.raises(RuntimeError, match="halted"):
        await client.write_audio_frame(_FakeFrame(bytes(480), 24000, 1))
    assert client.write_calls == 1




@pytest.mark.asyncio
async def test_generic_interruption_without_active_fragment_stops_queued_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.13.0")

    async def process_frame_without_pipeline(
        _processor: FrameProcessor, _frame: Any, _direction: FrameDirection
    ) -> None:
        return None

    monkeypatch.setattr(
        FrameProcessor, "process_frame", process_frame_without_pipeline
    )
    transport = _FakeTransport()
    client = transport._client
    store = _AtomicPlaybackStore()
    verifier = PlaybackReceiptVerifier(
        session_id="session-1",
        transport_epoch="epoch-1",
        active_peer_id="peer-1",
        capability_report_hash=REPORT_HASH,
    )
    cursor = SmallWebRTCPlaybackCursor.install(transport)
    runtime = FlowRuntime(
        _qualified_compiled_flow(welcome=True),
        "session-1",
        "epoch-1",
        event_store=store,
        session_managed=True,
    )

    async def abort_transport() -> bool:
        transport.closed = True
        client.closed = True
        return True

    runtime.configure_playback(
        cursor=cursor,
        verifier=verifier,
        marker_sender=lambda _: asyncio.sleep(0),
        abort_transport=abort_transport,
    )
    fragment = (await runtime.start())[0]
    await runtime.record_generated_fragment(fragment)
    assert await runtime.observe_audio_frame(fragment)

    processor = runtime.audio_processor()
    forwarded: list[tuple[Any, Any]] = []

    async def record_push(frame: Any, direction: FrameDirection) -> None:
        forwarded.append((frame, direction))

    processor.push_frame = record_push
    await processor.process_frame(
        TTSTextFrame(text=fragment.text, text_type="sentence"),
        FrameDirection.DOWNSTREAM,
    )
    audio_frame = OutputAudioRawFrame(
        audio=b"\x00\x00" * 240,
        sample_rate=24000,
        num_channels=1,
    )
    await processor.process_frame(audio_frame, FrameDirection.DOWNSTREAM)
    assert "flow_fragment" in audio_frame.metadata
    track = client._audio_output_track
    assert track is not None
    assert await client.write_audio_frame(audio_frame)
    await processor.process_frame(TTSStoppedFrame(), FrameDirection.DOWNSTREAM)

    assert processor._active_fragment is None
    assert any(
        isinstance(event, FragmentQueuedOutputEvent)
        for event, _, _ in store.commits
    )
    assert track._timestamp == 240

    await processor.process_frame(
        InterruptionFrame(), FrameDirection.DOWNSTREAM
    )

    assert cursor.halted
    assert transport.closed
    assert client.closed
    with pytest.raises(RuntimeError, match="halted"):
        await client.write_audio_frame(audio_frame)
    assert track._timestamp == 240


def test_cursor_rejects_boolean_channel_count_before_tracking_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(playback_receipts, "version", lambda _: "1.13.0")
    transport = _FakeTransport()
    cursor = SmallWebRTCPlaybackCursor.install(transport)
    binding = make_binding()
    cursor.begin_fragment(binding)
    malformed = _bound_frame(binding)
    malformed.num_channels = True

    with pytest.raises(RuntimeError, match="cannot capture"):
        asyncio.run(transport._client.write_audio_frame(malformed))
    assert cursor.finish_fragment(binding) is None
    assert not cursor.supported
    cursor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("welcome", [False, True])
@pytest.mark.parametrize("fail_commit", [False, True])
async def test_flow_runtime_commits_played_event_reduction_and_receipt_atomically(
    welcome: bool, fail_commit: bool
) -> None:
    store = _AtomicPlaybackStore()
    verifier = PlaybackReceiptVerifier(
        session_id="session-1",
        transport_epoch="epoch-1",
        active_peer_id="peer-1",
        capability_report_hash=REPORT_HASH,
    )
    runtime = FlowRuntime(
        _qualified_compiled_flow(welcome=welcome),
        "session-1",
        "epoch-1",
        event_store=store,
        playback_receipt_verifier=verifier,
    )

    if welcome:
        fragments = await runtime.start()
    else:
        await runtime.accept_transcription("Please begin.")
        state = runtime.state
        assert state.last_accepted_turn_id is not None
        assert state.generation_id is not None
        await runtime.apply_event(
            LocaleChangedEvent(
                event_id="locale-hi",
                session_id="session-1",
                transport_epoch="epoch-1",
                turn_id=state.last_accepted_turn_id,
                generation_id=state.generation_id,
                locale="hi",
            )
        )
        fragments = await runtime.fragments_for_generation(state.generation_id)
    assert fragments
    fragment = fragments[0]
    await runtime.record_generated_fragment(fragment)
    assert await runtime.observe_audio_frame(fragment)

    verifier.activate_generation(fragment.turn_id, fragment.generation_id)
    binding = PlaybackBinding(
        session_id=fragment.session_id,
        transport_epoch=fragment.transport_epoch,
        turn_id=fragment.turn_id,
        generation_id=fragment.generation_id,
        manifest_id=fragment.manifest_id,
        fragment_id=fragment.fragment_id,
        content_hash=fragment.content_hash,
        capability_report_hash=REPORT_HASH,
    )
    media_samples = PcmSampleRange(0, 240, 24000)
    queue_event = make_queue_event(
        binding, event_id=f"queue:{fragment.fragment_id}"
    )
    verifier.register_queued_fragment(queue_event, binding, media_samples)
    queue_reduction = await runtime.accept_output_event(queue_event)
    assert not queue_reduction.diagnostics

    # The queue event has committed durably; allow the verifier to accept its marker.
    verifier.confirm_queued_fragment(queue_event)

    store.fail_played_commit = fail_commit
    before_state = runtime.state
    wire = make_wire(binding, media_samples=media_samples)

    if fail_commit:
        with pytest.raises(RuntimeError, match="receipt persistence failed"):
            await runtime.accept_playback_receipt(
                wire, sender_peer_id="peer-1"
            )
        assert runtime.state == before_state
        assert verifier.committed_receipts() == ()
        assert not any(
            isinstance(event, FragmentPlayedOutputEvent)
            for event, _, _ in store.commits
        )
        attempted_event, _, attempted_receipts = store.attempts[-1]
        assert len(attempted_receipts) == 1
        assert isinstance(attempted_event, FragmentPlayedOutputEvent)
        assert isinstance(attempted_receipts[0], VerifiedPlaybackReceipt)
        assert (
            verifier.verified_receipt_for_event(attempted_event)
            is attempted_receipts[0]
        )
        return

    reduction = await runtime.accept_playback_receipt(
        wire, sender_peer_id="peer-1"
    )
    assert reduction is not None
    assert not reduction.diagnostics
    played_event, persisted_reduction, persisted_receipts = store.commits[-1]
    assert isinstance(played_event, FragmentPlayedOutputEvent)
    assert persisted_reduction == reduction
    assert len(persisted_receipts) == 1
    assert isinstance(persisted_receipts[0], VerifiedPlaybackReceipt)
    assert persisted_receipts[0].event == played_event
    assert verifier.committed_receipts() == persisted_receipts
    assert verifier.committed_receipts()[0] is persisted_receipts[0]
    if welcome:
        assert fragment.fragment_id in runtime.welcome_state["played_fragment_ids"]
    else:
        assert fragment.fragment_id in runtime.state.delivered_fragment_ids
