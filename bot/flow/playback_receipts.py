"""Verify browser render receipts against server-observed WebRTC output samples.

A committed receipt proves only that a qualified browser reported the bound
samples in its Web Audio output graph. It cannot prove physical audibility.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable

from bot.flow.schemas import FragmentPlayedOutputEvent, FragmentQueuedOutputEvent


PIPECAT_SMALL_WEBRTC_VERSION = "1.13.0"
MAX_PLAYBACK_CONTROL_BYTES = 4096
MAX_TRACKED_FRAGMENTS = 128
MAX_ACTIVE_OUTPUT_WRITES = 128
MAX_SESSION_RECEIPTS = 4096
MAX_SESSION_QUEUE_EVENTS = 4096
MAX_SESSION_GENERATIONS = 4096
MAX_CANCELLED_GENERATIONS = MAX_SESSION_GENERATIONS
MAX_CANCELLED_RANGES = 256
_MAX_BINDING_TEXT = 256
_MAX_EVIDENCE_ID = 128
_MAX_SAFE_INTEGER = 2**53 - 1
_MIN_SAMPLE_RATE = 8000
_MAX_SAMPLE_RATE = 192000
_BINDING_FIELDS = (
    "session_id",
    "transport_epoch",
    "turn_id",
    "generation_id",
    "manifest_id",
    "fragment_id",
    "content_hash",
    "capability_report_hash",
)
_RECEIPT_KEYS = frozenset(
    {"type", "version", "binding", "media_samples", "rendered_frames", "evidence_id"}
)
_RANGE_KEYS = frozenset({"sample_start", "sample_end", "sample_rate"})
_RENDERED_RANGE_KEYS = frozenset({"frame_start", "frame_end", "sample_rate"})


def _required_text(value: object, name: str, *, limit: int = _MAX_BINDING_TEXT) -> str:
    if not isinstance(value, str) or not value or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be a non-empty string of at most {limit} characters")
    return value


def _safe_int(value: object, name: str, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= _MAX_SAFE_INTEGER
    ):
        raise ValueError(f"{name} must be a safe integer greater than or equal to {minimum}")
    return value


def _rate(value: object, name: str) -> int:
    rate = _safe_int(value, name, minimum=_MIN_SAMPLE_RATE)
    if rate > _MAX_SAMPLE_RATE:
        raise ValueError(f"{name} must be no greater than {_MAX_SAMPLE_RATE}")
    return rate


def _exact_mapping(raw: object, keys: frozenset[str], name: str) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or len(raw) != len(keys) or set(raw) != keys:
        raise ValueError(f"{name} has an invalid shape")
    return raw


@dataclass(frozen=True, slots=True)
class PlaybackBinding:
    session_id: str
    transport_epoch: str
    turn_id: str
    generation_id: str
    manifest_id: str
    fragment_id: str
    content_hash: str
    capability_report_hash: str

    def __post_init__(self) -> None:
        for name in _BINDING_FIELDS:
            _required_text(getattr(self, name), name)
        if len(self.content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_hash
        ):
            raise ValueError("content_hash must be a lowercase SHA-256 hex digest")

    @classmethod
    def from_fragment_metadata(
        cls, metadata: Mapping[str, object], capability_report_hash: str
    ) -> PlaybackBinding:
        if not isinstance(metadata, Mapping):
            raise ValueError("fragment metadata must be a mapping")
        return cls(
            session_id=metadata.get("session_id"),  # type: ignore[arg-type]
            transport_epoch=metadata.get("transport_epoch"),  # type: ignore[arg-type]
            turn_id=metadata.get("turn_id"),  # type: ignore[arg-type]
            generation_id=metadata.get("generation_id"),  # type: ignore[arg-type]
            manifest_id=metadata.get("manifest_id"),  # type: ignore[arg-type]
            fragment_id=metadata.get("fragment_id"),  # type: ignore[arg-type]
            content_hash=metadata.get("content_hash"),  # type: ignore[arg-type]
            capability_report_hash=capability_report_hash,
        )

    def to_message(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in _BINDING_FIELDS}

    def matches_queue_event(self, event: FragmentQueuedOutputEvent) -> bool:
        return (
            event.session_id == self.session_id
            and event.transport_epoch == self.transport_epoch
            and event.turn_id == self.turn_id
            and event.generation_id == self.generation_id
            and event.manifest_id == self.manifest_id
            and event.fragment_id == self.fragment_id
        )

    def matches_played_event(self, event: FragmentPlayedOutputEvent) -> bool:
        return (
            self.matches_queue_event(event)
            and event.content_hash == self.content_hash
            and event.capability_report_hash == self.capability_report_hash
        )


@dataclass(frozen=True, slots=True)
class PcmSampleRange:
    """A half-open sample interval on Pipecat's outgoing audio track."""

    sample_start: int
    sample_end: int
    sample_rate: int

    def __post_init__(self) -> None:
        _safe_int(self.sample_start, "sample_start")
        _safe_int(self.sample_end, "sample_end", minimum=1)
        _rate(self.sample_rate, "sample_rate")
        if self.sample_end <= self.sample_start:
            raise ValueError("sample_end must be greater than sample_start")

    def to_message(self) -> dict[str, int]:
        return {
            "sample_start": self.sample_start,
            "sample_end": self.sample_end,
            "sample_rate": self.sample_rate,
        }


