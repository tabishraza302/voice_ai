from __future__ import annotations

import asyncio
import copy
import json
import os
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
import re
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
    TTSSpeakFrame,
    TranscriptionFrame,
    InterimTranscriptionFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineParams
from pipecat.pipeline.worker import PipelineWorker, WorkerParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.processors.aggregators.llm_context import NOT_GIVEN, LLMContext
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService, CartesiaTTSSettings
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService, ElevenLabsTTSSettings
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.utils.asyncio.task_manager import TaskManager
from .observability import tracing_enabled
from pipecat.workers.runner import WorkerRunner

SUPPORTED_LANGUAGES = frozenset({"en", "hi"})
SUPPORTED_STT_PROVIDER = "deepgram"
def _build_transport_params() -> TransportParams:
    return TransportParams(
        audio_in_enabled=True,
        audio_in_sample_rate=16_000,
        audio_in_filter=RNNoiseFilter(),
        audio_out_enabled=True,
        audio_out_sample_rate=24_000,
    )

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
_BACKCHANNELS = frozenset({
    "ok",
    "okay",
    "yeah",
    "yes",
    "yep",
    "uh-huh",
    "mhm",
    "mm",
    "right",
    "sure",
    "um",
    "umm",
    "ah",
    "हाँ",    # hān — informal yes (Devanagari ha + combining anusvara + visarga)
    "जी",     # jī — respectful yes/sir
    "जी।",    # jī with danda punctuation (STT may include it)
    "अच्छा",  # acchā — okay/good
    "ठीक",    # ṭhīk — fine/okay
    "हां",    # hāṁ — alternate transcription of hān
})


def _is_isolated_backchannel(text: str) -> bool:
    cleaned = text.strip().casefold()
    # Strip trailing punctuation (period, danda, question mark) for matching
    cleaned = cleaned.rstrip(".।?\u0964")
    return cleaned in _BACKCHANNELS or text.strip().casefold() in _BACKCHANNELS

_END_CALL_SCHEMA = FunctionSchema(
    name="end_call",
    description="End the call immediately. You MUST call this tool in THAT SAME TURN whenever the conversation reaches its conclusion, the user confirms they no longer need help, or the caller says goodbye. Pass your polite farewell sign-off in the message parameter so it is spoken to the caller before disconnecting.",
    properties={
        "message": {
            "type": "string",
            "description": "The final polite goodbye or closing sign-off message spoken to the caller before ending the call.",
        },
        "reason": {
            "type": "string",
            "description": "Reason for ending the call, e.g. booking confirmed, questions answered, caller said goodbye.",
        },
    },
    required=[],
)



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
        if not isinstance(agent, Mapping) or not isinstance(policy, Mapping) or not isinstance(prompt, Mapping):
            raise VoiceUnavailableError("The frozen call snapshot is incomplete.")
        if not isinstance(language, str) or language not in SUPPORTED_LANGUAGES:
            raise VoiceUnavailableError("The frozen call language is unavailable.")
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
            providers=providers,
        )


@dataclass(slots=True)
class _Playback:
    context_id: str | None
    kind: Literal["exact", "llm", "farewell"]
    generation: int
    step_id: str | None = None
    interrupted: bool = False


@dataclass(slots=True)
class _FarewellState:
    pending: bool = False
    started: bool = False
    context_id: str | None = None
    message: str | None = None
    mode: Literal["natural", "explicit"] | None = None
    generation: int | None = None


