from __future__ import annotations

import asyncio
import copy
import json
import os
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
import uuid

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSAudioRawFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams
from pipecat.pipeline.worker import PipelineWorker, WorkerParams
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService, CartesiaTTSSettings
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService, ElevenLabsTTSSettings
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.utils.asyncio.task_manager import TaskManager
from openai import AsyncOpenAI

from .extraction import (
    ConversationTurn,
    ExtractionField,
    FrozenPromptInstructions,
    PromptStep,
    build_turn_messages,
    extract_answers,
)
from .observability import tracing_enabled
from .workflow import (
    WorkflowAction,
    WorkflowConfigurationError,
    WorkflowState,
    WorkflowStep,
    WorkflowTransition,
    advance_workflow,
    start_workflow,
)


SUPPORTED_LANGUAGES = frozenset({"en", "hi"})
SUPPORTED_STT_PROVIDER = "deepgram"
SUPPORTED_STT_MODEL = "nova-3"
SUPPORTED_LLM_PROVIDER = "openai"
SUPPORTED_TTS_MODELS = {
    "cartesia": "sonic-3.6",
    "elevenlabs": "eleven_flash_v2_5",
}
_RECENT_TURNS = 12
_MAX_TRANSCRIPT_CHARS = 8_000
_MAX_LLM_RESPONSE_CHARS = 12_000
_PIPELINE_START_TIMEOUT_SECONDS = 30


class VoiceUnavailableError(RuntimeError):
    """A configured provider choice is not available on this server."""


class VoiceRuntimeError(RuntimeError):
    """A call could not safely continue in its current state."""


def _agent_value(agent: Any, name: str) -> Any:
    if isinstance(agent, Mapping):
        return agent.get(name)
    return getattr(agent, name, None)