@dataclass(frozen=True, slots=True)
class RenderedFrameRange:
    """A half-open AudioWorklet output-frame interval on AudioContext's clock."""

    frame_start: int
    frame_end: int
    sample_rate: int

    def __post_init__(self) -> None:
        _safe_int(self.frame_start, "frame_start")
        _safe_int(self.frame_end, "frame_end", minimum=1)
        _rate(self.sample_rate, "render_sample_rate")
        if self.frame_end <= self.frame_start:
            raise ValueError("frame_end must be greater than frame_start")

    def to_message(self) -> dict[str, int]:
        return {
            "frame_start": self.frame_start,
            "frame_end": self.frame_end,
            "sample_rate": self.sample_rate,
        }


@dataclass(frozen=True, slots=True)
class PlaybackMarker:
    binding: PlaybackBinding
    media_samples: PcmSampleRange

    def to_message(self) -> dict[str, object]:
        return {
            "type": "flow-playback-marker",
            "version": 1,
            "binding": self.binding.to_message(),
            "media_samples": self.media_samples.to_message(),
        }


@dataclass(frozen=True, slots=True)
class PlaybackCancellation:
    session_id: str
    transport_epoch: str
    turn_id: str
    generation_id: str
    media_ranges: tuple[PcmSampleRange, ...]

    def __post_init__(self) -> None:
        for name in ("session_id", "transport_epoch", "turn_id", "generation_id"):
            _required_text(getattr(self, name), name)
        if not isinstance(self.media_ranges, tuple):
            raise ValueError("media_ranges must be a tuple")
        if len(self.media_ranges) > MAX_CANCELLED_RANGES:
            raise ValueError(
                f"A playback cancellation cannot contain more than {MAX_CANCELLED_RANGES} ranges"
            )
        if any(not isinstance(item, PcmSampleRange) for item in self.media_ranges):
            raise ValueError("media_ranges must contain PCM sample ranges")

    def to_message(self) -> dict[str, object]:
        return {
            "type": "flow-playback-cancel",
            "version": 1,
            "session_id": self.session_id,
            "transport_epoch": self.transport_epoch,
            "turn_id": self.turn_id,
            "generation_id": self.generation_id,
            "media_ranges": [item.to_message() for item in self.media_ranges],
        }


def merge_playback_cancellations(
    *cancellations: PlaybackCancellation | None,
) -> PlaybackCancellation | None:
    present = tuple(item for item in cancellations if item is not None)
    if not present:
        return None
    first = present[0]
    ranges: list[PcmSampleRange] = []
    seen: set[PcmSampleRange] = set()
    for item in present:
        if (
            item.session_id != first.session_id
            or item.transport_epoch != first.transport_epoch
            or item.turn_id != first.turn_id
            or item.generation_id != first.generation_id
        ):
            raise ValueError(
                "Playback cancellations must name the same session, epoch, turn, and generation"
            )
        for media_range in item.media_ranges:
            if media_range not in seen:
                seen.add(media_range)
                ranges.append(media_range)
    if len(ranges) > MAX_CANCELLED_RANGES:
        raise RuntimeError("The generation exceeded the bounded playback cancellation ledger")
    return PlaybackCancellation(
        session_id=first.session_id,
        transport_epoch=first.transport_epoch,
        turn_id=first.turn_id,
        generation_id=first.generation_id,
        media_ranges=tuple(ranges),
    )


@dataclass(frozen=True, slots=True)
class VerifiedPlaybackReceipt:
    """Verifier-issued evidence; callers cannot manufacture a usable receipt."""

    event: FragmentPlayedOutputEvent
    binding: PlaybackBinding
    media_samples: PcmSampleRange
    rendered_frames: RenderedFrameRange
    evidence_id: str
    peer_id: str
    verifier_identity: str
    _authority: object = field(repr=False, compare=False)