class CallRuntime:
    """One call's frozen settings, pipeline, and goal-driven dialogue."""

    def __init__(
        self,
        definition: CallDefinition,
        writer: Any,
    ) -> None:
        self.definition = definition
        self.writer = writer
        self.recent_conversation: deque[dict[str, str]] = deque(maxlen=_RECENT_TURNS)
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
        self.user_speaking = False
        self.vad_speech_active = False
        self.speech_generation = 0
        self.flush_task: asyncio.Task[None] | None = None
        self.response_generation = 0
        self.active_response_generation: int | None = None
        self.response_text: dict[int, list[str]] = {}
        self.response_complete: set[int] = set()
        self.response_interrupted: set[int] = set()
        self.playbacks: dict[str | None, _Playback] = {}
        self.runner: WorkerRunner | None = None
        self.end_call_pending = False
        self.farewell = _FarewellState()
        self.tts_request_texts: dict[str, str] = {}
        self.transcript_overlapped_playback: bool = False
        self.last_delivered_speech: str | None = None
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
        from .sessions import SessionWriter

        writer = SessionWriter(session_id)
        return cls(definition, writer)

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


        connection.add_event_handler("closed", stop_for_connection_event)
        connection.add_event_handler("failed", stop_for_connection_event)
        connection.add_event_handler("disconnected", stop_for_disconnect)
        connection.add_event_handler("app-message", self.receive_app_message)
        if hasattr(connection, "_pc") and connection._pc is not None:
            @connection._pc.on("iceconnectionstatechange")
            def on_ice_state_change() -> None:
                if connection._pc and connection._pc.iceConnectionState in ("failed", "closed"):
                    self.request_stop("cancelled")
        if hasattr(connection, "connection_timeout_secs"):
            connection.connection_timeout_secs = 15
        params = _build_transport_params()
        self.transport = SmallWebRTCTransport(connection, params=params)
        providers = self.definition.providers
        stt = DeepgramSTTService(
            api_key=providers.stt_api_key,
            settings=DeepgramSTTSettings(
                model=providers.stt_model,
                language=providers.language,
                endpointing=300,
                interim_results=True,
                smart_format=True,
                numerals=True,
            ),
        )
        llm = OpenAILLMService(
            api_key=providers.llm_api_key,
            model=providers.llm_model,
        )
        llm.register_function("end_call", self._handle_end_call)
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
        tts.add_event_handler("on_tts_request", self._on_tts_request)
        vad = SileroVADAnalyzer(
            sample_rate=16_000,
            params=VADParams(start_secs=0.35, stop_secs=0.35),
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
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        self.runner = runner
        self.worker_task = asyncio.create_task(
            runner.run(),
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

    def request_stop(self, status: str) -> None:
        if self.closing:
            return
        if status not in {"completed", "failed", "cancelled", "expired"}:
            status = "failed"
        self.closing = True
        self.termination_status = status
        if status == "completed" and self.connection is not None:
            try:
                self.connection.send_app_message({"type": "call.completed"})
            except Exception:
                pass
        self.cleanup_task = asyncio.create_task(
            _cleanup_runtime(self),
            name=f"voice-cleanup-{self.session_id}",
        )
    async def receive_app_message(self, *args: Any, **kwargs: Any) -> None:
        message = args[1] if len(args) > 1 else (args[0] if args else kwargs.get("message"))
        if isinstance(message, dict) and message.get("type") == "end":
            self.request_stop("completed")

    def is_bot_speaking(self) -> bool:
        return bool(self.playbacks) and not all(p.interrupted for p in self.playbacks.values())


    def note_interruption(self) -> None:
        generation = self.active_response_generation
        if generation is not None:
            self.response_interrupted.add(generation)
        for playback in self.playbacks.values():
            playback.interrupted = True
        self.tts_request_texts.clear()

    def _on_tts_request(self, service: Any, context_id: str, text: str) -> None:
        text_str = str(text).strip() if text else ""
        self.tts_request_texts[context_id] = text_str
        if self.farewell.pending and self.farewell.message and not self.farewell.started:
            farewell_text = self.farewell.message.strip()
            if self.farewell.mode == "explicit":
                if text_str.casefold() == farewell_text.casefold():
                    self.farewell.context_id = context_id
            elif self.farewell.mode == "natural":
                if text_str and (text_str.casefold() in farewell_text.casefold() or farewell_text.casefold() in text_str.casefold()):
                    self.farewell.context_id = context_id

    async def on_tts_started(self, frame: TTSStartedFrame) -> None:
        context_id = frame.context_id
        if context_id in self.playbacks:
            self.request_stop("failed")
            return
        is_farewell_context = (
            self.farewell.pending
            and self.farewell.context_id is not None
            and context_id == self.farewell.context_id
        )
        if is_farewell_context and not self.farewell.started:
            self.farewell.started = True

        if self.farewell.mode == "explicit" and is_farewell_context:
            self.playbacks[context_id] = _Playback(
                context_id=context_id,
                kind="farewell",
                generation=0,
                interrupted=False,
            )
        elif self.active_response_generation is not None:
            self.playbacks[context_id] = _Playback(
                context_id=context_id,
                kind="llm",
                generation=self.active_response_generation,
                interrupted=self.user_speaking or self.active_response_generation in self.response_interrupted,
            )
        else:
            self.playbacks[context_id] = _Playback(
                context_id=context_id,
                kind="farewell",
                generation=0,
                interrupted=False,
            )

    async def on_tts_stopped(self, frame: TTSStoppedFrame, *, transport_completed: bool) -> None:
        req_text = self.tts_request_texts.pop(frame.context_id, None) or ""
        playback = self.playbacks.pop(frame.context_id, None)
        if playback is None:
            return

        is_farewell = False
        if self.farewell.pending:
            if self.farewell.mode == "natural" and self.farewell.generation is not None:
                is_farewell = playback.generation == self.farewell.generation
            elif self.farewell.mode == "explicit" and self.farewell.message:
                is_farewell = (
                    frame.context_id == self.farewell.context_id
                    or req_text.casefold() == self.farewell.message.strip().casefold()
                )

        if not transport_completed:
            if not playback.interrupted:
                self.request_stop("failed")
            if is_farewell:
                self.farewell = _FarewellState()
            return

        generation = playback.generation
        chunks = self.response_text.pop(generation, [])
        text = "".join(chunks).strip()
        is_complete = generation in self.response_complete
        self.response_complete.discard(generation)
        self.response_interrupted.discard(generation)
        if not playback.interrupted and is_complete and text:
            self._persist_message(text)
            self.recent_conversation.append({"role": "assistant", "content": text})
            await self._send_caption("assistant", text, final=True)
            self.last_delivered_speech = text
        elif not playback.interrupted and is_farewell and self.farewell.mode == "explicit" and self.farewell.message:
            farewell_text = self.farewell.message
            self._persist_message(farewell_text, role="assistant")
            self.recent_conversation.append({"role": "assistant", "content": farewell_text})
            await self._send_caption("assistant", farewell_text, final=True)
            self.last_delivered_speech = farewell_text

        if is_farewell:
            self.farewell = _FarewellState()
        if self.end_call_pending and not self.playbacks and not self.farewell.pending:
            self.request_stop("completed")

    async def _handle_end_call(self, params: Any) -> None:
        args = getattr(params, "arguments", {}) or {}
        message = args.get("message") if isinstance(args, dict) else None
        current_text = ""
        if self.active_response_generation is not None:
            current_text = "".join(self.response_text.get(self.active_response_generation, [])).strip()

        already_delivered = bool(
            message
            and self.last_delivered_speech
            and message.casefold() in self.last_delivered_speech.casefold()
        )
        already_generating = bool(
            message
            and current_text
            and message.casefold() in current_text.casefold()
        )

        for ctx_id in list(self.playbacks.keys()):
            if self.playbacks[ctx_id].interrupted:
                self.playbacks.pop(ctx_id, None)

        if already_generating:
            matching_playback_ctx = None
            for ctx_id, pb in self.playbacks.items():
                if pb.generation == self.active_response_generation:
                    matching_playback_ctx = ctx_id
                    break
            self.farewell.pending = True
            self.farewell.mode = "natural"
            self.farewell.generation = self.active_response_generation
            self.farewell.message = current_text
            if matching_playback_ctx is not None:
                self.farewell.started = True
                self.farewell.context_id = matching_playback_ctx
            else:
                for ctx_id, req_text in self.tts_request_texts.items():
                    if req_text and (req_text.casefold() in current_text.casefold() or current_text.casefold() in req_text.casefold()):
                        self.farewell.context_id = ctx_id
                        break
        elif message and not already_delivered:

            self.farewell = _FarewellState(
                pending=True,
                mode="explicit",
                message=message,
            )
            await self.workflow_processor.push_frame(
                TTSSpeakFrame(text=message),
                FrameDirection.DOWNSTREAM,
            )
        callback = getattr(params, "result_callback", None)
        if callable(callback):
            res = callback({"status": "call_ending"})
            if asyncio.iscoroutine(res):
                await res
        self.end_call_pending = True

        async def _safety_stop() -> None:
            max_wait = 10.0 if message else 1.5
            poll_interval = 0.25
            waited = 0.0
            while waited < max_wait and not self.closing:
                await asyncio.sleep(poll_interval)
                waited += poll_interval
                if not self.playbacks and not self.farewell.pending:
                    break
            if not self.closing:
                self.request_stop("completed")
        asyncio.create_task(_safety_stop())
    async def handle_final_transcription(self, text: str) -> None:
        transcript = text.strip()
        if not transcript:
            return
        if len(transcript) > _MAX_TRANSCRIPT_CHARS:
            raise VoiceRuntimeError("Caller speech exceeded the per-turn transcript limit.")
        overlapped = self.transcript_overlapped_playback or self.is_bot_speaking()
        self.transcript_overlapped_playback = False
        self.user_speaking = False
        self._persist_message(transcript, role="user")
        self.recent_conversation.append({"role": "user", "content": transcript})
        await self._send_caption("user", transcript, final=True)
        if not (overlapped and _is_isolated_backchannel(transcript)):
            await self._push_dialogue_turn()

    def _build_system_message(self, directive: str | None = None) -> dict[str, str]:
        lines: list[str] = []
        if self.definition.global_prompt.strip():
            lines.append(self.definition.global_prompt.strip())
        if self.definition.agent_instructions.strip():
            lines.append(self.definition.agent_instructions.strip())
        lines.append(f"Respond in language: {self.definition.language}.")
        lines.append(
            "Call Termination Rule: Once the caller confirms they no longer need help, says goodbye, "
            "or the conversation reaches its end state, you MUST call the end_call tool in THAT SAME TURN. "
            "Pass your final polite closing message in the message argument of end_call to end the session directly. "
            "Never deliver a closing message without invoking end_call, and never wait for another user response to end the call."
        )
        if directive:
            lines.append(directive)
        return {"role": "system", "content": "\n".join(lines)}

    def _build_context_messages(self, directive: str | None = None) -> list[dict[str, Any]]:
        return [self._build_system_message(directive), *self.recent_conversation]

    async def _push_dialogue_turn(self, directive: str | None = None) -> None:
        messages = self._build_context_messages(directive)
        context = LLMContext(
            messages=messages,
            tools=[_END_CALL_SCHEMA],
        )
        await self.workflow_processor.push_frame(
            LLMContextFrame(context),
            FrameDirection.DOWNSTREAM,
        )

    def begin_llm_response(self) -> int:
        self.response_generation += 1
        generation = self.response_generation
        self.active_response_generation = generation
        self.response_text[generation] = []
        return generation

    def append_llm_text(self, text: str) -> str:
        generation = self.active_response_generation
        if generation is None:
            return ""
        chunks = self.response_text.get(generation)
        if chunks is None:
            return ""
        if sum(map(len, chunks)) + len(text) > _MAX_LLM_RESPONSE_CHARS:
            raise VoiceRuntimeError("The generated response exceeded its per-turn limit.")
        chunks.append(text)
        return "".join(chunks).strip()
    def finish_llm_response(self) -> None:
        generation = self.active_response_generation
        if generation is not None:
            self.response_complete.add(generation)

    def interrupt_llm_response(self) -> None:
        generation = self.active_response_generation
        if generation is not None:
            self.response_interrupted.add(generation)
        self.note_interruption()

    def _persist_message(self, content: str, *, role: str = "assistant") -> None:
        if content:
            self.writer.enqueue_message(role, content)

    async def _send_caption(
        self, role: Literal["user", "assistant"], text: str, *, final: bool = True
    ) -> None:
        if self.connection is None or not text:
            return
        try:
            result = self.connection.send_app_message(
                {"type": "caption", "role": role, "text": text, "final": final}
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            return


class _WorkflowProcessor(FrameProcessor):
    def __init__(self, runtime: CallRuntime) -> None:
        super().__init__()
        self.runtime = runtime

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(
            frame, (TranscriptionFrame, InterimTranscriptionFrame)
        ):
            await self._handle_transcription_frame(frame)
            return
        await self.push_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            return
        if isinstance(frame, StartFrame):
            directive = (
                "Greet the caller warmly in one short sentence and ask how you can help."
            )
            await self.runtime._push_dialogue_turn(directive=directive)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            is_continuation = bool(
                self.runtime.flush_task is not None
                and not self.runtime.flush_task.done()
            )
            self.runtime.vad_speech_active = True
            self.runtime.user_speaking = True
            self.runtime.speech_generation += 1
            if is_continuation:
                self.runtime.flush_task.cancel()
            if self.runtime.is_bot_speaking():
                self.runtime.transcript_overlapped_playback = True
            else:
                if not is_continuation and not self.runtime.transcript_segments:
                    self.runtime.transcript_overlapped_playback = False
                self.runtime.note_interruption()
                await self.broadcast_interruption()
            await self.push_frame(UserStartedSpeakingFrame(), direction)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.runtime.vad_speech_active = False
            self.runtime.user_speaking = False
            self.runtime.speech_generation += 1
            gen = self.runtime.speech_generation

            self._schedule_fallback_flush(gen)
        elif isinstance(frame, InterruptionFrame):
            self.runtime.note_interruption()
        elif isinstance(frame, ErrorFrame):
            self.runtime.request_stop("failed")

    def _schedule_fallback_flush(self, expected_gen: int) -> None:
        async def _fallback_flush() -> None:
            try:
                await asyncio.sleep(0.4)
            except asyncio.CancelledError:
                return
            if self.runtime.closing or self.runtime.closed:
                return
            if self.runtime.speech_generation == expected_gen and self.runtime.transcript_segments:
                text = " ".join(self.runtime.transcript_segments).strip()
                self.runtime.transcript_segments.clear()
                if text:
                    await self.runtime.handle_final_transcription(text)

        self.runtime.flush_task = asyncio.create_task(_fallback_flush())


    async def _handle_transcription_frame(self, frame: Frame) -> None:
        if isinstance(frame, InterimTranscriptionFrame):
            raw_text = frame.text.strip()
            if raw_text:
                if self.runtime.is_bot_speaking():
                    self.runtime.transcript_overlapped_playback = True
                    if not _is_isolated_backchannel(raw_text):
                        self.runtime.note_interruption()
                        await self.broadcast_interruption()
                text_so_far = " ".join([*self.runtime.transcript_segments, raw_text]).strip()
                if text_so_far:
                    await self.runtime._send_caption("user", text_so_far, final=False)
            return

        result = getattr(frame, "result", None)
        is_final = bool(getattr(result, "is_final", False))
        speech_final = bool(getattr(result, "speech_final", False))
        from_finalize = bool(getattr(result, "from_finalize", False))
        raw_text = getattr(frame, "text", "").strip()
        if raw_text and self.runtime.is_bot_speaking():
            self.runtime.transcript_overlapped_playback = True
            if not _is_isolated_backchannel(raw_text):
                self.runtime.note_interruption()
                await self.broadcast_interruption()

        if is_final and raw_text:
            self.runtime.transcript_segments.append(raw_text)

        if not (speech_final or from_finalize):
            if raw_text:
                text_so_far = (
                    " ".join(self.runtime.transcript_segments).strip()
                    if is_final
                    else " ".join([*self.runtime.transcript_segments, raw_text]).strip()
                )
                if text_so_far:
                    await self.runtime._send_caption("user", text_so_far, final=False)
            if (
                is_final
                and raw_text
                and not self.runtime.vad_speech_active
                and self.runtime.flush_task is not None
                and self.runtime.flush_task.done()
            ):
                self._schedule_fallback_flush(self.runtime.speech_generation)
            return

        if not is_final and raw_text:
            self.runtime.transcript_segments.append(raw_text)
        self.runtime.speech_generation += 1

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
                full_text = self.runtime.append_llm_text(frame.text)
                if full_text:
                    await self.runtime._send_caption("assistant", full_text, final=False)
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
        # Only this upstream tracker clears all contexts; downstream processing
        # may lag behind newly queued speech and must not invalidate it.
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
            if isinstance(frame, TTSStoppedFrame):
                if frame.context_id is not None:
                    transport_completed = frame.context_id in self.audio_contexts
                    self.audio_contexts.discard(frame.context_id)
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
    try:
        worker_task = runtime.worker_task
        current_task = asyncio.current_task()
        if runtime.duration_task is not None and runtime.duration_task is not current_task:
            runtime.duration_task.cancel()
        if runtime.flush_task is not None and not runtime.flush_task.done():
            runtime.flush_task.cancel()
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
            )
        except Exception:
            status = "failed"
        runtime.closed = True
        runtime.termination_status = status
    finally:
        from .signaling import release_call_runtime

        await release_call_runtime(runtime)