def validate_tts_voice_selection(
    *,
    provider: str | None,
    model: str | None,
    voice_id: str | None,
    language: str,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return a publish error if the voice selection is invalid."""
    settings = os.environ if env is None else env
    if provider not in SUPPORTED_TTS_MODELS:
        return "Choose a supported TTS provider: Cartesia or ElevenLabs."
    expected_model = SUPPORTED_TTS_MODELS[provider]
    if model != expected_model:
        return f"The verified {provider} model is {expected_model}."
    if language not in SUPPORTED_LANGUAGES:
        return "The selected language is not supported by the verified voice runtime."
    key_name = "CARTESIA_API_KEY" if provider == "cartesia" else "ELEVENLABS_API_KEY"
    if not settings.get(key_name, "").strip():
        return f"TTS is unavailable because {key_name} is not configured on the server."
    if not isinstance(voice_id, str) or not voice_id.strip():
        return "A provider voice ID is required."
    return None


def provider_publish_errors(
    agent: Any,
    env: Mapping[str, str] | None = None,
) -> dict[str, list[str]]:
    """Validate provider choices against server credentials and verified allowlists.

    The environment is an administrator-controlled source. Provider models and
    voice IDs are never accepted merely because a caller supplied a non-empty
    string.
    """
    settings = os.environ if env is None else env
    errors: dict[str, list[str]] = {}

    def add(field_name: str, message: str) -> None:
        errors.setdefault(field_name, []).append(message)

    languages = _agent_value(agent, "languages")
    selected_languages: tuple[str, ...] = ()
    if not isinstance(languages, (list, tuple)) or not languages:
        add("languages", "At least one supported call language is required.")
    else:
        selected_languages = tuple(item for item in languages if isinstance(item, str))
        if len(selected_languages) != len(languages):
            add("languages", "Every call language must be a language code.")
        if len(selected_languages) != 1:
            add("languages", "Multilingual calls are unavailable until live TTS language switching is verified.")
        for language in selected_languages:
            if language not in SUPPORTED_LANGUAGES:
                add("languages", f"Language '{language}' is not supported by the verified voice runtime.")

    stt_provider = _agent_value(agent, "stt_provider")
    if stt_provider != SUPPORTED_STT_PROVIDER:
        add("stt_provider", "Only Deepgram speech recognition is available.")
    if _agent_value(agent, "stt_model") != SUPPORTED_STT_MODEL:
        add("stt_model", f"The verified Deepgram model is {SUPPORTED_STT_MODEL}.")
    if not settings.get("DEEPGRAM_API_KEY", "").strip():
        add("stt_provider", "Speech recognition is unavailable because DEEPGRAM_API_KEY is not configured on the server.")

    llm_provider = _agent_value(agent, "llm_provider")
    if llm_provider != SUPPORTED_LLM_PROVIDER:
        add("llm_provider", "Only OpenAI language models are available.")
    if not settings.get("OPENAI_API_KEY", "").strip():
        add("llm_provider", "OpenAI is unavailable because OPENAI_API_KEY is not configured on the server.")
    model_value = _agent_value(agent, "llm_model")
    verified_models = {
        item.strip()
        for item in settings.get("VERIFIED_OPENAI_MODELS", "").split(",")
        if item.strip()
    }
    if not verified_models:
        add("llm_model", "OpenAI model publishing is unavailable because VERIFIED_OPENAI_MODELS is not configured on the server.")
    elif not isinstance(model_value, str) or model_value not in verified_models:
        add("llm_model", "Choose an OpenAI model in the server's verified model allowlist.")

    tts_provider = _agent_value(agent, "tts_provider")
    tts_model = _agent_value(agent, "tts_model")
    voice_id = _agent_value(agent, "voice_id")
    if tts_provider not in SUPPORTED_TTS_MODELS:
        add("tts_provider", "Choose Cartesia or ElevenLabs for speech output.")
    elif tts_model != SUPPORTED_TTS_MODELS[tts_provider]:
        add("tts_model", f"The verified {tts_provider} model is {SUPPORTED_TTS_MODELS[tts_provider]}.")
    if len(selected_languages) == 1 and selected_languages[0] in SUPPORTED_LANGUAGES:
        voice_error = validate_tts_voice_selection(
            provider=tts_provider,
            model=tts_model,
            voice_id=voice_id,
            language=selected_languages[0],
            env=settings,
        )
        if voice_error is not None:
            add("voice_id", voice_error)
    elif not selected_languages:
        add("voice_id", "A voice cannot be selected until a supported call language is configured.")

    return errors


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    stt_model: str
    stt_api_key: str
    llm_model: str
    llm_api_key: str
    tts_provider: Literal["cartesia", "elevenlabs"]
    tts_model: str
    tts_api_key: str
    voice_id: str
    language: str


@dataclass(frozen=True, slots=True)
class CallDefinition:
    session_id: uuid.UUID
    agent_id: uuid.UUID
    call_type: Literal["test", "public"]
    agent_status: str
    agent_name: str
    agent_instructions: str
    global_prompt: str
    prompt_version: int
    language: str
    max_call_duration_seconds: int
    max_concurrent_calls_per_agent: int
    max_concurrent_calls_overall: int
    steps: tuple[Mapping[str, Any], ...]
    providers: ProviderConfig

    @classmethod
    def from_snapshot(
        cls,
        session_id: uuid.UUID,
        call_type: Literal["test", "public"],
        snapshot: Mapping[str, Any],
        env: Mapping[str, str],
    ) -> CallDefinition:
        if not isinstance(snapshot, Mapping):
            raise VoiceUnavailableError("The frozen call snapshot is malformed.")
        agent = snapshot.get("agent")
        policy = snapshot.get("policy")
        prompt = snapshot.get("global_prompt")
        language = snapshot.get("language")
        rows = snapshot.get("steps")
        if not isinstance(agent, Mapping) or not isinstance(policy, Mapping) or not isinstance(prompt, Mapping):
            raise VoiceUnavailableError("The frozen call snapshot is incomplete.")
        if not isinstance(language, str) or language not in SUPPORTED_LANGUAGES:
            raise VoiceUnavailableError("The frozen call language is unavailable.")
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise VoiceUnavailableError("The frozen workflow snapshot is malformed.")
        if not isinstance(prompt.get("text"), str) or not prompt.get("text", "").strip():
            raise VoiceUnavailableError("The frozen global prompt is unavailable.")
        prompt_version = prompt.get("version")
        if not isinstance(prompt_version, int) or isinstance(prompt_version, bool) or prompt_version < 1:
            raise VoiceUnavailableError("The frozen global prompt version is unavailable.")
        limits: list[int] = []
        for key in (
            "max_call_duration_seconds",
            "max_concurrent_calls_per_agent",
            "max_concurrent_calls_overall",
        ):
            value = policy.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise VoiceUnavailableError("The frozen call policy is invalid.")
            limits.append(value)

        agent_data = copy.deepcopy(dict(agent))
        agent_data["languages"] = list(agent_data.get("languages", []))
        if language not in agent_data["languages"]:
            raise VoiceUnavailableError("The frozen call language is not enabled for this agent.")
        errors = provider_publish_errors(agent_data, env)
        if errors:
            summary = "; ".join(
                f"{field_name}: {message}"
                for field_name, messages in errors.items()
                for message in messages
            )
            raise VoiceUnavailableError(summary)

        tts_provider = agent_data["tts_provider"]
        tts_model = agent_data["tts_model"]
        voice_id = agent_data["voice_id"]
        if tts_provider not in {"cartesia", "elevenlabs"}:
            raise VoiceUnavailableError("The frozen TTS provider is unavailable.")
        providers = ProviderConfig(
            stt_model=agent_data["stt_model"],
            stt_api_key=env["DEEPGRAM_API_KEY"],
            llm_model=agent_data["llm_model"],
            llm_api_key=env["OPENAI_API_KEY"],
            tts_provider=tts_provider,
            tts_model=tts_model,
            tts_api_key=env["CARTESIA_API_KEY" if tts_provider == "cartesia" else "ELEVENLABS_API_KEY"],
            voice_id=voice_id,
            language=language,
        )
        return cls(
            session_id=session_id,
            agent_id=uuid.UUID(str(agent_data["id"])),
            call_type=call_type,
            agent_status=str(agent_data.get("status", "")),
            agent_name=str(agent_data.get("name", "")),
            agent_instructions=str(agent_data.get("instructions", "")),
            global_prompt=prompt["text"],
            prompt_version=prompt_version,
            language=language,
            max_call_duration_seconds=limits[0],
            max_concurrent_calls_per_agent=limits[1],
            max_concurrent_calls_overall=limits[2],
            steps=tuple(copy.deepcopy(dict(row)) for row in rows),
            providers=providers,
        )


@dataclass(slots=True)
class _Playback:
    context_id: str | None
    kind: Literal["exact", "llm"]
    generation: int
    step_id: str | None = None
    interrupted: bool = False


class CallRuntime:
    """One call's frozen settings, pipeline, extraction client and workflow."""

    def __init__(
        self,
        definition: CallDefinition,
        initial: WorkflowTransition,
        writer: Any,
        extraction_client: AsyncOpenAI,
    ) -> None:
        self.definition = definition
        self.workflow_state: WorkflowState = initial.state
        self._initial_action = initial.action
        self._initial_action_sent = False
        self.writer = writer
        self.extraction_client = extraction_client
        self.prompt_instructions = FrozenPromptInstructions(
            global_policy=definition.global_prompt,
            agent_instructions=definition.agent_instructions,
        )
        self.recent_conversation: deque[ConversationTurn] = deque(maxlen=_RECENT_TURNS)
        self.validation_feedback: tuple[str, ...] = ()
        self.transcript_segments: list[str] = []
        self.connection: SmallWebRTCConnection | None = None
        self.transport: SmallWebRTCTransport | None = None
        self.worker: PipelineWorker | None = None
        self.worker_task: asyncio.Task[None] | None = None
        self.duration_task: asyncio.Task[None] | None = None
        self.ready: asyncio.Future[None] | None = None
        self.cleanup_task: asyncio.Task[None] | None = None
        self.closing = False
        self.renegotiating = False
        self.closed = False
        self.termination_status: str | None = None
        self.termination_outcome: str | None = None
        self.user_speaking = False
        self.response_generation = 0
        self.active_response_generation: int | None = None
        self.response_text: dict[int, list[str]] = {}
        self.response_complete: set[int] = set()
        self.response_interrupted: set[int] = set()
        self.terminal_narration_pending = False
        self.terminal_generation: int | None = None
        self.playbacks: dict[str | None, _Playback] = {}
        self.workflow_processor = _WorkflowProcessor(self)
        self.response_capture = _ResponseCaptureProcessor(self)
        self.tts_context_tracker = _TTSContextTracker(self)
        self.output_completion = _OutputCompletionProcessor(self, self.tts_context_tracker.audio_contexts)

    @classmethod
    def create(
        cls,
        session_id: uuid.UUID,
        call_type: Literal["test", "public"],
        snapshot: Mapping[str, Any],
        env: Mapping[str, str] | None = None,
    ) -> CallRuntime:
        settings = os.environ if env is None else env
        definition = CallDefinition.from_snapshot(session_id, call_type, snapshot, settings)
        initial = start_workflow(definition.steps, definition.language)
        if initial.action is None:
            raise VoiceUnavailableError("The frozen workflow has no initial action.")
        try:
            client = AsyncOpenAI(
                api_key=definition.providers.llm_api_key,
                timeout=30.0,
            )
        except Exception:
            raise VoiceUnavailableError("The OpenAI extraction client could not be created.") from None
        from .sessions import SessionWriter

        writer = SessionWriter(session_id)
        return cls(definition, initial, writer, client)

    @property
    def session_id(self) -> uuid.UUID:
        return self.definition.session_id

    @property
    def agent_id(self) -> uuid.UUID:
        return self.definition.agent_id


    async def attach(self, connection: SmallWebRTCConnection) -> None:
        if self.connection is not None:
            raise VoiceRuntimeError("A voice runtime cannot attach to more than one connection.")
        self.connection = connection

        async def stop_for_connection_event(*_: Any) -> None:
            self.request_stop("cancelled")

        async def stop_for_disconnect(*_: Any) -> None:
            if not self.renegotiating:
                self.request_stop("cancelled")

        async def receive_app_message(message: Any) -> None:
            if isinstance(message, dict) and message.get("type") == "end":
                self.request_stop("cancelled")

        connection.add_event_handler("closed", stop_for_connection_event)
        connection.add_event_handler("failed", stop_for_connection_event)
        connection.add_event_handler("disconnected", stop_for_disconnect)
        connection.add_event_handler("app-message", receive_app_message)
        params = TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=16_000,
            audio_out_enabled=True,
            audio_out_sample_rate=24_000,
        )
        self.transport = SmallWebRTCTransport(connection, params=params)
        providers = self.definition.providers
        stt = DeepgramSTTService(
            api_key=providers.stt_api_key,
            settings=DeepgramSTTSettings(
                model=providers.stt_model,
                language=providers.language,
                endpointing=300,
                interim_results=False,
                smart_format=True,
                numerals=True,
                utterance_end_ms=1000,
            ),
        )
        llm = OpenAILLMService(
            api_key=providers.llm_api_key,
            model=providers.llm_model,
        )
        if providers.tts_provider == "cartesia":
            tts = CartesiaTTSService(
                api_key=providers.tts_api_key,
                settings=CartesiaTTSSettings(
                    model=providers.tts_model,
                    voice=providers.voice_id,
                    language=providers.language,
                ),
            )
        else:
            tts = ElevenLabsTTSService(
                api_key=providers.tts_api_key,
                settings=ElevenLabsTTSSettings(
                    model=providers.tts_model,
                    voice=providers.voice_id,
                    language=providers.language,
                ),
            )
        vad = SileroVADAnalyzer(
            sample_rate=16_000,
            params=VADParams(start_secs=0.2, stop_secs=0.35),
        )
        pipeline = Pipeline(
            [
                self.transport.input(),
                VADProcessor(vad_analyzer=vad),
                stt,
                self.workflow_processor,
                llm,
                self.response_capture,
                tts,
                self.tts_context_tracker,
                self.transport.output(),
                self.output_completion,
            ]
        )
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(audio_in_sample_rate=16_000, audio_out_sample_rate=24_000),
            additional_span_attributes={
                "session.id": str(self.session_id),
                "agent.id": str(self.agent_id),
                "call.type": self.definition.call_type,
                "gen_ai.request.model": self.definition.providers.llm_model,
                "tts.provider": self.definition.providers.tts_provider,
                "tts.model": self.definition.providers.tts_model,
            },
            enable_tracing=tracing_enabled(),
            conversation_id=str(self.session_id),
        )
        self.worker = worker
        loop = asyncio.get_running_loop()
        self.ready = loop.create_future()

        async def on_started(*_: Any) -> None:
            if self.ready is not None and not self.ready.done():
                self.ready.set_result(None)

        async def on_finished(*_: Any) -> None:
            if self.ready is not None and not self.ready.done():
                self.ready.set_exception(VoiceRuntimeError("The voice pipeline stopped during setup."))
            if not self.closing:
                self.request_stop("cancelled")

        async def on_error(*_: Any) -> None:
            if self.ready is not None and not self.ready.done():
                self.ready.set_exception(VoiceRuntimeError("A voice provider failed during setup."))
            self.request_stop("failed")

        worker.add_event_handler("on_pipeline_started", on_started)
        worker.add_event_handler("on_pipeline_finished", on_finished)
        worker.add_event_handler("on_pipeline_error", on_error)
        self.worker_task = asyncio.create_task(
            worker.run(WorkerParams(task_manager=TaskManager())),
            name=f"voice-pipeline-{self.session_id}",
        )
        try:
            await asyncio.wait_for(asyncio.shield(self.ready), timeout=_PIPELINE_START_TIMEOUT_SECONDS)
        except Exception:
            self.request_stop("failed")
            raise
        self.duration_task = asyncio.create_task(
            self._expire_after_duration(),
            name=f"voice-duration-{self.session_id}",
        )


    async def _expire_after_duration(self) -> None:
        await asyncio.sleep(self.definition.max_call_duration_seconds)
        self.request_stop("expired")

    def request_stop(self, status: str, outcome: str | None = None) -> None:
        if self.closing:
            return
        if status not in {"completed", "failed", "cancelled", "expired"}:
            status = "failed"
        self.closing = True
        self.termination_status = status
        self.termination_outcome = outcome
        if status == "completed" and self.connection is not None:
            try:
                self.connection.send_app_message({"type": "call.completed"})
            except Exception:
                pass
        self.cleanup_task = asyncio.create_task(
            _cleanup_runtime(self),
            name=f"voice-cleanup-{self.session_id}",
        )

    def note_interruption(self) -> None:
        self.user_speaking = True
        generation = self.active_response_generation
        if generation is not None:
            self.response_interrupted.add(generation)
        for playback in self.playbacks.values():
            playback.interrupted = True

    async def on_tts_started(self, frame: TTSStartedFrame) -> None:
        context_id = frame.context_id
        if context_id in self.playbacks:
            self.request_stop("failed")
            return
        generation = self.active_response_generation
        if generation is not None:
            self.playbacks[context_id] = _Playback(
                context_id=context_id,
                kind="llm",
                generation=generation,
                interrupted=self.user_speaking or generation in self.response_interrupted,
            )

    async def on_tts_stopped(self, frame: TTSStoppedFrame, *, transport_completed: bool) -> None:
        playback = self.playbacks.pop(frame.context_id, None)
        if playback is None:
            return
        if not transport_completed:
            if not playback.interrupted:
                self.request_stop("failed")
            return

        generation = playback.generation
        chunks = self.response_text.pop(generation, [])
        text = "".join(chunks).strip()
        is_complete = generation in self.response_complete
        self.response_complete.discard(generation)
        self.response_interrupted.discard(generation)
        if playback.interrupted or not is_complete or not text:
            if self.terminal_generation == generation:
                self.terminal_generation = None
                self.terminal_narration_pending = True
            return
        self._persist_message(text)
        self.recent_conversation.append(ConversationTurn(role="assistant", content=text))
        await self._send_caption("assistant", text)
        if self.terminal_generation == generation:
            self.terminal_generation = None
            self.terminal_narration_pending = False
            self.request_stop("completed", self.workflow_state.outcome)

    async def handle_final_transcription(self, text: str) -> None:
        transcript = text.strip()
        if not transcript:
            return
        if len(transcript) > _MAX_TRANSCRIPT_CHARS:
            raise VoiceRuntimeError("Caller speech exceeded the per-turn transcript limit.")
        self.user_speaking = False
        self._persist_message(transcript, role="user")
        self.recent_conversation.append(ConversationTurn(role="user", content=transcript))
        await self._send_caption("user", transcript)

        def _step_options(step: WorkflowStep) -> tuple[str, ...]:
            options: list[str] = []
            for branch in step.branches:
                for condition in branch.conditions:
                    if condition.field_key == step.field_key and condition.operand is not None:
                        val = str(condition.operand).strip()
                        if val and val not in options:
                            options.append(val)
            return tuple(options)

        by_key = {step.field_key: step for step in self.workflow_state.steps}
        unanswered = [
            ExtractionField(
                field_key=step.field_key,
                options=_step_options(step),
            )
            for step in self.workflow_state.steps
            if step.field_key not in self.workflow_state.answers
        ]
        corrections = [
            ExtractionField(
                field_key=field_key,
                options=_step_options(by_key[field_key]) if field_key in by_key else (),
            )
            for field_key in self.workflow_state.answers
            if field_key in by_key
        ]
        extracted = await extract_answers(
            self.extraction_client,
            self.definition.providers.llm_model,
            unanswered,
            tuple(self.recent_conversation),
            correction_fields=corrections,
            confirmed_answers=self.workflow_state.answers,
        )
        transition = advance_workflow(self.workflow_state, extracted)
        action = transition.action
        self.apply_transition_events(transition.events)
        self.workflow_state = transition.state
        if action is not None:
            await self.dispatch(action)
        elif transition.state.completed and not self.closing and self.terminal_generation is None:
            if transition.state.outcome is not None:
                await self.dispatch(
                    WorkflowAction(
                        kind="outcome",
                        field_key=transition.state.outcome,
                        content=transition.state.outcome,
                    )
                )
            else:
                await self.dispatch(WorkflowAction(kind="complete"))

    def apply_transition_events(self, events: Sequence[Mapping[str, Any]]) -> None:
        feedback: list[str] = []
        for event in events:
            event_type = event.get("type")
            if event_type == "answer":
                accepted = event.get("accepted") is True
                validation_errors = event.get("validation_errors", [])
                value = event.get("value")
                field_key = event.get("field_key")
                if not accepted:
                    self.writer.enqueue_answer(
                        str(field_key),
                        value,
                        False,
                        {
                            "reason": event.get("reason", "invalid"),
                            "validation_errors": list(validation_errors) if isinstance(validation_errors, (list, tuple)) else [],
                        },
                    )
                    feedback.append(
                        "The caller's answer did not meet the configured requirements. Ask for a corrected value without explaining internal rules."
                    )
                else:
                    self.writer.enqueue_answer(
                        str(field_key),
                        value,
                        True,
                        {"reason": "accepted", "validation_errors": []},
                    )
            elif event_type == "answer_invalidated":
                self.writer.enqueue_answer(
                    str(event.get("field_key", "")),
                    event.get("value"),
                    False,
                    {"reason": "correction", "invalidated": True},
                )
        self.validation_feedback = tuple(feedback)

    async def dispatch(self, action: WorkflowAction) -> None:
        step = self._step_for_action(action)
        active_step = self._prompt_step(step) if step is not None else None
        feedback = list(self.validation_feedback)
        if action.kind == "clarify":
            if action.reason == "ambiguous":
                feedback.append("Ask the caller to clarify the answer because more than one value is possible.")
            elif action.reason == "missing":
                feedback.append("Ask only for the current unanswered information.")
            else:
                feedback.append("Ask the caller for a corrected value without revealing private validation rules.")
        elif action.kind == "outcome":
            outcome = action.content or action.field_key or ""
            feedback.append(f"The configured call outcome is {outcome}. Explain the result plainly and close the call.")
        elif action.kind == "complete":
            feedback.append("The workflow is complete. Thank the caller and close the call politely.")


        if action.kind in {"complete", "outcome"}:
            self.terminal_narration_pending = True
            self.terminal_generation = None
        turn = build_turn_messages(
            instructions=self.prompt_instructions,
            active_step=active_step,
            recent_conversation=tuple(self.recent_conversation),
            confirmed_answers=self.workflow_state.answers,
            validation_feedback=feedback,
            language=self.definition.language,
        )
        await self.workflow_processor.push_frame(
            LLMContextFrame(LLMContext(messages=[dict(message) for message in turn.messages])),
            FrameDirection.DOWNSTREAM,
        )

    def begin_llm_response(self) -> int:
        self.response_generation += 1
        generation = self.response_generation
        self.active_response_generation = generation
        self.response_text[generation] = []
        if self.terminal_narration_pending:
            self.terminal_generation = generation
            self.terminal_narration_pending = False
        return generation

    def append_llm_text(self, text: str) -> None:
        generation = self.active_response_generation
        if generation is None:
            return
        chunks = self.response_text.get(generation)
        if chunks is None:
            return
        if sum(map(len, chunks)) + len(text) > _MAX_LLM_RESPONSE_CHARS:
            raise VoiceRuntimeError("The generated response exceeded its per-turn limit.")
        chunks.append(text)

    def finish_llm_response(self) -> None:
        generation = self.active_response_generation
        if generation is not None:
            self.response_complete.add(generation)

    def interrupt_llm_response(self) -> None:
        generation = self.active_response_generation
        if generation is not None:
            self.response_interrupted.add(generation)
        self.note_interruption()

    def _step_for_action(self, action: WorkflowAction) -> WorkflowStep | None:
        if action.step_id is not None:
            return next((step for step in self.workflow_state.steps if step.id == action.step_id), None)
        if action.field_key is not None:
            return next((step for step in self.workflow_state.steps if step.field_key == action.field_key), None)
        return None

    @staticmethod
    def _prompt_step(step: WorkflowStep) -> PromptStep:
        return PromptStep(
            field_key=step.field_key,
            title=step.title,
            objective=step.objective,
        )

    def _persist_message(self, content: str, *, role: str = "assistant") -> None:
        if content:
            self.writer.enqueue_message(role, content)

    async def _send_caption(self, role: Literal["user", "assistant"], text: str) -> None:
        if self.connection is None or not text:
            return
        try:
            result = self.connection.send_app_message({"type": "caption", "role": role, "text": text})
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            # Audio and persistence do not depend on the optional caption channel.
            return