@dataclass(slots=True)
class _QueuedFragment:
    event: FragmentQueuedOutputEvent
    binding: PlaybackBinding
    media_samples: PcmSampleRange
    queue_verifier_called: bool = False
    queue_committed: bool = False
    receipt: VerifiedPlaybackReceipt | None = None
    receipt_verifier_called: bool = False
    receipt_committed: bool = False


class PlaybackReceiptVerifier:
    """Authenticate, bind, and consume one browser render receipt per fragment."""

    def __init__(
        self,
        *,
        session_id: str,
        transport_epoch: str,
        active_peer_id: str,
        capability_report_hash: str,
    ) -> None:
        self.session_id = _required_text(session_id, "session_id")
        self.transport_epoch = _required_text(transport_epoch, "transport_epoch")
        self.active_peer_id = _required_text(active_peer_id, "active_peer_id")
        self.capability_report_hash = _required_text(
            capability_report_hash, "capability_report_hash"
        )
        self.verifier_identity = f"playback-verifier:{uuid.uuid4().hex}"
        self._authority = object()
        self._active_turn_id: str | None = None
        self._active_generation_id: str | None = None
        self._queued: dict[str, _QueuedFragment] = {}
        self._queued_by_event_id: dict[str, _QueuedFragment] = {}
        self._receipts_by_event_id: dict[str, _QueuedFragment] = {}
        self._used_evidence_ids: set[str] = set()
        self._used_queue_event_ids: set[str] = set()
        self._queue_event_count = 0
        self._committed: list[VerifiedPlaybackReceipt] = []
        self._seen_generation_ids: set[str] = set()
        self._cancelled_generation_ids: set[str] = set()
        self._receipt_count = 0
        self._closed = False

    def activate_generation(self, turn_id: str, generation_id: str) -> None:
        self._assert_open()
        turn_id = _required_text(turn_id, "turn_id")
        generation_id = _required_text(generation_id, "generation_id")
        if generation_id in self._cancelled_generation_ids:
            raise ValueError("A cancelled generation cannot be activated again")
        if (turn_id, generation_id) == (self._active_turn_id, self._active_generation_id):
            return
        if generation_id in self._seen_generation_ids:
            raise ValueError("A previously active generation cannot be activated again")
        if any(not item.receipt_committed for item in self._queued.values()):
            raise RuntimeError("Cancel or finish the active generation before activating another")
        if len(self._seen_generation_ids) >= MAX_SESSION_GENERATIONS:
            self.close()
            raise RuntimeError("The session exceeded the bounded playback generation ledger")
        self._clear_generation_entries()
        self._seen_generation_ids.add(generation_id)
        self._active_turn_id = turn_id
        self._active_generation_id = generation_id

    def register_queued_fragment(
        self,
        queue_event: FragmentQueuedOutputEvent,
        binding: PlaybackBinding,
        media_samples: PcmSampleRange,
    ) -> PlaybackMarker:
        """Register an observed output-track range before accepting its queue event."""
        self._assert_open()
        if not isinstance(queue_event, FragmentQueuedOutputEvent):
            raise ValueError("The queue event has an invalid type")
        if not isinstance(binding, PlaybackBinding) or not isinstance(
            media_samples, PcmSampleRange
        ):
            raise ValueError("Playback queue registration has invalid evidence types")
        self._validate_binding(binding)
        if not binding.matches_queue_event(queue_event):
            raise ValueError("The queue event does not match the playback binding")
        if (binding.turn_id, binding.generation_id) != (
            self._active_turn_id,
            self._active_generation_id,
        ):
            raise ValueError("The fragment does not belong to the active generation")
        _required_text(queue_event.event_id, "event_id")
        if queue_event.event_id in self._used_queue_event_ids:
            raise ValueError("The queue event ID was already used")
        if binding.fragment_id in self._queued:
            raise ValueError("The fragment was already registered")
        if len(self._queued) >= MAX_TRACKED_FRAGMENTS:
            raise RuntimeError("The active generation exceeded the bounded playback ledger")
        if self._queue_event_count >= MAX_SESSION_QUEUE_EVENTS:
            self.close()
            raise RuntimeError("The session exceeded the bounded playback queue-event ledger")
        item = _QueuedFragment(queue_event, binding, media_samples)
        self._queued[binding.fragment_id] = item
        self._queued_by_event_id[queue_event.event_id] = item
        self._used_queue_event_ids.add(queue_event.event_id)
        self._queue_event_count += 1
        return PlaybackMarker(binding, media_samples)

    def confirm_queued_fragment(self, queue_event: FragmentQueuedOutputEvent) -> bool:
        """Enable receipt acceptance only after the queue event's durable commit."""
        item = self._queued_by_event_id.get(queue_event.event_id)
        if (
            self._closed
            or item is None
            or item.event != queue_event
            or not item.queue_verifier_called
            or item.queue_committed
            or not self._is_active(item.binding)
        ):
            return False
        item.queue_committed = True
        return True

    def discard_queued_fragment(self, queue_event: FragmentQueuedOutputEvent) -> None:
        item = self._queued_by_event_id.get(queue_event.event_id)
        if item is None or item.event != queue_event or item.queue_committed:
            return
        self._queued_by_event_id.pop(queue_event.event_id, None)
        self._queued.pop(item.binding.fragment_id, None)

    def accept_render_receipt(
        self, raw_message: object, *, sender_peer_id: str
    ) -> VerifiedPlaybackReceipt | None:
        """Return proof only for a strict, active-peer receipt over rendered PCM."""
        if self._closed or sender_peer_id != self.active_peer_id:
            return None
        parsed = _parse_receipt_message(raw_message)
        if parsed is None:
            return None
        binding, media_samples, rendered_frames, evidence_id = parsed
        item = self._queued.get(binding.fragment_id)
        if (
            item is None
            or not item.queue_committed
            or item.receipt is not None
            or not self._is_active(binding)
            or binding != item.binding
            or media_samples != item.media_samples
            or evidence_id in self._used_evidence_ids
            or self._receipt_count >= MAX_SESSION_RECEIPTS
        ):
            return None
        if Fraction(
            rendered_frames.frame_end - rendered_frames.frame_start,
            rendered_frames.sample_rate,
        ) != Fraction(
            media_samples.sample_end - media_samples.sample_start,
            media_samples.sample_rate,
        ):
            return None

        event = FragmentPlayedOutputEvent(
            event_id=f"flow-playback:{uuid.uuid4().hex}",
            session_id=binding.session_id,
            transport_epoch=binding.transport_epoch,
            turn_id=binding.turn_id,
            generation_id=binding.generation_id,
            manifest_id=binding.manifest_id,
            fragment_id=binding.fragment_id,
            content_hash=binding.content_hash,
            evidence_id=evidence_id,
            capability_report_hash=binding.capability_report_hash,
        )
        record = VerifiedPlaybackReceipt(
            event=event,
            binding=binding,
            media_samples=media_samples,
            rendered_frames=rendered_frames,
            evidence_id=evidence_id,
            peer_id=sender_peer_id,
            verifier_identity=self.verifier_identity,
            _authority=self._authority,
        )
        item.receipt = record
        self._receipts_by_event_id[event.event_id] = item
        self._used_evidence_ids.add(evidence_id)
        self._receipt_count += 1
        return record

    def verify_event(self, event: object) -> bool:
        """FlowRuntime hook: allow only the exact registered queue or minted receipt."""
        if self._closed:
            return False
        if isinstance(event, FragmentQueuedOutputEvent):
            item = self._queued_by_event_id.get(event.event_id)
            if (
                item is None
                or item.event != event
                or item.queue_verifier_called
                or not self._is_active(item.binding)
            ):
                return False
            item.queue_verifier_called = True
            return True
        if isinstance(event, FragmentPlayedOutputEvent):
            item = self._receipts_by_event_id.get(event.event_id)
            record = item.receipt if item is not None else None
            if (
                item is None
                or not item.queue_committed
                or record is None
                or record._authority is not self._authority
                or record.event != event
                or not record.binding.matches_played_event(event)
                or item.receipt_verifier_called
                or not self._is_active(record.binding)
            ):
                return False
            item.receipt_verifier_called = True
            return True
        return False

    def verified_receipt_for_event(
        self, event: FragmentPlayedOutputEvent
    ) -> VerifiedPlaybackReceipt | None:
        """Return the exact pending verifier proof for the event being durably committed."""
        if self._closed or not isinstance(event, FragmentPlayedOutputEvent):
            return None
        item = self._receipts_by_event_id.get(event.event_id)
        record = item.receipt if item is not None else None
        if (
            item is None
            or record is None
            or record._authority is not self._authority
            or record.event != event
            or not item.receipt_verifier_called
            or item.receipt_committed
            or not self._is_active(record.binding)
        ):
            return None
        return record

    def confirm_event_committed(self, event: FragmentPlayedOutputEvent) -> bool:
        """Publish the proof only after FlowRuntime's durable event+receipt commit."""
        if self._closed or not isinstance(event, FragmentPlayedOutputEvent):
            return False
        item = self._receipts_by_event_id.get(event.event_id)
        record = item.receipt if item is not None else None
        if (
            item is None
            or record is None
            or record._authority is not self._authority
            or record.event != event
            or not item.receipt_verifier_called
            or item.receipt_committed
            or not self._is_active(record.binding)
        ):
            return False
        item.receipt_committed = True
        self._committed.append(record)
        self._receipts_by_event_id.pop(event.event_id, None)
        return True

    def committed_receipts(self) -> tuple[VerifiedPlaybackReceipt, ...]:
        """Return only verifier proofs confirmed after durable event-store commit."""
        return tuple(self._committed)

    def cancel_generation(self, generation_id: str) -> PlaybackCancellation | None:
        """Invalidate pending proof and return only known output-track ranges."""
        if (
            self._closed
            or generation_id != self._active_generation_id
            or self._active_turn_id is None
        ):
            return None
        ranges = tuple(
            item.media_samples
            for item in self._queued.values()
            if item.binding.generation_id == generation_id and not item.receipt_committed
        )
        cancellation = PlaybackCancellation(
            session_id=self.session_id,
            transport_epoch=self.transport_epoch,
            turn_id=self._active_turn_id,
            generation_id=generation_id,
            media_ranges=ranges,
        )
        if len(self._cancelled_generation_ids) >= MAX_CANCELLED_GENERATIONS:
            self.close()
            return cancellation
        self._cancelled_generation_ids.add(generation_id)
        self._clear_generation_entries()
        self._active_turn_id = None
        self._active_generation_id = None
        return cancellation

    def close(self) -> None:
        self._closed = True
        self._clear_generation_entries()
        self._active_turn_id = None
        self._active_generation_id = None

    def _validate_binding(self, binding: PlaybackBinding) -> None:
        if (
            binding.session_id != self.session_id
            or binding.transport_epoch != self.transport_epoch
            or binding.capability_report_hash != self.capability_report_hash
        ):
            raise ValueError("The playback binding does not match this session and qualification")

    def _is_active(self, binding: PlaybackBinding) -> bool:
        return (
            binding.session_id == self.session_id
            and binding.transport_epoch == self.transport_epoch
            and binding.capability_report_hash == self.capability_report_hash
            and binding.turn_id == self._active_turn_id
            and binding.generation_id == self._active_generation_id
            and binding.generation_id not in self._cancelled_generation_ids
        )

    def _clear_generation_entries(self) -> None:
        self._queued.clear()
        self._queued_by_event_id.clear()
        self._receipts_by_event_id.clear()

    def _assert_open(self) -> None:
        if self._closed:
            raise RuntimeError("The playback receipt verifier is closed")


