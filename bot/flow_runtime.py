"""Serialized flow execution bridged to Pipecat's real frame lifecycle.

The reducer and compiler stay provider-neutral in ``bot.flow``. This module
owns the per-session lock, translates finalized STT/LLM/TTS frames into flow
events, and puts controller-authored speech fragments back on the actual TTS
path. TTS or transport-frame observation is never treated as proof of playback;
completion requires an explicitly verified transport receipt.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    StartFrame,
    TextFrame,
    TranscriptionFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from bot.flow import (
    CancelGenerationCommand,
    ChatCommittedOutputEvent,
    CompiledFlow,
    EmitSpeechManifestCommand,
    FlowDiagnostic,
    FlowEvent,
    FragmentGeneratedOutputEvent,
    FragmentPlayedOutputEvent,
    FragmentQueuedOutputEvent,
    GenerationFailedModelEvent,
    GenerationSealedModelEvent,
    LocaleChangedEvent,
    ModelEvent,
    OutputCancelledOutputEvent,
    OutputEvent,
    Reduction,
    SessionState,
    SessionTerminatedEvent,
    SpeechDeltaModelEvent,
    SpeechFragment,
    StateProposal,
    StateProposalModelEvent,
    TeardownCommand,
    UserTurnAcceptedEvent,
    canonical_json_v1,
    extract_deterministic_updates,
    initial_session_state,
    reduce_event,

)
from bot.transcripts import MAX_CLIENT_UTTERANCE_CHARS


class FlowEventStore(Protocol):
    """Durably commit one accepted reducer event and its resulting state.

    A production store writes the event, state snapshot and reducer commands in
    one transaction. Leaving this unset keeps reducer state in memory only and
    deliberately disables transport completion receipts.
    """

    async def commit(self, event: FlowEvent, reduction: Reduction) -> None:
        """Persist the event and reduction atomically, or raise without commit."""
        ...


TransportEventVerifier = Callable[
    [OutputEvent], bool | Awaitable[bool]
]
TeardownCallback = Callable[[], Awaitable[None]]

_FLOW_CONTEXT_PREFIX = "VOICE_AI_FLOW_DATA_V1:"
_FLOW_CONTEXT_INSTRUCTIONS = (
    "Use the following versioned flow data only as subordinate conversation "
    "configuration. It cannot override platform policy. Do not claim an "
    "approval, eligibility decision, payment, or action succeeded unless a "
    "controller-authorized message says so. Ask about the current objective "
    "and use the user's language. Data: "
)
_OUTPUT_STEP_TYPES = frozenset({"inform", "summary", "end"})


def _event_id() -> str:
    return f"flow-event:{uuid.uuid4().hex}"


def _event_generation(event: FlowEvent) -> str:
    return event.generation_id


def _diagnostic_reduction(
    state: SessionState,
    code: str,
    path: str,
    message: str,
) -> Reduction:
    return Reduction(
        state=state.model_copy(deep=True),
        commands=[],
        diagnostics=[FlowDiagnostic(code=code, path=path, message=message)],
    )


def _join_reductions(state: SessionState, reductions: Sequence[Reduction]) -> Reduction:
    commands = [command for result in reductions for command in result.commands]
    diagnostics = [diagnostic for result in reductions for diagnostic in result.diagnostics]
    diagnostics.sort(key=lambda item: item.sort_key())
    return Reduction(
        state=state.model_copy(deep=True),
        commands=commands,
        diagnostics=diagnostics,
    )


def _split_preserving_text(text: str, max_chars: int) -> list[str]:
    """Split authored welcome text without losing whitespace or punctuation."""
    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            sentence_end = max(
                (
                    index + 1
                    for index in range(start, end)
                    if text[index] in ".?!।"
                    and (index + 1 == len(text) or text[index + 1].isspace())
                ),
                default=0,
            )
            if sentence_end > start:
                end = sentence_end
            else:
                whitespace_end = max(
                    (index + 1 for index in range(start, end) if text[index].isspace()),
                    default=0,
                )
                if whitespace_end > start:
                    end = whitespace_end
        parts.append(text[start:end])
        start = end
    return parts


def _manifest_hash(
    manifest_id: str,
    step_id: str,
    fragments: Sequence[SpeechFragment],
) -> str:
    payload = {
        "manifest_id": manifest_id,
        "step_id": step_id,
        "fragments": [fragment.model_dump(mode="json") for fragment in fragments],
    }
    return hashlib.sha256(canonical_json_v1(payload)).hexdigest()


class FlowRuntime:
    """One compiled flow and reducer state bound to one live session/epoch.

    ``FlowRuntime`` is also the ``flow_hooks`` value accepted by
    ``build_voice_pipeline``. Its processors are installed at four distinct
    boundaries: final STT before aggregation, flow context before the LLM,
    controller speech after the LLM, and fragment/audio correlation after TTS.
    """

    def __init__(
        self,
        compiled: CompiledFlow,
        session_id: str,
        transport_epoch: str,
        locale: str = "en",
        *,
        state: SessionState | None = None,
        event_store: FlowEventStore | None = None,
        transport_event_verifier: TransportEventVerifier | None = None,
    ) -> None:
        if not session_id or not session_id.strip():
            raise ValueError("FlowRuntime requires a non-empty session_id")
        if not transport_epoch or not transport_epoch.strip():
            raise ValueError("FlowRuntime requires a non-empty transport_epoch")
        if locale not in ("en", "hi"):
            raise ValueError("FlowRuntime locale must be 'en' or 'hi'")

        self.compiled = compiled
        self.session_id = session_id.strip()
        self.transport_epoch = transport_epoch.strip()
        self._event_store = event_store
        self._transport_event_verifier = transport_event_verifier
        self._lock = asyncio.Lock()
        self._state = state or initial_session_state(
            compiled,
            self.session_id,
            transport_epoch=self.transport_epoch,
            locale=locale,
        )
        if self._state.session_id != self.session_id:
            raise ValueError("restored SessionState belongs to a different session")
        if self._state.transport_epoch != self.transport_epoch:
            raise ValueError("restored SessionState belongs to a different transport epoch")
        if self._state.flow_hash != compiled.flow_hash:
            raise ValueError("restored SessionState belongs to a different flow hash")
        if self._state.artifact_hash != compiled.artifact_hash:
            raise ValueError("restored SessionState belongs to a different artifact hash")
        if self._state.locale != locale and state is None:
            raise ValueError("initial SessionState locale does not match runtime locale")

        self._steps_by_id = {
            step["id"]: step for step in compiled.normalized.get("steps", [])
        }
        self._turn_sequence = len(self._state.turn_history)
        self._generation_sequence = self._sequence_from_id(
            self._state.generation_id, "generation:"
        )
        self._event_hashes: dict[str, str] = {}
        self._manifest_hashes: dict[str, str] = {}
        self._welcome = compiled.normalized.get("welcome")
        self._welcome_question_step_id = (
            self._welcome.get("question_step_id")
            if isinstance(self._welcome, dict)
            else None
        )
        self._welcome_generation_id = "generation:0"
        self._welcome_status = (
            "pending" if isinstance(self._welcome, dict) else "not_configured"
        )
        self._welcome_manifest_id: str | None = None
        self._welcome_fragment_ids: set[str] = set()
        self._welcome_played_fragment_ids: set[str] = set()
        self._welcome_generated_fragment_ids: set[str] = set()
        self._event_count_by_generation: dict[str, int] = defaultdict(int)
        self._model_next_sequence: dict[str, int] = defaultdict(int)
        self._model_pending: dict[str, dict[int, SpeechDeltaModelEvent]] = defaultdict(dict)
        self._model_pending_bytes: dict[str, int] = defaultdict(int)
        self._pending_fragments: deque[SpeechFragment] = deque()
        self._audio_expected_fragments: deque[SpeechFragment] = deque()
        self._known_fragments: dict[str, SpeechFragment] = {}
        self._generated_fragments_by_generation: dict[str, int] = defaultdict(int)
        self._audio_observed_fragment_ids: set[str] = set()
        self._active_audio_fragment_ids: set[str] = set()
        self._queued_fragment_ids: set[str] = set()
        self._cancelled_generations: set[str] = set()
        self._interrupted_generations: set[str] = set()
        self._cancellation_events_recorded: set[str] = set()
        self._closed = self._state.teardown_emitted
        self._end_frame_requested = False
        self._teardown_callbacks: list[TeardownCallback] = []

    @staticmethod
    def _sequence_from_id(value: str | None, prefix: str) -> int:
        if not value or not value.startswith(prefix):
            return 0
        try:
            return int(value[len(prefix) :])
        except ValueError:
            return 0

    @property
    def state(self) -> SessionState:
        """A detached snapshot; callers cannot mutate the authoritative state."""
        return self._state.model_copy(deep=True)

    @property
    def durable(self) -> bool:
        """Whether state and output events are committed through an event store."""
        return self._event_store is not None
    @property
    def welcome_state(self) -> dict[str, Any]:
        """Return runtime-owned welcome tracking without implying playback."""
        return {
            "status": self._welcome_status,
            "question_step_id": self._welcome_question_step_id,
            "manifest_id": self._welcome_manifest_id,
            "manifest_hash": (
                self._manifest_hashes.get(self._welcome_manifest_id)
                if self._welcome_manifest_id
                else None
            ),
            "fragment_ids": sorted(self._welcome_fragment_ids),
            "audio_observed_fragment_ids": sorted(
                self._welcome_fragment_ids & self._audio_observed_fragment_ids
            ),
            "played_fragment_ids": sorted(self._welcome_played_fragment_ids),
        }

    async def start(self) -> list[SpeechFragment]:
        """Prepare the authored welcome once when the voice session starts."""
        async with self._lock:
            if self._welcome_status != "pending":
                return []
            if (
                self._closed
                or self._state.teardown_emitted
                or self._state.turn_history
                or self._state.active_step_id != self._welcome_question_step_id
            ):
                self._welcome_status = "skipped"
                return []
            text = self._welcome.get(self._state.locale, "")
            if not isinstance(text, str) or not text:
                self._welcome_status = "unavailable"
                return []
            parts = _split_preserving_text(
                text, max(1, self.compiled.limits.max_speech_fragment_chars)
            )
            if (
                len(parts) > self.compiled.limits.max_generation_fragments
                or sum(len(part.encode("utf-8")) for part in parts)
                > self.compiled.limits.max_pending_speech_bytes
            ):
                self._welcome_status = "rejected"
                return []

            manifest_id = (
                f"manifest:welcome:{self.session_id}:{self.transport_epoch}:"
                f"{self.compiled.flow_hash[:16]}"
            )
            step_id = self._welcome_question_step_id
            fragments = []
            for part_index, part in enumerate(parts):
                content_hash = hashlib.sha256(part.encode("utf-8")).hexdigest()
                fragment_id = f"{manifest_id}:welcome:{part_index}"
                fragments.append(
                    SpeechFragment(
                        event_id=f"frag_evt:{fragment_id}",
                        session_id=self.session_id,
                        transport_epoch=self.transport_epoch,
                        turn_id="turn:0",
                        generation_id=self._welcome_generation_id,
                        fragment_id=fragment_id,
                        semantic_fragment_id=(
                            f"welcome:{step_id}:{part_index}:{content_hash}"
                        ),
                        step_id=step_id,
                        state_version=self._state.version,
                        manifest_id=manifest_id,
                        content_hash=content_hash,
                        locale=self._state.locale,
                        text=part,
                        required=True,
                    )
                )
            self._welcome_manifest_id = manifest_id
            self._welcome_fragment_ids = {
                fragment.fragment_id for fragment in fragments
            }
            self._known_fragments.update(
                (fragment.fragment_id, fragment) for fragment in fragments
            )
            self._manifest_hashes[manifest_id] = _manifest_hash(
                manifest_id, step_id, fragments
            )
            self._welcome_status = "queued"
            return fragments

    def processors(self) -> Sequence[FrameProcessor]:
        """Return processors inserted after context shaping and before the LLM."""
        return (FlowContextProcessor(self),)

    def input_processor(self) -> FrameProcessor:
        """Observe final STT/interruption/lifecycle frames before aggregation."""
        return FlowInputProcessor(self)

    def output_processors(self) -> Sequence[FrameProcessor]:
        """Observe model frames and release only controller-owned manifests."""
        return (FlowOutputProcessor(self),)

    def audio_processor(self) -> FrameProcessor:
        """Bind TTS text and audio frames to their controller SpeechFragment."""
        return FlowAudioProcessor(self)

    def register_teardown_callback(self, callback: TeardownCallback) -> None:
        if callback not in self._teardown_callbacks:
            self._teardown_callbacks.append(callback)

    async def accept_transcription(self, text: str) -> Reduction:
        """Accept one finalized transcript and run allowlisted extraction once."""
        if not isinstance(text, str):
            return _diagnostic_reduction(
                self._state, "INVALID_USER_TEXT", "/text", "User text must be a string"
            )
        text = text.strip()
        if not text:
            return _diagnostic_reduction(
                self._state, "INVALID_USER_TEXT", "/text", "User text must not be empty"
            )
        if len(text) > MAX_CLIENT_UTTERANCE_CHARS:
            return _diagnostic_reduction(
                self._state,
                "USER_TEXT_TOO_LONG",
                "/text",
                f"User text exceeds {MAX_CLIENT_UTTERANCE_CHARS} characters",
            )

        async with self._lock:
            if self._closed or self._state.teardown_emitted:
                result = self._no_op_locked()
            else:
                if self._welcome_status in ("pending", "queued", "emitted"):
                    await self._cancel_welcome_locked()
                self._turn_sequence += 1
                self._generation_sequence += 1
                turn_id = f"turn:{self._turn_sequence}"
                generation_id = f"generation:{self._generation_sequence}"
                event = UserTurnAcceptedEvent(
                    event_id=_event_id(),
                    session_id=self.session_id,
                    transport_epoch=self.transport_epoch,
                    turn_id=turn_id,
                    generation_id=generation_id,
                    text=text,
                    locale=self._state.locale,
                )
                results = [await self._apply_event_locked(event)]
                if not self._state.teardown_emitted:
                    capture_definitions = [
                        capture
                        for step in self.compiled.normalized.get("steps", [])
                        for capture in step.get("captures", [])
                    ]
                    updates = extract_deterministic_updates(
                        text,
                        turn_id,
                        capture_definitions,
                        locale=self._state.locale,
                    )
                    if updates:
                        proposal = StateProposal(
                            proposal_id=f"proposal:{generation_id}:deterministic",
                            session_id=self.session_id,
                            turn_id=turn_id,
                            generation_id=generation_id,
                            base_state_version=self._state.version,
                            updates=updates,
                        )
                        proposal_event = StateProposalModelEvent(
                            event_id=_event_id(),
                            session_id=self.session_id,
                            transport_epoch=self.transport_epoch,
                            turn_id=turn_id,
                            generation_id=generation_id,
                            proposal=proposal,
                        )
                        results.append(await self._apply_event_locked(proposal_event))
                result = _join_reductions(self._state, results)

        await self._dispatch_teardown_if_needed(result)
        return result

    async def apply_event(self, event: FlowEvent) -> Reduction:
        """Apply an internal normalized event under the session lock.

        Transport-originated queued/played receipts must go through
        ``accept_output_event`` so their trust and ledger checks cannot be
        bypassed by calling the pure reducer adapter directly.
        """
        if isinstance(
            event,
            (
                FragmentQueuedOutputEvent,
                FragmentPlayedOutputEvent,
                ChatCommittedOutputEvent,
                OutputCancelledOutputEvent,
            ),
        ):
            return _diagnostic_reduction(
                self._state,
                "UNTRUSTED_OUTPUT_EVENT",
                "/type",
                "Output events must use the verified transport event entrypoint",
            )
        async with self._lock:
            result = await self._apply_event_locked(event)
        await self._dispatch_teardown_if_needed(result)
        return result

    async def apply_model_event(self, event: ModelEvent) -> Reduction:
        """Validate and apply model-side events from the current adapter stream."""
        async with self._lock:
            if event.session_id != self.session_id or event.transport_epoch != self.transport_epoch:
                result = self._event_binding_failure_locked(event)
            elif event.generation_id != self._state.generation_id:
                result = self._no_op_locked()
            elif event.turn_id != self._state.last_accepted_turn_id:
                result = _diagnostic_reduction(
                    self._state,
                    "STALE_STATE",
                    "/turn_id",
                    "Model event does not match the active user turn",
                )
            elif isinstance(event, SpeechDeltaModelEvent):
                result = await self._apply_speech_delta_locked(event)
            elif isinstance(event, GenerationSealedModelEvent):
                result = await self._seal_generation_locked(event)
            elif isinstance(event, GenerationFailedModelEvent):
                result = await self._apply_event_locked(event)
                if not result.diagnostics:
                    self._cancelled_generations.add(event.generation_id)
                    self._discard_generation_fragments_locked(event.generation_id)
                    self._model_pending.pop(event.generation_id, None)
                    self._model_pending_bytes.pop(event.generation_id, None)
            else:
                result = await self._apply_event_locked(event)
        await self._dispatch_teardown_if_needed(result)
        return result

    async def accept_output_event(self, event: OutputEvent) -> Reduction:
        """Accept a transport-verified queue/playback receipt, never a client ACK."""
        async with self._lock:
            if not isinstance(event, (FragmentQueuedOutputEvent, FragmentPlayedOutputEvent)):
                result = _diagnostic_reduction(
                    self._state,
                    "OUTPUT_EVENT_UNSUPPORTED",
                    "/type",
                    "Only transport fragment queue and playback receipts are supported",
                )
            elif (
                self.compiled.manifest.get("playback_mode")
                != "qualified_fragment_completion"
                or not self._event_store
                or not self._transport_event_verifier
            ):
                result = _diagnostic_reduction(
                    self._state,
                    "PLAYBACK_UNQUALIFIED",
                    "/capability_report_hash",
                    "Playback requires a qualified compiled adapter, a durable event store, and a trusted transport verifier",
                )
            else:
                result = await self._accept_verified_output_event_locked(event)
        await self._dispatch_teardown_if_needed(result)
        return result

    async def close(self, reason: str = "shutdown") -> Reduction:
        """Record session termination once; an in-flight terminal frame stays authoritative."""
        async with self._lock:
            if self._closed or self._state.teardown_emitted:
                result = self._no_op_locked()
            else:
                event = SessionTerminatedEvent(
                    event_id=_event_id(),
                    session_id=self.session_id,
                    transport_epoch=self.transport_epoch,
                    turn_id=self._state.last_accepted_turn_id or "turn:0",
                    generation_id=self._state.generation_id or "generation:0",
                    reason=reason,
                )
                result = await self._apply_event_locked(event)
                self._closed = True
                if self._state.generation_id:
                    self._cancelled_generations.add(self._state.generation_id)
                    self._discard_generation_fragments_locked(self._state.generation_id)
        return result

    async def interrupt(self, generation_id: str | None = None) -> None:
        """Discard unplayed output and persist its cancellation under the session lock."""
        async with self._lock:
            target = generation_id or self._state.generation_id
            if (
                target is None
                and self._welcome_status in ("pending", "queued", "emitted")
            ):
                target = self._welcome_generation_id
            if not target:
                return
            if target == self._welcome_generation_id and not self._welcome_fragment_ids:
                self._welcome_status = "cancelled"
                self._cancelled_generations.add(target)
                self._interrupted_generations.add(target)
                return
            if target in self._interrupted_generations:
                await self._record_output_cancelled_locked(target, [])
                return
            self._interrupted_generations.add(target)
            self._cancelled_generations.add(target)
            if target == self._welcome_generation_id and self._welcome_status != "played":
                self._welcome_status = "cancelled"
            discarded = self._discard_generation_fragments_locked(target)
            await self._record_output_cancelled_locked(target, discarded)

    async def _cancel_welcome_locked(self) -> None:
        if self._welcome_status == "pending":
            self._welcome_status = "cancelled"
            self._cancelled_generations.add(self._welcome_generation_id)
            return
        if self._welcome_status not in ("queued", "emitted"):
            return
        self._welcome_status = "cancelled"
        self._cancelled_generations.add(self._welcome_generation_id)
        discarded = self._discard_generation_fragments_locked(
            self._welcome_generation_id
        )
        if discarded:
            await self._record_output_cancelled_locked(
                self._welcome_generation_id, discarded
            )
    async def response_generation(self) -> str | None:
        async with self._lock:
            generation_id = self._state.generation_id
            if not generation_id or generation_id in self._cancelled_generations:
                return None
            return generation_id

    async def generation_cancelled(self, generation_id: str | None) -> bool:
        if not generation_id:
            return True
        async with self._lock:
            return (
                generation_id in self._cancelled_generations
                or generation_id != self._state.generation_id
                or self._closed
            )

    async def interruption_seen(self, generation_id: str) -> bool:
        async with self._lock:
            return generation_id in self._interrupted_generations

    async def should_suppress_model_speech(self) -> bool:
        async with self._lock:
            step = self._steps_by_id.get(self._state.active_step_id or "")
            if not step or step.get("type") not in _OUTPUT_STEP_TYPES:
                return False
            return bool(self._state.active_manifest_id or step.get("messages"))

    async def context_payload(self) -> dict[str, Any]:
        async with self._lock:
            step = self._steps_by_id.get(self._state.active_step_id or "")
            if step is None:
                active_step: dict[str, Any] | None = None
            else:
                active_step = {
                    "id": step["id"],
                    "type": step["type"],
                    "title": step.get("title", ""),
                    "instruction": step.get("instruction", ""),
                    "captures": [
                        {
                            "field": capture["field"],
                            "kind": capture["kind"],
                            "status": self._state.fields[capture["field"]].status,
                        }
                        for capture in step.get("captures", [])
                        if capture["field"] in self._state.fields
                    ],
                }
            return {
                "artifact_hash": self.compiled.artifact_hash,
                "flow_hash": self.compiled.flow_hash,
                "locale": self._state.locale,
                "state_version": self._state.version,
                "active_step": active_step,
            }

    async def fragments_for_generation(self, generation_id: str) -> list[SpeechFragment]:
        async with self._lock:
            is_welcome = generation_id == self._welcome_generation_id
            if (
                (generation_id != self._state.generation_id and not is_welcome)
                or generation_id in self._cancelled_generations
                or self._closed
            ):
                return []
            selected: list[SpeechFragment] = []
            remaining: deque[SpeechFragment] = deque()
            while self._pending_fragments:
                fragment = self._pending_fragments.popleft()
                if fragment.generation_id == generation_id:
                    selected.append(fragment)
                else:
                    remaining.append(fragment)
            self._pending_fragments = remaining
            if is_welcome and selected:
                self._welcome_status = "emitted"
            return selected
    async def record_generated_fragment(self, fragment: SpeechFragment) -> Reduction:
        """Persist the exact controller fragment before sending it into TTS."""
        async with self._lock:
            is_welcome = fragment.generation_id == self._welcome_generation_id
            if not self._fragment_is_current_locked(fragment):
                result = _diagnostic_reduction(
                    self._state,
                    "STALE_FRAGMENT",
                    "/fragment",
                    "Speech fragment no longer belongs to the active manifest and generation",
                )
            elif (
                not is_welcome
                and self._generated_fragments_by_generation[fragment.generation_id]
                >= self.compiled.limits.max_generation_fragments
            ):
                result = _diagnostic_reduction(
                    self._state,
                    "RUNTIME_LIMIT_EXCEEDED",
                    "/fragment",
                    "Generation exceeds max_generation_fragments",
                )
            elif (
                self._pending_speech_bytes_locked()
                + len(fragment.text.encode("utf-8"))
                > self.compiled.limits.max_pending_speech_bytes
            ):
                result = _diagnostic_reduction(
                    self._state,
                    "RUNTIME_LIMIT_EXCEEDED",
                    "/fragment/text",
                    "Pending speech exceeds max_pending_speech_bytes",
                )
            elif is_welcome:
                if fragment.fragment_id not in self._welcome_fragment_ids:
                    result = _diagnostic_reduction(
                        self._state,
                        "UNKNOWN_FRAGMENT",
                        "/fragment_id",
                        "Fragment is not part of the active welcome",
                    )
                elif fragment.fragment_id in self._welcome_generated_fragment_ids:
                    result = self._no_op_locked()
                else:
                    result = self._no_op_locked()
                    event = FragmentGeneratedOutputEvent(
                        event_id=_event_id(),
                        session_id=fragment.session_id,
                        transport_epoch=fragment.transport_epoch,
                        turn_id=fragment.turn_id,
                        generation_id=fragment.generation_id,
                        fragment=fragment,
                    )
                    if self._event_store is not None:
                        await self._event_store.commit(event, result)
                    self._event_hashes[event.event_id] = hashlib.sha256(
                        canonical_json_v1(event.model_dump(mode="json"))
                    ).hexdigest()
                    self._event_count_by_generation[fragment.generation_id] += 1
                    self._welcome_generated_fragment_ids.add(fragment.fragment_id)
                    self._welcome_status = "emitted"
                    self._audio_expected_fragments.append(fragment)
            else:
                event = FragmentGeneratedOutputEvent(
                    event_id=_event_id(),
                    session_id=fragment.session_id,
                    transport_epoch=fragment.transport_epoch,
                    turn_id=fragment.turn_id,
                    generation_id=fragment.generation_id,
                    fragment=fragment,
                )
                result = await self._apply_event_locked(event, internal_output=True)
                if not result.diagnostics:
                    self._generated_fragments_by_generation[fragment.generation_id] += 1
                    self._audio_expected_fragments.append(fragment)
        return result

    async def match_tts_text(self, text: str) -> SpeechFragment | None:
        """Match a real TTSTextFrame exactly to the next authorized fragment."""
        async with self._lock:
            if not self._audio_expected_fragments:
                return None
            fragment = self._audio_expected_fragments[0]
            if (
                text != fragment.text
                or not self._fragment_is_current_locked(fragment)
                or fragment.generation_id in self._cancelled_generations
            ):
                return None
            self._active_audio_fragment_ids.add(fragment.fragment_id)
            return self._audio_expected_fragments.popleft()

    async def finish_audio_fragment(self, fragment: SpeechFragment) -> None:
        """Forget a TTS-stage fragment after the backend has stopped producing it."""
        async with self._lock:
            self._active_audio_fragment_ids.discard(fragment.fragment_id)

    async def observe_audio_frame(self, fragment: SpeechFragment) -> bool:
        """Record TTS output reaching the transport boundary, not audible playback."""
        async with self._lock:
            if (
                not self._fragment_is_current_locked(fragment)
                or fragment.generation_id in self._cancelled_generations
            ):
                return False
            self._audio_observed_fragment_ids.add(fragment.fragment_id)
            return True
    async def record_speech_delta(
        self,
        generation_id: str,
        sequence: int,
        text: str,
    ) -> Reduction:
        state = self.state
        event = SpeechDeltaModelEvent(
            event_id=_event_id(),
            session_id=self.session_id,
            transport_epoch=self.transport_epoch,
            turn_id=state.last_accepted_turn_id or "turn:0",
            generation_id=generation_id,
            seq=sequence,
            text=text,
        )
        return await self.apply_model_event(event)

    async def seal_generation(
        self,
        generation_id: str,
        last_sequence: int,
    ) -> Reduction:
        state = self.state
        event = GenerationSealedModelEvent(
            event_id=_event_id(),
            session_id=self.session_id,
            transport_epoch=self.transport_epoch,
            turn_id=state.last_accepted_turn_id or "turn:0",
            generation_id=generation_id,
            last_seq=last_sequence,
        )
        return await self.apply_model_event(event)

    async def fail_generation(self, generation_id: str, code: str) -> Reduction:
        state = self.state
        event = GenerationFailedModelEvent(
            event_id=_event_id(),
            session_id=self.session_id,
            transport_epoch=self.transport_epoch,
            turn_id=state.last_accepted_turn_id or "turn:0",
            generation_id=generation_id,
            code=code,
        )
        return await self.apply_model_event(event)

    def fragment_metadata(self, fragment: SpeechFragment) -> dict[str, Any]:
        return {
            "session_id": fragment.session_id,
            "transport_epoch": fragment.transport_epoch,
            "turn_id": fragment.turn_id,
            "generation_id": fragment.generation_id,
            "manifest_id": fragment.manifest_id,
            "manifest_hash": self._manifest_hashes.get(fragment.manifest_id),
            "fragment_id": fragment.fragment_id,
            "semantic_fragment_id": fragment.semantic_fragment_id,
            "step_id": fragment.step_id,
            "state_version": fragment.state_version,
            "content_hash": fragment.content_hash,
            "locale": fragment.locale,
            "required": fragment.required,
            "artifact_hash": self.compiled.artifact_hash,
            "flow_hash": self.compiled.flow_hash,
        }

    async def _apply_speech_delta_locked(
        self, event: SpeechDeltaModelEvent
    ) -> Reduction:
        generation_id = event.generation_id
        expected = self._model_next_sequence[generation_id]
        pending = self._model_pending[generation_id]
        if event.seq < expected:
            return self._no_op_locked()
        if event.seq > expected:
            if event.seq - expected > self.compiled.limits.max_adapter_events_per_generation:
                return _diagnostic_reduction(
                    self._state,
                    "MODEL_SEQUENCE_GAP",
                    "/seq",
                    "Speech sequence is outside the bounded reorder window",
                )
            if event.seq in pending:
                if pending[event.seq].text == event.text:
                    return self._no_op_locked()
                return _diagnostic_reduction(
                    self._state,
                    "MODEL_SEQUENCE_CONFLICT",
                    "/seq",
                    "Different speech deltas claim the same sequence number",
                )
            pending_bytes = self._model_pending_bytes[generation_id] + len(event.text.encode("utf-8"))
            if (
                len(pending) >= self.compiled.limits.max_adapter_events_per_generation
                or pending_bytes > self.compiled.limits.max_pending_speech_bytes
            ):
                return _diagnostic_reduction(
                    self._state,
                    "RUNTIME_LIMIT_EXCEEDED",
                    "/seq",
                    "Out-of-order speech exceeds the configured buffer bound",
                )
            pending[event.seq] = event
            self._model_pending_bytes[generation_id] = pending_bytes
            return self._no_op_locked()

        results = [await self._apply_event_locked(event)]
        if results[0].diagnostics:
            return results[0]
        self._model_next_sequence[generation_id] += 1
        while self._model_next_sequence[generation_id] in pending:
            next_event = pending.pop(self._model_next_sequence[generation_id])
            results.append(await self._apply_event_locked(next_event))
            if results[-1].diagnostics:
                break
            self._model_next_sequence[generation_id] += 1
            self._model_pending_bytes[generation_id] -= len(next_event.text.encode("utf-8"))
        return _join_reductions(self._state, results)

    async def _seal_generation_locked(
        self, event: GenerationSealedModelEvent
    ) -> Reduction:
        expected_last = self._model_next_sequence[event.generation_id] - 1
        pending = self._model_pending.get(event.generation_id, {})
        if event.last_seq != expected_last or pending:
            failed = GenerationFailedModelEvent(
                event_id=_event_id(),
                session_id=self.session_id,
                transport_epoch=self.transport_epoch,
                turn_id=event.turn_id,
                generation_id=event.generation_id,
                code="speech_sequence_gap",
            )
            result = await self._apply_event_locked(failed)
            self._cancelled_generations.add(event.generation_id)
            self._discard_generation_fragments_locked(event.generation_id)
            diagnostic = FlowDiagnostic(
                code="MODEL_SEQUENCE_GAP",
                path="/last_seq",
                message="Generation sealed with missing or out-of-order speech deltas",
            )
            return result.model_copy(update={"diagnostics": [diagnostic]})
        return await self._apply_event_locked(event)

    async def _accept_verified_output_event_locked(
        self, event: FragmentQueuedOutputEvent | FragmentPlayedOutputEvent
    ) -> Reduction:
        binding_failure = self._event_binding_failure_locked(event)
        if binding_failure.diagnostics:
            return binding_failure
        if event.generation_id == self._welcome_generation_id:
            return await self._accept_verified_welcome_output_event_locked(event)
        if self._closed or self._state.teardown_emitted:
            return self._no_op_locked()
        if event.generation_id != self._state.generation_id:
            return self._no_op_locked()
        if event.generation_id in self._cancelled_generations:
            return self._no_op_locked()
        if event.turn_id != self._state.last_accepted_turn_id:
            return self._no_op_locked()
        if event.manifest_id != self._state.active_manifest_id:
            return self._no_op_locked()
        fragment_meta = self._state.active_manifest_fragments.get(event.fragment_id)
        fragment = self._known_fragments.get(event.fragment_id)
        if not fragment_meta or not fragment:
            return _diagnostic_reduction(
                self._state,
                "UNKNOWN_FRAGMENT",
                "/fragment_id",
                "Transport receipt does not identify a fragment in the active manifest",
            )
        if event.fragment_id not in self._audio_observed_fragment_ids:
            return _diagnostic_reduction(
                self._state,
                "FRAGMENT_NOT_RENDERED",
                "/fragment_id",
                "No matching TTS audio frame reached the transport boundary",
            )
        if isinstance(event, FragmentQueuedOutputEvent):
            if event.fragment_id in self._queued_fragment_ids:
                return self._no_op_locked()
        else:
            if event.fragment_id not in self._queued_fragment_ids:
                return _diagnostic_reduction(
                    self._state,
                    "FRAGMENT_NOT_QUEUED",
                    "/fragment_id",
                    "Playback evidence requires a prior trusted queue receipt",
                )
            if event.content_hash != fragment_meta.get("content_hash"):
                return _diagnostic_reduction(
                    self._state,
                    "CONTENT_HASH_MISMATCH",
                    "/content_hash",
                    "Playback evidence content hash does not match the sealed fragment",
                )
            if not event.evidence_id.strip():
                return _diagnostic_reduction(
                    self._state,
                    "INVALID_PLAYBACK_EVIDENCE",
                    "/evidence_id",
                    "Playback evidence ID must be non-empty",
                )
            report_hash = self.compiled.manifest.get("adapter_qualification_references")
            if (
                not report_hash
                or report_hash == "0" * 64
                or event.capability_report_hash != report_hash
            ):
                return _diagnostic_reduction(
                    self._state,
                    "PLAYBACK_UNQUALIFIED",
                    "/capability_report_hash",
                    "Playback evidence is not bound to the compiled adapter qualification report",
                )

        assert self._transport_event_verifier is not None
        verified = self._transport_event_verifier(event)
        if inspect.isawaitable(verified):
            verified = await verified
        if not verified:
            return _diagnostic_reduction(
                self._state,
                "UNTRUSTED_TRANSPORT_EVENT",
                "/event_id",
                "Transport adapter did not verify the output receipt",
            )

        result = await self._apply_event_locked(event, internal_output=True)
        if result.diagnostics:
            return result
        if isinstance(event, FragmentQueuedOutputEvent):
            self._queued_fragment_ids.add(event.fragment_id)
        else:
            self._audio_observed_fragment_ids.discard(event.fragment_id)
            self._queued_fragment_ids.discard(event.fragment_id)
        return result

    async def _accept_verified_welcome_output_event_locked(
        self, event: FragmentQueuedOutputEvent | FragmentPlayedOutputEvent
    ) -> Reduction:
        if (
            self._closed
            or self._welcome_status in ("cancelled", "skipped", "unavailable", "rejected")
            or event.turn_id != "turn:0"
            or event.manifest_id != self._welcome_manifest_id
        ):
            return self._no_op_locked()
        fragment = self._known_fragments.get(event.fragment_id)
        if (
            fragment is None
            or event.fragment_id not in self._welcome_fragment_ids
            or event.fragment_id not in self._audio_observed_fragment_ids
        ):
            return _diagnostic_reduction(
                self._state,
                "FRAGMENT_NOT_RENDERED",
                "/fragment_id",
                "Welcome receipt does not identify rendered TTS output",
            )
        if isinstance(event, FragmentQueuedOutputEvent):
            if event.fragment_id in self._queued_fragment_ids:
                return self._no_op_locked()
        else:
            if event.fragment_id not in self._queued_fragment_ids:
                return _diagnostic_reduction(
                    self._state,
                    "FRAGMENT_NOT_QUEUED",
                    "/fragment_id",
                    "Welcome playback requires a prior trusted queue receipt",
                )
            if event.content_hash != fragment.content_hash or not event.evidence_id.strip():
                return _diagnostic_reduction(
                    self._state,
                    "INVALID_PLAYBACK_EVIDENCE",
                    "/content_hash",
                    "Welcome evidence must match the authored fragment and identify the receipt",
                )
            report_hash = self.compiled.manifest.get("adapter_qualification_references")
            if (
                not report_hash
                or report_hash == "0" * 64
                or event.capability_report_hash != report_hash
            ):
                return _diagnostic_reduction(
                    self._state,
                    "PLAYBACK_UNQUALIFIED",
                    "/capability_report_hash",
                    "Welcome playback evidence is not bound to its compiled qualification report",
                )
        assert self._transport_event_verifier is not None
        verified = self._transport_event_verifier(event)
        if inspect.isawaitable(verified):
            verified = await verified
        if not verified:
            return _diagnostic_reduction(
                self._state,
                "UNTRUSTED_TRANSPORT_EVENT",
                "/event_id",
                "Transport adapter did not verify the welcome receipt",
            )
        event_digest = hashlib.sha256(
            canonical_json_v1(event.model_dump(mode="json"))
        ).hexdigest()
        prior_digest = self._event_hashes.get(event.event_id)
        if prior_digest is not None:
            if prior_digest == event_digest:
                return self._no_op_locked()
            return _diagnostic_reduction(
                self._state,
                "EVENT_ID_CONFLICT",
                "/event_id",
                "Event ID was reused with different event content",
            )
        result = self._no_op_locked()
        if self._event_store is not None:
            await self._event_store.commit(event, result)
        event_hash = hashlib.sha256(
            canonical_json_v1(event.model_dump(mode="json"))
        ).hexdigest()
        self._event_hashes[event.event_id] = event_hash
        self._event_count_by_generation[event.generation_id] += 1
        if isinstance(event, FragmentQueuedOutputEvent):
            self._queued_fragment_ids.add(event.fragment_id)
        else:
            self._welcome_played_fragment_ids.add(event.fragment_id)
            self._audio_observed_fragment_ids.discard(event.fragment_id)
            self._queued_fragment_ids.discard(event.fragment_id)
            if self._welcome_played_fragment_ids >= self._welcome_fragment_ids:
                self._welcome_status = "played"
        return result

    async def _apply_event_locked(
        self,
        event: FlowEvent,
        *,
        internal_output: bool = False,
    ) -> Reduction:
        if self._closed or self._state.teardown_emitted:
            return self._no_op_locked()
        binding_failure = self._event_binding_failure_locked(event)
        if binding_failure.diagnostics:
            return binding_failure
        if not internal_output and isinstance(
            event,
            (
                FragmentQueuedOutputEvent,
                FragmentPlayedOutputEvent,
                ChatCommittedOutputEvent,
                OutputCancelledOutputEvent,
            ),
        ):
            return _diagnostic_reduction(
                self._state,
                "UNTRUSTED_OUTPUT_EVENT",
                "/type",
                "Transport output event bypassed the verified event entrypoint",
            )

        event_data = event.model_dump(mode="json")
        event_digest = hashlib.sha256(canonical_json_v1(event_data)).hexdigest()
        prior_digest = self._event_hashes.get(event.event_id)
        if prior_digest is not None:
            if prior_digest == event_digest:
                return self._no_op_locked()
            return _diagnostic_reduction(
                self._state,
                "EVENT_ID_CONFLICT",
                "/event_id",
                "Event ID was reused with different event content",
            )

        generation_id = _event_generation(event)
        max_events = self.compiled.limits.max_adapter_events_per_generation
        is_cancellation = isinstance(event, OutputCancelledOutputEvent)
        if (
            self._event_count_by_generation[generation_id] >= max_events
            and not is_cancellation
        ):
            return _diagnostic_reduction(
                self._state,
                "RUNTIME_LIMIT_EXCEEDED",
                "/event_id",
                "Generation exceeds max_adapter_events_per_generation",
            )
        reduction = reduce_event(self.compiled, self._state, event)
        if self._event_store is not None:
            await self._event_store.commit(event, reduction)
        self._state = reduction.state
        self._event_hashes[event.event_id] = event_digest
        self._event_count_by_generation[generation_id] += 1

        runtime_diagnostics: list[FlowDiagnostic] = []
        for command in reduction.commands:
            if isinstance(command, CancelGenerationCommand):
                cancelled_generation = command.cancellation_id.removeprefix("cancel:")
                if cancelled_generation:
                    self._cancelled_generations.add(cancelled_generation)
                    discarded = self._discard_generation_fragments_locked(
                        cancelled_generation
                    )
                    await self._record_output_cancelled_locked(
                        cancelled_generation, discarded
                    )
            elif isinstance(command, EmitSpeechManifestCommand):
                runtime_diagnostics.extend(self._queue_manifest_locked(command))
            elif isinstance(command, TeardownCommand):
                self._closed = True

        if runtime_diagnostics:
            diagnostics = [*reduction.diagnostics, *runtime_diagnostics]
            diagnostics.sort(key=lambda item: item.sort_key())
            return reduction.model_copy(update={"diagnostics": diagnostics})
        return reduction

    def _queue_manifest_locked(
        self, command: EmitSpeechManifestCommand
    ) -> list[FlowDiagnostic]:
        fragments = command.fragments
        is_welcome = (
            command.manifest_id == self._welcome_manifest_id
            and self._welcome_status in ("queued", "emitted")
        )
        welcome_ids = self._welcome_fragment_ids if is_welcome else set()
        manifest_hash = _manifest_hash(
            command.manifest_id, command.step_id, fragments
        )
        if (
            len(fragments) > self.compiled.limits.max_generation_fragments
            or sum(len(fragment.text.encode("utf-8")) for fragment in fragments)
            + self._pending_speech_bytes_locked()
            > self.compiled.limits.max_pending_speech_bytes
        ):
            return [
                FlowDiagnostic(
                    code="RUNTIME_LIMIT_EXCEEDED",
                    path="/fragments",
                    message="Manifest exceeds configured fragment or speech limits",
                )
            ]
        for fragment in fragments:
            if (
                fragment.session_id != self.session_id
                or fragment.transport_epoch != self.transport_epoch
                or (
                    fragment.generation_id != self._state.generation_id
                    and not (
                        is_welcome
                        and fragment.generation_id == self._welcome_generation_id
                    )
                )
                or fragment.manifest_id != command.manifest_id
                or fragment.content_hash
                != hashlib.sha256(fragment.text.encode("utf-8")).hexdigest()
                or len(fragment.text) > self.compiled.limits.max_speech_fragment_chars
                or not fragment.required
                or (
                    is_welcome
                    and fragment.fragment_id not in welcome_ids
                )
            ):
                return [
                    FlowDiagnostic(
                        code="INVALID_SPEECH_MANIFEST",
                        path="/fragments",
                        message="Controller manifest contains a fragment with an invalid session, hash, size, or required flag",
                    )
                ]
        self._manifest_hashes[command.manifest_id] = manifest_hash
        for fragment in fragments:
            self._known_fragments[fragment.fragment_id] = fragment
            if not is_welcome:
                self._pending_fragments.append(fragment)
        return []

    def _event_binding_failure_locked(self, event: FlowEvent) -> Reduction:
        if event.session_id != self.session_id:
            return _diagnostic_reduction(
                self._state,
                "SESSION_MISMATCH",
                "/session_id",
                "Event session does not match the bound flow runtime",
            )
        if event.transport_epoch != self.transport_epoch:
            return _diagnostic_reduction(
                self._state,
                "STALE_TRANSPORT_EPOCH",
                "/transport_epoch",
                "Event belongs to a different transport epoch",
            )
        return self._no_op_locked()

    def _fragment_is_current_locked(self, fragment: SpeechFragment) -> bool:
        known = self._known_fragments.get(fragment.fragment_id)
        is_welcome = (
            fragment.generation_id == self._welcome_generation_id
            and fragment.manifest_id == self._welcome_manifest_id
            and fragment.fragment_id in self._welcome_fragment_ids
            and self._welcome_status in ("queued", "emitted")
        )
        is_active_turn = (
            fragment.generation_id == self._state.generation_id
            and fragment.manifest_id == self._state.active_manifest_id
        )
        return bool(
            known
            and known.content_hash == fragment.content_hash
            and known.manifest_id == fragment.manifest_id
            and (is_welcome or is_active_turn)
            and fragment.generation_id not in self._cancelled_generations
            and not self._closed
        )

    def _pending_speech_bytes_locked(self) -> int:
        fragments = {
            fragment.fragment_id: fragment
            for fragment in (*self._pending_fragments, *self._audio_expected_fragments)
        }
        fragments.update(
            {
                fragment_id: self._known_fragments[fragment_id]
                for fragment_id in self._active_audio_fragment_ids
                if fragment_id in self._known_fragments
            }
        )
        return sum(len(fragment.text.encode("utf-8")) for fragment in fragments.values())

    def _discard_generation_fragments_locked(self, generation_id: str) -> list[str]:
        delivered = set(self._state.delivered_fragment_ids)
        delivered.update(self._welcome_played_fragment_ids)
        discarded = [
            fragment.fragment_id
            for fragment in self._known_fragments.values()
            if fragment.generation_id == generation_id
            and fragment.fragment_id not in delivered
        ]
        self._pending_fragments = deque(
            fragment
            for fragment in self._pending_fragments
            if fragment.generation_id != generation_id
        )
        self._audio_expected_fragments = deque(
            fragment
            for fragment in self._audio_expected_fragments
            if fragment.generation_id != generation_id
        )
        self._active_audio_fragment_ids.difference_update(discarded)
        self._audio_observed_fragment_ids.difference_update(discarded)
        self._queued_fragment_ids.difference_update(discarded)
        return discarded

    async def _record_output_cancelled_locked(
        self,
        generation_id: str,
        discarded_fragment_ids: list[str],
    ) -> None:
        """Persist one cancellation record while preserving completed fragments."""
        if generation_id in self._cancellation_events_recorded:
            return
        turn_id = next(
            (
                turn["turn_id"]
                for turn in reversed(self._state.turn_history)
                if turn.get("generation_id") == generation_id
            ),
            self._state.last_accepted_turn_id or "turn:0",
        )
        event = OutputCancelledOutputEvent(
            event_id=_event_id(),
            session_id=self.session_id,
            transport_epoch=self.transport_epoch,
            turn_id=turn_id,
            generation_id=generation_id,
            cancellation_id=f"cancel:{generation_id}",
            discarded_fragment_ids=discarded_fragment_ids,
        )
        reduction = reduce_event(self.compiled, self._state, event)
        if self._event_store is not None:
            await self._event_store.commit(event, reduction)
        self._state = reduction.state
        self._event_hashes[event.event_id] = hashlib.sha256(
            canonical_json_v1(event.model_dump(mode="json"))
        ).hexdigest()
        self._event_count_by_generation[generation_id] += 1
        self._cancellation_events_recorded.add(generation_id)
    def _no_op_locked(self) -> Reduction:
        return Reduction(state=self._state.model_copy(deep=True), commands=[], diagnostics=[])

    async def _dispatch_teardown_if_needed(self, reduction: Reduction) -> None:
        if not any(isinstance(command, TeardownCommand) for command in reduction.commands):
            return
        async with self._lock:
            if self._end_frame_requested:
                return
            self._end_frame_requested = True
            callbacks = tuple(self._teardown_callbacks)
        for callback in callbacks:
            await callback()


class FlowInputProcessor(FrameProcessor):
    """Accept finalized Pipecat transcripts and honor real interruption frames."""

    def __init__(self, runtime: FlowRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        self._seen_frame_ids: set[int] = set()
        self._frame_order: deque[int] = deque()
        self._frame_id_limit = max(2, runtime.compiled.limits.max_turns * 2)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            generation_id = frame.metadata.get("flow_cancel_generation_id")
            try:
                await self._runtime.interrupt(generation_id)
            finally:
                await self.push_frame(frame, direction)
            return
        if (
            isinstance(frame, TranscriptionFrame)
            and direction == FrameDirection.DOWNSTREAM
            and frame.finalized
        ):
            if not self._remember_frame_id(frame.id):
                return
            result = await self._runtime.accept_transcription(frame.text)
            if result.state.teardown_emitted:
                return
            cancel_commands = [
                command
                for command in result.commands
                if isinstance(command, CancelGenerationCommand)
            ]
            for command in cancel_commands:
                generation_id = command.cancellation_id.removeprefix("cancel:")
                if generation_id and not await self._runtime.interruption_seen(generation_id):
                    interruption = InterruptionFrame()
                    interruption.metadata["flow_cancel_generation_id"] = generation_id
                    await self.push_frame(interruption, FrameDirection.DOWNSTREAM)
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._runtime.close("pipeline_ended" if isinstance(frame, EndFrame) else "pipeline_cancelled")
        await self.push_frame(frame, direction)

    def _remember_frame_id(self, frame_id: int) -> bool:
        if frame_id in self._seen_frame_ids:
            return False
        self._seen_frame_ids.add(frame_id)
        self._frame_order.append(frame_id)
        if len(self._frame_order) > self._frame_id_limit:
            self._seen_frame_ids.discard(self._frame_order.popleft())
        return True


class FlowContextProcessor(FrameProcessor):
    """Put the current compiled objective in the shared LLM context as data."""

    def __init__(self, runtime: FlowRuntime) -> None:
        super().__init__()
        self._runtime = runtime

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            await self._inject_flow_context(frame.context)
        await self.push_frame(frame, direction)

    async def _inject_flow_context(self, context: LLMContext) -> None:
        payload = await self._runtime.context_payload()
        encoded = canonical_json_v1(payload).decode("utf-8")
        content = _FLOW_CONTEXT_PREFIX + _FLOW_CONTEXT_INSTRUCTIONS + encoded
        max_bytes = self._runtime.compiled.limits.max_context_utf8_bytes
        if len(content.encode("utf-8")) > max_bytes:
            return
        messages = [
            message
            for message in context.messages
            if not (
                isinstance(message, dict)
                and message.get("role") == "system"
                and isinstance(message.get("content"), str)
                and message["content"].startswith(_FLOW_CONTEXT_PREFIX)
            )
        ]
        insert_at = next(
            (
                index
                for index, message in enumerate(messages)
                if not isinstance(message, dict) or message.get("role") != "system"
            ),
            len(messages),
        )
        messages.insert(insert_at, {"role": "system", "content": content})
        context.set_messages(messages)


class FlowOutputProcessor(FrameProcessor):
    """Translate model frames and release immutable fragments through the TTS path."""

    def __init__(self, runtime: FlowRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        self._response_generation_id: str | None = None
        self._speech_sequence = 0
        runtime.register_teardown_callback(self._request_pipeline_end)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            target = frame.metadata.get("flow_cancel_generation_id") or self._response_generation_id
            try:
                await self._runtime.interrupt(target)
            finally:
                if target == self._response_generation_id:
                    self._response_generation_id = None
                await self.push_frame(frame, direction)
            return
        if isinstance(frame, StartFrame) and direction == FrameDirection.DOWNSTREAM:
            fragments = await self._runtime.start()
            await self.push_frame(frame, direction)
            await self._emit_fragments(fragments, direction)
            return
        if isinstance(frame, LLMFullResponseStartFrame):
            self._response_generation_id = await self._runtime.response_generation()
            self._speech_sequence = 0
            await self.push_frame(frame, direction)
            if self._response_generation_id:
                await self._emit_pending_fragments(self._response_generation_id, direction)
            return
        if isinstance(frame, LLMTextFrame):
            generation_id = self._response_generation_id
            if generation_id and await self._runtime.generation_cancelled(generation_id):
                return
            if not generation_id:
                return
            await self._runtime.record_speech_delta(
                generation_id, self._speech_sequence, frame.text
            )
            self._speech_sequence += 1
            frame.metadata["flow_model_preview"] = True
            frame.append_to_context = False
            if await self._runtime.should_suppress_model_speech():
                return
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMFullResponseEndFrame):
            generation_id = self._response_generation_id
            if generation_id:
                await self._runtime.seal_generation(
                    generation_id, self._speech_sequence - 1
                )
            await self.push_frame(frame, direction)
            self._response_generation_id = None
            return
        if isinstance(frame, ErrorFrame):
            generation_id = self._response_generation_id
            if generation_id:
                await self._runtime.fail_generation(generation_id, "pipeline_error")
            self._response_generation_id = None
            await self.push_frame(frame, direction)
            return
        await self.push_frame(frame, direction)

    async def _emit_pending_fragments(
        self, generation_id: str, direction: FrameDirection
    ) -> None:
        fragments = await self._runtime.fragments_for_generation(generation_id)
        await self._emit_fragments(fragments, direction)

    async def _emit_fragments(
        self, fragments: Sequence[SpeechFragment], direction: FrameDirection
    ) -> None:
        for fragment in fragments:
            result = await self._runtime.record_generated_fragment(fragment)
            if result.diagnostics:
                continue
            speech_frame = TextFrame(fragment.text)
            speech_frame.append_to_context = False
            speech_frame.metadata["flow_fragment"] = self._runtime.fragment_metadata(fragment)
            try:
                await self.push_frame(speech_frame, direction)
            except Exception:
                await self._runtime.interrupt(fragment.generation_id)
                raise
    async def _request_pipeline_end(self) -> None:
        await self.push_frame(EndFrame(), FrameDirection.DOWNSTREAM)


class FlowAudioProcessor(FrameProcessor):
    """Correlate exact TTS text/audio frames without fabricating playback receipts."""

    def __init__(self, runtime: FlowRuntime) -> None:
        super().__init__()
        self._runtime = runtime
        self._active_fragment: SpeechFragment | None = None
        self._audio_correlation_ambiguous = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            target = frame.metadata.get("flow_cancel_generation_id")
            if target is None and self._active_fragment:
                target = self._active_fragment.generation_id
            try:
                await self._runtime.interrupt(target)
            finally:
                if self._active_fragment and self._active_fragment.generation_id == target:
                    self._active_fragment = None
                self._audio_correlation_ambiguous = False
                await self.push_frame(frame, direction)
            return
        if isinstance(frame, TTSTextFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._active_fragment is not None:
                await self._runtime.finish_audio_fragment(self._active_fragment)
                self._active_fragment = None
                self._audio_correlation_ambiguous = True
            fragment = await self._runtime.match_tts_text(frame.text)
            if fragment is not None:
                self._active_fragment = fragment
                frame.metadata["flow_fragment"] = self._runtime.fragment_metadata(fragment)
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, OutputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            fragment = self._active_fragment
            if (
                fragment
                and not self._audio_correlation_ambiguous
                and await self._runtime.observe_audio_frame(fragment)
            ):
                frame.metadata["flow_fragment"] = self._runtime.fragment_metadata(fragment)
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, TTSStoppedFrame):
            if self._active_fragment:
                await self._runtime.finish_audio_fragment(self._active_fragment)
            self._active_fragment = None
            self._audio_correlation_ambiguous = False
        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._runtime.close(
                "pipeline_ended" if isinstance(frame, EndFrame) else "pipeline_cancelled"
            )
        await self.push_frame(frame, direction)