class _WorkflowProcessor(FrameProcessor):
    def __init__(self, runtime: CallRuntime) -> None:
        super().__init__()
        self.runtime = runtime

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, TranscriptionFrame):
            await self._handle_transcription_frame(frame)
            return
        await self.push_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            return
        if isinstance(frame, StartFrame):
            if not self.runtime._initial_action_sent and self.runtime._initial_action is not None:
                self.runtime._initial_action_sent = True
                await self.runtime.dispatch(self.runtime._initial_action)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self.runtime.user_speaking = True
            self.runtime.note_interruption()
            await self.broadcast_interruption()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.runtime.user_speaking = False
        elif isinstance(frame, InterruptionFrame):
            self.runtime.note_interruption()
        elif isinstance(frame, ErrorFrame):
            self.runtime.request_stop("failed")

    async def _handle_transcription_frame(self, frame: TranscriptionFrame) -> None:
        result = frame.result
        is_final = bool(getattr(result, "is_final", False))
        speech_final = bool(getattr(result, "speech_final", False))
        from_finalize = bool(getattr(result, "from_finalize", False))
        if is_final and frame.text.strip():
            self.runtime.transcript_segments.append(frame.text.strip())
        if not (speech_final or from_finalize):
            return
        text = " ".join(self.runtime.transcript_segments).strip()
        self.runtime.transcript_segments.clear()
        if text:
            await self.runtime.handle_final_transcription(text)