@dataclass(slots=True)
class _FragmentRange:
    binding: PlaybackBinding
    sample_start: int | None = None
    sample_end: int | None = None
    sample_rate: int | None = None
    invalid: bool = False

    def add(self, sample_range: PcmSampleRange) -> None:
        if self.invalid:
            return
        if self.sample_rate is not None and self.sample_rate != sample_range.sample_rate:
            self.invalid = True
            return
        if self.sample_end is not None and sample_range.sample_start != self.sample_end:
            self.invalid = True
            return
        if self.sample_start is None:
            self.sample_start = sample_range.sample_start
        self.sample_end = sample_range.sample_end
        self.sample_rate = sample_range.sample_rate

    def finish(self) -> PcmSampleRange | None:
        if (
            self.invalid
            or self.sample_start is None
            or self.sample_end is None
            or self.sample_rate is None
        ):
            return None
        return PcmSampleRange(self.sample_start, self.sample_end, self.sample_rate)


class SmallWebRTCPlaybackCursor:
    """Capture exact PTS ranges accepted by pinned Pipecat's SmallWebRTC writer."""

    def __init__(self) -> None:
        self._client: Any = None
        self._original_write: Callable[..., Any] | None = None
        self._wrapper: Callable[..., Any] | None = None
        self._fragments: dict[str, _FragmentRange] = {}
        self._active_fragment_id: str | None = None
        self._closed = False
        self._halted = False
        self._active_writes = 0
        self._active_write_tasks: dict[asyncio.Task[Any], int] = {}
        self._writes_drained = asyncio.Event()
        self._writes_drained.set()
        self._unsupported_reason: str | None = None

    @classmethod
    def install(cls, transport: Any) -> SmallWebRTCPlaybackCursor:
        """Install before connection setup; reject unknown Pipecat versions/shapes."""
        try:
            installed_version = version("pipecat-ai")
        except PackageNotFoundError as exc:
            raise RuntimeError("Cannot verify the pinned pipecat-ai version") from exc
        if installed_version != PIPECAT_SMALL_WEBRTC_VERSION:
            raise RuntimeError(
                f"Playback cursor supports pipecat-ai {PIPECAT_SMALL_WEBRTC_VERSION}, "
                f"not {installed_version}"
            )
        client = getattr(transport, "_client", None)
        if (
            client is None
            or not callable(getattr(client, "write_audio_frame", None))
            or not hasattr(client, "_audio_output_track")
            or getattr(client, "_voice_playback_cursor", None) is not None
        ):
            raise RuntimeError("Unrecognized SmallWebRTC output transport shape")

        cursor = cls()
        cursor._client = client
        cursor._original_write = client.write_audio_frame
        original_write = cursor._original_write

        async def instrumented_write(frame: Any) -> bool:
            cursor._begin_write()
            try:
                return await cursor._write_frame(frame, client, original_write)
            finally:
                cursor._end_write()

        cursor._wrapper = instrumented_write
        try:
            client._voice_playback_cursor = cursor
            client.write_audio_frame = instrumented_write
        except (AttributeError, TypeError) as exc:
            if getattr(client, "_voice_playback_cursor", None) is cursor:
                delattr(client, "_voice_playback_cursor")
            raise RuntimeError("SmallWebRTC output writer cannot be instrumented") from exc
        return cursor

    @property
    def supported(self) -> bool:
        return (
            not self._closed
            and not self._halted
            and self._unsupported_reason is None
        )

    @property
    def unsupported_reason(self) -> str | None:
        return self._unsupported_reason

    @property
    def halted(self) -> bool:
        return self._halted

    def halt(self) -> None:
        """Reject future writes and cancel all writers already awaiting output."""
        if self._closed:
            raise RuntimeError("Cannot halt a closed playback cursor")
        if self._halted:
            return
        self._halted = True
        for task in tuple(self._active_write_tasks):
            task.cancel()

    async def wait_for_inflight_writes(self) -> None:
        """Wait until every writer admitted before halt has actually settled."""
        if not self._halted:
            raise RuntimeError("Playback cursor must be halted before draining")
        await self._writes_drained.wait()

    def begin_fragment(self, binding: PlaybackBinding) -> None:
        if self._halted:
            raise RuntimeError("Playback cursor is halted")
        if not self.supported:
            raise RuntimeError(self._unsupported_reason or "Playback cursor is closed")
        if not isinstance(binding, PlaybackBinding):
            raise ValueError("The playback binding has an invalid type")
        if binding.fragment_id in self._fragments:
            self._invalidate_fragment(binding.fragment_id)
            raise RuntimeError("A playback fragment was already active")
        if len(self._fragments) >= MAX_TRACKED_FRAGMENTS:
            self._unsupported_reason = "fragment ledger limit exceeded"
            self._fragments.clear()
            self._active_fragment_id = None
            raise RuntimeError(self._unsupported_reason)
        if self._active_fragment_id is not None:
            self._invalidate_active_fragment()
            self._unsupported_reason = "overlapping playback fragments"
            self._fragments.clear()
            self._active_fragment_id = None
            raise RuntimeError(self._unsupported_reason)
        self._fragments[binding.fragment_id] = _FragmentRange(binding)
        self._active_fragment_id = binding.fragment_id

    def finish_fragment(self, binding: PlaybackBinding) -> PcmSampleRange | None:
        if self._halted:
            return None
        item = self._fragments.pop(binding.fragment_id, None)
        if item is None or item.binding != binding:
            return None
        if self._active_fragment_id == binding.fragment_id:
            self._active_fragment_id = None
        return item.finish()

    def cancel_generation(self, generation_id: str) -> PlaybackCancellation | None:
        if self._closed:
            return None
        matching = [
            self._fragments.pop(fragment_id)
            for fragment_id, item in tuple(self._fragments.items())
            if item.binding.generation_id == generation_id
        ]
        ranges = tuple(item.finish() for item in matching)
        concrete_ranges = tuple(item for item in ranges if item is not None)
        if self._active_fragment_id not in self._fragments:
            self._active_fragment_id = None
        if not matching:
            return None
        first = matching[0].binding
        return PlaybackCancellation(
            session_id=first.session_id,
            transport_epoch=first.transport_epoch,
            turn_id=first.turn_id,
            generation_id=generation_id,
            media_ranges=concrete_ranges,
        )

    def close(self) -> None:
        if self._halted or self._closed:
            return
        self._closed = True
        client = self._client
        if client is not None and getattr(client, "_voice_playback_cursor", None) is self:
            if getattr(client, "write_audio_frame", None) is self._wrapper:
                client.write_audio_frame = self._original_write
            delattr(client, "_voice_playback_cursor")
        self._fragments.clear()
        self._active_fragment_id = None

    def _begin_write(self) -> None:
        if self._halted:
            raise RuntimeError("Playback cursor is halted")
        if self._active_writes >= MAX_ACTIVE_OUTPUT_WRITES:
            self._unsupported_reason = "in-flight output writer limit exceeded"
            self._fragments.clear()
            self._active_fragment_id = None
            self.halt()
            raise RuntimeError(self._unsupported_reason)
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Playback writes must run in an asyncio task")
        self._active_write_tasks[task] = self._active_write_tasks.get(task, 0) + 1
        self._active_writes += 1
        self._writes_drained.clear()

    def _end_write(self) -> None:
        task = asyncio.current_task()
        if task is None or task not in self._active_write_tasks:
            raise RuntimeError("Playback cursor lost an active writer task")
        task_count = self._active_write_tasks[task]
        if task_count == 1:
            del self._active_write_tasks[task]
        else:
            self._active_write_tasks[task] = task_count - 1
        self._active_writes -= 1
        if self._active_writes == 0:
            self._writes_drained.set()

    async def _write_frame(
        self,
        frame: Any,
        client: Any,
        original_write: Callable[..., Any],
    ) -> bool:
        if self._closed:
            return await original_write(frame)
        binding = self._frame_binding(frame)
        if binding is None:
            self._invalidate_active_fragment()
            return await original_write(frame)
        track = getattr(client, "_audio_output_track", None)
        actual_range = self._next_track_range(frame, track)
        if actual_range is None:
            raise RuntimeError(
                "Playback cursor cannot capture the outgoing audio sample range"
            )
        try:
            accepted = await original_write(frame)
        except BaseException:
            self._invalidate_fragment(binding.fragment_id)
            raise
        if not accepted:
            self._invalidate_fragment(binding.fragment_id)
            return accepted
        if getattr(client, "_audio_output_track", None) is not track:
            self._unsupported_reason = (
                "SmallWebRTC output track changed during an audio write"
            )
            self._fragments.clear()
            self._active_fragment_id = None
            raise RuntimeError(self._unsupported_reason)
        self._record(binding, actual_range)
        return accepted

    def _frame_binding(self, frame: Any) -> PlaybackBinding | None:
        metadata = getattr(frame, "metadata", None)
        flow_fragment = metadata.get("flow_fragment") if isinstance(metadata, Mapping) else None
        if not isinstance(flow_fragment, Mapping):
            return None
        candidate: PlaybackBinding | None = None
        for item in self._fragments.values():
            try:
                candidate = PlaybackBinding.from_fragment_metadata(
                    flow_fragment, item.binding.capability_report_hash
                )
            except (TypeError, ValueError):
                continue
            if candidate.fragment_id != item.binding.fragment_id:
                candidate = None
                continue
            if candidate != item.binding:
                self._invalidate_fragment(item.binding.fragment_id)
                return None
            return item.binding
        return None

    def _next_track_range(self, frame: Any, track: Any) -> PcmSampleRange | None:
        if self._closed or self._unsupported_reason is not None:
            return None
        if track is None:
            self._unsupported_reason = "SmallWebRTC audio output track is unavailable"
            self._fragments.clear()
            self._active_fragment_id = None
            return None
        try:
            sample_rate = track._sample_rate
            timestamp = track._timestamp
            samples_per_chunk = track._samples_per_10ms
            bytes_per_chunk = track._bytes_per_10ms
            queued_chunks = len(track._chunk_queue)
            audio = frame.audio
            num_channels = frame.num_channels
            frame_rate = frame.sample_rate
            if (
                isinstance(sample_rate, bool)
                or not isinstance(sample_rate, int)
                or not _MIN_SAMPLE_RATE <= sample_rate <= _MAX_SAMPLE_RATE
                or isinstance(timestamp, bool)
                or not isinstance(timestamp, int)
                or not 0 <= timestamp <= _MAX_SAFE_INTEGER
                or isinstance(samples_per_chunk, bool)
                or not isinstance(samples_per_chunk, int)
                or samples_per_chunk != sample_rate * 10 // 1000
                or isinstance(bytes_per_chunk, bool)
                or not isinstance(bytes_per_chunk, int)
                or bytes_per_chunk != samples_per_chunk * 2
                or isinstance(queued_chunks, bool)
                or not isinstance(queued_chunks, int)
                or queued_chunks < 0
                or not isinstance(audio, (bytes, bytearray, memoryview))
                or isinstance(num_channels, bool)
                or not isinstance(num_channels, int)
                or num_channels != 1
                or frame_rate != sample_rate
                or len(audio) == 0
                or len(audio) % bytes_per_chunk != 0
            ):
                raise ValueError("unrecognized RawAudioTrack cursor or PCM layout")
            start = timestamp + queued_chunks * samples_per_chunk
            sample_count = len(audio) // 2
            return PcmSampleRange(start, start + sample_count, sample_rate)
        except (AttributeError, TypeError, ValueError, OverflowError) as exc:
            self._unsupported_reason = str(exc) or "unrecognized RawAudioTrack cursor shape"
            self._fragments.clear()
            self._active_fragment_id = None
            return None

    def _record(self, binding: PlaybackBinding, sample_range: PcmSampleRange) -> None:
        if self._closed or self._unsupported_reason is not None:
            return
        item = self._fragments.get(binding.fragment_id)
        if item is None or item.binding != binding:
            return
        if self._active_fragment_id != binding.fragment_id:
            item.invalid = True
            return
        item.add(sample_range)

    def _invalidate_fragment(self, fragment_id: str) -> None:
        item = self._fragments.get(fragment_id)
        if item is not None:
            item.invalid = True

    def _invalidate_active_fragment(self) -> None:
        if self._active_fragment_id is not None:
            self._invalidate_fragment(self._active_fragment_id)