class _ResponseCaptureProcessor(FrameProcessor):
    def __init__(self, runtime: CallRuntime) -> None:
        super().__init__()
        self.runtime = runtime

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self.runtime.begin_llm_response()
            elif isinstance(frame, LLMTextFrame):
                self.runtime.append_llm_text(frame.text)
            elif isinstance(frame, LLMFullResponseEndFrame):
                self.runtime.finish_llm_response()
            elif isinstance(frame, InterruptionFrame):
                self.runtime.interrupt_llm_response()
        await self.push_frame(frame, direction)


class _TTSContextTracker(FrameProcessor):
    # SmallWebRTCOutputTransport rechunks audio without retaining context_id;
    # keep the original context IDs to match its downstream stop markers.
    def __init__(self, runtime: CallRuntime) -> None:
        super().__init__()
        self.runtime = runtime
        self.audio_contexts: set[str] = set()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        stop_due_to_error = False
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, InterruptionFrame):
                self.audio_contexts.clear()
            elif isinstance(frame, TTSStartedFrame):
                await self.runtime.on_tts_started(frame)
            elif isinstance(frame, TTSAudioRawFrame) and frame.context_id is not None:
                self.audio_contexts.add(frame.context_id)
            elif isinstance(frame, ErrorFrame):
                self.audio_contexts.clear()
                stop_due_to_error = True
        await self.push_frame(frame, direction)
        if stop_due_to_error:
            self.runtime.request_stop("failed")


class _OutputCompletionProcessor(FrameProcessor):
    def __init__(self, runtime: CallRuntime, audio_contexts: set[str]) -> None:
        super().__init__()
        self.runtime = runtime
        self.audio_contexts = audio_contexts

    # SmallWebRTCOutputTransport forwards TTSStoppedFrame only after preceding
    # queued audio has been sent through the server's WebRTC audio track.
    # The remote speaker still provides no render or playback acknowledgment.
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        transport_completed = False
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, InterruptionFrame):
                self.audio_contexts.clear()
            elif isinstance(frame, TTSStoppedFrame):
                if frame.context_id is not None:
                    transport_completed = frame.context_id in self.audio_contexts
                    self.audio_contexts.discard(frame.context_id)
            elif isinstance(frame, ErrorFrame):
                self.audio_contexts.clear()
        await self.push_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSStoppedFrame):
                await self.runtime.on_tts_stopped(
                    frame,
                    transport_completed=transport_completed,
                )
            elif isinstance(frame, ErrorFrame):
                self.runtime.request_stop("failed")

def create_call_runtime(
    session_id: uuid.UUID,
    call_type: Literal["test", "public"],
    snapshot: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
) -> CallRuntime:
    return CallRuntime.create(session_id, call_type, snapshot, env)


async def _cleanup_runtime(runtime: CallRuntime) -> None:
    status = runtime.termination_status or "failed"
    worker_task = runtime.worker_task
    current_task = asyncio.current_task()
    if runtime.duration_task is not None and runtime.duration_task is not current_task:
        runtime.duration_task.cancel()
    if runtime.worker is not None and worker_task is not None and not worker_task.done():
        try:
            await runtime.worker.cancel()
        except Exception:
            status = "failed"
        if worker_task is not current_task:
            try:
                await asyncio.wait_for(asyncio.shield(worker_task), timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                status = "failed"
    if runtime.connection is not None:
        try:
            await runtime.connection.disconnect()
        except Exception:
            status = "failed"
    try:
        await runtime.writer.flush()
    except Exception:
        status = "failed"
    from .sessions import set_session_status

    try:
        await set_session_status(
            runtime.session_id,
            status,
            outcome=runtime.termination_outcome if status == "completed" else None,
        )
    except Exception:
        status = "failed"
    try:
        await runtime.extraction_client.close()
    except Exception:
        pass
    runtime.closed = True
    runtime.termination_status = status
    from .signaling import release_call_runtime

    await release_call_runtime(runtime)