def _parse_binding(raw: object) -> PlaybackBinding:
    mapping = _exact_mapping(raw, frozenset(_BINDING_FIELDS), "binding")
    return PlaybackBinding(**{name: mapping[name] for name in _BINDING_FIELDS})  # type: ignore[arg-type]


def _parse_pcm_range(raw: object) -> PcmSampleRange:
    mapping = _exact_mapping(raw, _RANGE_KEYS, "media_samples")
    return PcmSampleRange(
        sample_start=_safe_int(mapping["sample_start"], "sample_start"),
        sample_end=_safe_int(mapping["sample_end"], "sample_end", minimum=1),
        sample_rate=_rate(mapping["sample_rate"], "sample_rate"),
    )


def _parse_rendered_range(raw: object) -> RenderedFrameRange:
    mapping = _exact_mapping(raw, _RENDERED_RANGE_KEYS, "rendered_frames")
    return RenderedFrameRange(
        frame_start=_safe_int(mapping["frame_start"], "frame_start"),
        frame_end=_safe_int(mapping["frame_end"], "frame_end", minimum=1),
        sample_rate=_rate(mapping["sample_rate"], "render_sample_rate"),
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _parse_receipt_message(
    raw_message: object,
) -> tuple[PlaybackBinding, PcmSampleRange, RenderedFrameRange, str] | None:
    try:
        if isinstance(raw_message, bytes):
            if len(raw_message) > MAX_PLAYBACK_CONTROL_BYTES:
                return None
            message = json.loads(raw_message, object_pairs_hook=_reject_duplicate_json_keys)
        elif isinstance(raw_message, str):
            if len(raw_message.encode("utf-8")) > MAX_PLAYBACK_CONTROL_BYTES:
                return None
            message = json.loads(
                raw_message, object_pairs_hook=_reject_duplicate_json_keys
            )
        else:
            message = raw_message
        mapping = _exact_mapping(message, _RECEIPT_KEYS, "receipt")
        if (
            mapping["type"] != "flow-playback-receipt"
            or type(mapping["version"]) is not int
            or mapping["version"] != 1
        ):
            return None
        binding = _parse_binding(mapping["binding"])
        media_samples = _parse_pcm_range(mapping["media_samples"])
        rendered_frames = _parse_rendered_range(mapping["rendered_frames"])
        evidence_id = _required_text(
            mapping["evidence_id"], "evidence_id", limit=_MAX_EVIDENCE_ID
        )
        return binding, media_samples, rendered_frames, evidence_id
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        OverflowError,
    ):
        return None
