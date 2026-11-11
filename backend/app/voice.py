from __future__ import annotations

import asyncio
import copy
import json
import os
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
import re
import uuid

if TYPE_CHECKING:
    from .tts_cache import TTSAudioCache

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
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
from pipecat.services.azure.stt import AzureSTTService, AzureSTTSettings
from pipecat.services.cartesia.tts import CartesiaTTSService, CartesiaTTSSettings
from pipecat.services.deepgram.stt import DeepgramSTTService, DeepgramSTTSettings
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService, ElevenLabsTTSSettings
from pipecat.services.openai.llm import OpenAILLMService

from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import TransportParams
from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.utils.asyncio.task_manager import TaskManager
from .cached_tts import CachedCartesiaTTSService, CachedElevenLabsTTSService
from .observability import tracing_enabled
from pipecat.workers.runner import WorkerRunner

SUPPORTED_LANGUAGES = frozenset({"en", "hi"})
SUPPORTED_STT_MODELS = {
    "deepgram": "nova-3",
    "azure": "azure-speech",
}
SUPPORTED_AZURE_ENGLISH_LOCALES = frozenset({"en-IN", "en-US", "en-GB"})


def _build_transport_params() -> TransportParams:
    return TransportParams(
        audio_in_enabled=True,
        audio_in_sample_rate=16_000,
        audio_in_filter=RNNoiseFilter(),
        audio_out_enabled=True,
        audio_out_sample_rate=24_000,
    )


SUPPORTED_LLM_PROVIDER = "openai"
SUPPORTED_TTS_MODELS = {
    "cartesia": "sonic-3.6",
    "elevenlabs": "eleven_flash_v2_5",
}
_RECENT_TURNS = 12
_MAX_TRANSCRIPT_CHARS = 8_000
_MAX_LLM_RESPONSE_CHARS = 12_000
_PIPELINE_START_TIMEOUT_SECONDS = 30
# Azure recognition results carry offset/duration in 100 ns ticks relative to
# the input audio stream.
_AZURE_TICKS_PER_SECOND = 10_000_000
# VAD reports a stop after its stop_secs of silence. Snapshot the end of the
# last speech window on the continuous input timeline at VAD stop and allow
# Azure's segment endpoint a small margin; later silence must not move it.
_AZURE_ENDPOINT_MARGIN_SECONDS = 0.2
# VAD stop anchors one cancellable turn-end decision. With Silero VAD params
# (start_secs=0.35, stop_secs=0.35), an internal pause of ~410ms triggers VAD
# stop at 350ms, while resumed speech is not confirmed until 350ms of audio later
# (~410ms after stop). A 450ms quiet deadline ensures that detector start
# hysteresis does not prematurely flush completed prior clauses before resumed
# speech can cancel the deadline. When a covering final arrives after this quiet
# window has already elapsed, it completes and submits immediately with zero wait.
_AZURE_TURN_QUIET_SECONDS = 0.45
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
    if stt_provider not in SUPPORTED_STT_MODELS:
        add("stt_provider", "Choose Deepgram or Azure for speech recognition.")
    else:
        expected_model = SUPPORTED_STT_MODELS[stt_provider]
        if _agent_value(agent, "stt_model") != expected_model:
            add("stt_model", f"The supported {stt_provider} model is {expected_model}.")
        if stt_provider == "deepgram":
            if not settings.get("DEEPGRAM_API_KEY", "").strip():
                add("stt_provider", "Speech recognition is unavailable because DEEPGRAM_API_KEY is not configured on the server.")
        elif stt_provider == "azure":
            if not settings.get("AZURE_SPEECH_API_KEY", "").strip():
                add("stt_provider", "Speech recognition is unavailable because AZURE_SPEECH_API_KEY is not configured on the server.")
            if not settings.get("AZURE_SPEECH_REGION", "").strip() and not settings.get("AZURE_SPEECH_ENDPOINT", "").strip():
                add("stt_provider", "Azure speech recognition requires AZURE_SPEECH_REGION or AZURE_SPEECH_ENDPOINT on the server.")
            if selected_languages == ("en",):
                locale = _agent_value(agent, "azure_stt_english_locale")
                if locale not in SUPPORTED_AZURE_ENGLISH_LOCALES:
                    add("azure_stt_english_locale", "Choose an Azure English locale: en-IN, en-US, or en-GB.")

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
    stt_provider: Literal["deepgram", "azure"]
    stt_model: str
    stt_api_key: str
    stt_region: str | None
    stt_endpoint: str | None
    stt_language: str
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

        stt_provider = agent_data["stt_provider"]
        if stt_provider == "deepgram":
            stt_api_key = env["DEEPGRAM_API_KEY"].strip()
            stt_region = None
            stt_endpoint = None
            stt_language = language
        elif stt_provider == "azure":
            stt_api_key = env["AZURE_SPEECH_API_KEY"].strip()
            raw_endpoint = env.get("AZURE_SPEECH_ENDPOINT", "").strip() or None
            raw_region = env.get("AZURE_SPEECH_REGION", "").strip() or None
            if raw_endpoint is not None:
                stt_endpoint = raw_endpoint
                stt_region = None
            else:
                stt_endpoint = None
                stt_region = raw_region
            if language == "hi":
                stt_language = "hi-IN"
            else:
                stt_language = agent_data["azure_stt_english_locale"]
        else:
            raise VoiceUnavailableError("The frozen STT provider is unavailable.")

        tts_provider = agent_data["tts_provider"]
        tts_model = agent_data["tts_model"]
        voice_id = agent_data["voice_id"]
        if tts_provider not in {"cartesia", "elevenlabs"}:
            raise VoiceUnavailableError("The frozen TTS provider is unavailable.")
        providers = ProviderConfig(
            stt_provider=stt_provider,
            stt_model=agent_data["stt_model"],
            stt_api_key=stt_api_key,
            stt_region=stt_region,
            stt_endpoint=stt_endpoint,
            stt_language=stt_language,
            llm_model=agent_data["llm_model"],
            llm_api_key=env["OPENAI_API_KEY"].strip(),
            tts_provider=tts_provider,
            tts_model=tts_model,
            tts_api_key=env["CARTESIA_API_KEY" if tts_provider == "cartesia" else "ELEVENLABS_API_KEY"].strip(),
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
    interruptible: bool = True
    transport_completed: bool = False


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
        *,
        tts_cache: TTSAudioCache | None = None,
    ) -> None:
        self.definition = definition
        self.writer = writer
        self.tts_cache: TTSAudioCache | None = tts_cache
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
        # Azure segments are not turn boundaries. Keep finalized segments in
        # transcript_segments until VAD stop and a final covering the last
        # speech window agree that the turn is complete. Azure offset/duration
        # values and input_audio_ticks share the continuous STT input stream
        # timeline.
        self.input_audio_ticks = 0
        self.azure_speech_end_ticks: int | None = None
        self.azure_accepted_end_ticks: int | None = None
        self.azure_turn_final_end_ticks: int | None = None
        self.azure_interim_since_final = False
        self.response_generation = 0
        self.active_response_generation: int | None = None
        self.response_text: dict[int, list[str]] = {}
        self.response_complete: set[int] = set()
        self.response_interrupted: set[int] = set()
        # The generations whose end has been handed on downstream. That frame
        # is the boundary of the reply: everything the reply is made of has
        # been handed to the TTS service by then.
        self.response_ended: set[int] = set()
        self.in_flight_generations: set[int] = set()
        self.delivered_generations: set[int] = set()
        # The segments of a response that have been handed to the TTS service
        # and have not produced their output yet. The end of the response and
        # the output of its segments arrive in either order, so a response is
        # only closed when both are in.
        self.response_segments: dict[int, set[str]] = {}
        self.segment_generation: dict[str, int] = {}
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
        self.output_completion = _OutputCompletionProcessor(
            self,
            self.tts_context_tracker.audio_contexts,
            self.tts_context_tracker.uninterruptible_contexts,
        )

    @classmethod
    def create(
        cls,
        session_id: uuid.UUID,
        call_type: Literal["test", "public"],
        snapshot: Mapping[str, Any],
        env: Mapping[str, str] | None = None,
        *,
        tts_cache: TTSAudioCache | None = None,
    ) -> CallRuntime:
        settings = os.environ if env is None else env
        definition = CallDefinition.from_snapshot(session_id, call_type, snapshot, settings)
        from .sessions import SessionWriter

        writer = SessionWriter(session_id)
        return cls(
            definition,
            writer,
            tts_cache=tts_cache,
        )

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
        if providers.stt_provider == "deepgram":
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
        else:
            stt = AzureSTTService(
                api_key=providers.stt_api_key,
                region=providers.stt_region,
                private_endpoint=providers.stt_endpoint,
                sample_rate=16000,
                settings=AzureSTTSettings(language=Language(providers.stt_language)),
            )
        llm = OpenAILLMService(
            api_key=providers.llm_api_key,
            model=providers.llm_model,
        )
        llm.register_function("end_call", self._handle_end_call)
        if providers.tts_provider == "cartesia":
            # The settings are built once and handed to either service, so the
            # cached path and the plain path cannot drift apart.
            cartesia_settings = CartesiaTTSSettings(
                model=providers.tts_model,
                voice=providers.voice_id,
                language=providers.language,
            )
            if self.tts_cache is not None and self.tts_cache.enabled:
                tts = CachedCartesiaTTSService(
                    api_key=providers.tts_api_key,
                    settings=cartesia_settings,
                    cache=self.tts_cache,
                    agent_id=self.agent_id,
                )
            else:
                tts = CartesiaTTSService(
                    api_key=providers.tts_api_key,
                    settings=cartesia_settings,
                )
        else:
            elevenlabs_settings = ElevenLabsTTSSettings(
                model=providers.tts_model,
                voice=providers.voice_id,
                language=providers.language,
            )
            if self.tts_cache is not None and self.tts_cache.enabled:
                tts = CachedElevenLabsTTSService(
                    api_key=providers.tts_api_key,
                    settings=elevenlabs_settings,
                    cache=self.tts_cache,
                    agent_id=self.agent_id,
                )
            else:
                tts = ElevenLabsTTSService(
                    api_key=providers.tts_api_key,
                    settings=elevenlabs_settings,
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

    def note_input_audio(self, frame: InputAudioRawFrame) -> None:
        sample_rate = frame.sample_rate or 16_000
        self.input_audio_ticks += (
            frame.num_frames * _AZURE_TICKS_PER_SECOND // sample_rate
        )

    def azure_tolerance_ticks(self) -> int:
        return int(_AZURE_ENDPOINT_MARGIN_SECONDS * _AZURE_TICKS_PER_SECOND)


    def note_interruption(self) -> None:
        generation = self.active_response_generation
        if generation is not None:
            self.response_interrupted.add(generation)
        for gen in list(self.in_flight_generations):
            self.response_interrupted.add(gen)
            # An interrupted reply is never persisted, so its text and the
            # segments it was waiting for are dropped with it.
            self.response_text.pop(gen, None)
            self.response_ended.discard(gen)
            for context_id in self.response_segments.pop(gen, set()):
                self.segment_generation.pop(context_id, None)
        self.in_flight_generations.clear()
        for playback in self.playbacks.values():
            if playback.interruptible:
                playback.interrupted = True
        uninterruptible_ids = {
            ctx_id for ctx_id, p in self.playbacks.items() if not p.interruptible
        }
        if self.farewell.pending and self.farewell.mode == "explicit" and self.farewell.context_id:
            uninterruptible_ids.add(self.farewell.context_id)
        self.tts_request_texts = {
            k: v for k, v in self.tts_request_texts.items() if k in uninterruptible_ids
        }

    def _on_tts_request(self, service: Any, context_id: str, text: str) -> None:
        text_str = str(text).strip() if text else ""
        self.tts_request_texts[context_id] = text_str
        is_explicit_farewell = False
        if self.farewell.pending and self.farewell.message and not self.farewell.started:
            farewell_text = self.farewell.message.strip()
            if self.farewell.mode == "explicit":
                if text_str.casefold() == farewell_text.casefold():
                    self.farewell.context_id = context_id
                    is_explicit_farewell = True
            elif self.farewell.mode == "natural":
                if text_str and (text_str.casefold() in farewell_text.casefold() or farewell_text.casefold() in text_str.casefold()):
                    self.farewell.context_id = context_id
        if is_explicit_farewell:
            # The forced farewell is an utterance of its own, not a segment of
            # the reply that is still being spoken.
            return
        generation = self.active_response_generation
        if generation is not None and generation in self.in_flight_generations:
            # The segment has been handed to the TTS service: the response it
            # belongs to cannot be closed before it has produced its output.
            self.response_segments.setdefault(generation, set()).add(context_id)
            self.segment_generation[context_id] = generation

    async def on_tts_started(
        self,
        frame: TTSStartedFrame,
        *,
        generation: int | None = None,
        interruptible: bool = True,
    ) -> None:
        context_id = frame.context_id
        if context_id in self.playbacks:
            self.request_stop("failed")
            return
        is_farewell_context = (
            self.farewell.pending
            and (
                (self.farewell.context_id is not None and context_id == self.farewell.context_id)
                or (
                    self.farewell.message
                    and self.tts_request_texts.get(context_id, "").strip().casefold()
                    == self.farewell.message.strip().casefold()
                )
            )
        )
        if is_farewell_context and not self.farewell.started:
            self.farewell.started = True
            self.farewell.context_id = context_id

        effective_generation = generation if generation is not None else (self.active_response_generation or 0)

        if self.farewell.mode == "explicit" and is_farewell_context:
            self.farewell.started = True
            self.farewell.context_id = context_id
            self.playbacks[context_id] = _Playback(
                context_id=context_id,
                kind="farewell",
                generation=0,
                interrupted=False,
                interruptible=False,
            )
        elif effective_generation > 0:
            self.playbacks[context_id] = _Playback(
                context_id=context_id,
                kind="llm",
                generation=effective_generation,
                interrupted=self.user_speaking or effective_generation in self.response_interrupted,
                interruptible=interruptible,
            )
        elif is_farewell_context:
            self.playbacks[context_id] = _Playback(
                context_id=context_id,
                kind="farewell",
                generation=0,
                interrupted=False,
                interruptible=interruptible,
            )
        else:
            self.playbacks[context_id] = _Playback(
                context_id=context_id,
                kind="exact",
                generation=0,
                interrupted=False,
                interruptible=interruptible,
            )

    async def on_tts_stopped(self, frame: TTSStoppedFrame, *, transport_completed: bool) -> None:
        req_text = self.tts_request_texts.pop(frame.context_id, None) or ""
        playback = self.playbacks.pop(frame.context_id, None)
        # The segment is over, whether or not it produced any output: the
        # response it belongs to may now be closed.
        segment_generation = self.segment_generation.pop(frame.context_id, None)
        if segment_generation is not None:
            outstanding = self.response_segments.get(segment_generation)
            if outstanding is not None:
                outstanding.discard(frame.context_id)
        generation = (
            segment_generation
            if segment_generation is not None
            else (playback.generation if playback is not None else 0)
        )
        if playback is None:
            # The runtime never saw this segment start, but its output is over
            # all the same: the response it belongs to may now be closed.
            await self._finalize_response(generation)
            return

        is_explicit_farewell = (
            self.farewell.pending
            and self.farewell.mode == "explicit"
            and (
                frame.context_id == self.farewell.context_id
                or (self.farewell.message and req_text.casefold() == self.farewell.message.strip().casefold())
            )
        )

        if not transport_completed:
            if not playback.interrupted:
                self.request_stop("failed")
            if is_explicit_farewell:
                self.farewell = _FarewellState()
            return

        playback.transport_completed = True
        if not playback.interrupted and generation > 0:
            self.delivered_generations.add(generation)
        # The reply is only closed once its text and the output of all of its
        # segments are in, so a segment that is still on its way neither loses
        # the reply nor lets the call end before it was spoken.
        await self._finalize_response(generation)

        if is_explicit_farewell and not playback.interrupted and self.farewell.message:
            farewell_text = self.farewell.message
            self._persist_message(farewell_text, role="assistant")
            self.recent_conversation.append({"role": "assistant", "content": farewell_text})
            await self._send_caption("assistant", farewell_text, final=True)
            self.last_delivered_speech = farewell_text
            self.farewell = _FarewellState()
            if self.end_call_pending and not self.playbacks and not self.farewell.pending and not self.in_flight_generations:
                self.request_stop("completed")

    async def _finalize_response(self, generation: int) -> None:
        """Close a response once its text and the output of its segments are in.

        The end of the response and the output of its segments arrive in
        either order: the provider's audio can still be on its way when the
        response ends, and the transport can hand the end of the response on
        before the last segment has been played. A response is therefore only
        closed when both are in, so that a segment that is still on its way
        neither loses the reply nor lets the call end before it was spoken.
        """
        if generation <= 0:
            return
        if generation not in self.in_flight_generations:
            return
        if generation not in self.response_ended:
            # The end of the response has not been handed on yet: the reply is
            # not complete, so it may not be closed.
            return
        if generation not in self.response_complete:
            # The response itself has not ended yet.
            return
        if self.response_segments.get(generation):
            # A segment of this response has not produced its output yet.
            return

        text = "".join(self.response_text.get(generation, [])).strip()
        is_interrupted = generation in self.response_interrupted
        delivered = generation in self.delivered_generations
        self.response_text.pop(generation, None)
        self.response_complete.discard(generation)
        self.response_interrupted.discard(generation)
        self.response_ended.discard(generation)
        self.delivered_generations.discard(generation)
        self.in_flight_generations.discard(generation)
        self.response_segments.pop(generation, None)

        if not is_interrupted and delivered and text:
            self._persist_message(text)
            self.recent_conversation.append({"role": "assistant", "content": text})
            await self._send_caption("assistant", text, final=True)
            self.last_delivered_speech = text

        if (
            self.farewell.pending
            and self.farewell.mode == "natural"
            and self.farewell.generation == generation
        ):
            # The farewell of this reply has been spoken: it is over.
            self.farewell = _FarewellState()

        self._maybe_complete_call()

    def _maybe_complete_call(self) -> None:
        """End the call once everything it had to say has been said.

        A reply that is still being spoken keeps the call open: the caller
        may not be cut off in the middle of an answer, and a farewell that is
        still on its way has not been said yet.
        """
        if (
            self.end_call_pending
            and not self.playbacks
            and not self.farewell.pending
            and not self.in_flight_generations
        ):
            self.request_stop("completed")

    async def on_llm_response_completed(self, frame: LLMFullResponseEndFrame) -> None:
        generation = getattr(frame, "generation", None)
        if generation is None:
            return
        # The end of the response has been handed on: every segment of the
        # reply has been given to the TTS service by now. The reply itself is
        # only closed once the output of those segments is in as well, so a
        # segment that is still on its way neither loses the reply nor lets
        # the call end before it was spoken.
        self.response_ended.add(generation)
        await self._finalize_response(generation)
        # The reply may still be on its way, in which case the call stays
        # open; this is also the point where a call that has nothing left to
        # say is ended.
        self._maybe_complete_call()

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
            speak_frame = TTSSpeakFrame(text=message)
            speak_frame.interruptible = False
            await self.workflow_processor.push_frame(
                speak_frame,
                FrameDirection.DOWNSTREAM,
            )
        callback = getattr(params, "result_callback", None)
        if callable(callback):
            res = callback({"status": "call_ending"})
            if asyncio.iscoroutine(res):
                await res
        self.end_call_pending = True
        if not self.playbacks and not self.farewell.pending and not self.in_flight_generations:
            self.request_stop("completed")
            return

        async def _safety_stop() -> None:
            max_wait = 10.0 if message else 1.5
            poll_interval = 0.25
            waited = 0.0
            while waited < max_wait and not self.closing:
                await asyncio.sleep(poll_interval)
                waited += poll_interval
                if not self.playbacks and not self.farewell.pending and not self.in_flight_generations:
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
        self.in_flight_generations.add(generation)
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
            frame, InputAudioRawFrame
        ):
            self.runtime.note_input_audio(frame)
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
            # A new speech window needs a new end snapshot at its own VAD stop.
            self.runtime.azure_speech_end_ticks = None
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
            if self.runtime.definition.providers.stt_provider == "azure":
                if not self.runtime.vad_speech_active and not self.runtime.user_speaking:
                    return
                self.runtime.vad_speech_active = False
                self.runtime.user_speaking = False
                self.runtime.speech_generation += 1
                gen = self.runtime.speech_generation
                stop_secs = max(0.0, float(getattr(frame, "stop_secs", 0.0) or 0.0))
                # The last stop_secs of forwarded audio are the silence that made
                # VAD declare the stop, so the speech window ends that much earlier.
                self.runtime.azure_speech_end_ticks = max(
                    0,
                    self.runtime.input_audio_ticks
                    - int(stop_secs * _AZURE_TICKS_PER_SECOND),
                )
                self._schedule_azure_turn_end(gen)
            else:
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

    def _cancel_turn_task(self) -> None:
        task = self.runtime.flush_task
        if task is not None and not task.done():
            task.cancel()

    def _azure_turn_is_current(self, expected_gen: int) -> bool:
        return (
            not self.runtime.closing
            and not self.runtime.closed
            and self.runtime.speech_generation == expected_gen
            and not self.runtime.vad_speech_active
        )

    def _azure_turn_is_complete(self) -> bool:
        runtime = self.runtime
        if runtime.vad_speech_active or runtime.azure_interim_since_final:
            return False
        if not runtime.transcript_segments:
            return False
        target = runtime.azure_speech_end_ticks
        if target is None:
            return True
        final_end = runtime.azure_turn_final_end_ticks
        if final_end is None:
            # Without Azure timing there is no way to prove later audio was
            # missed, so accept the recognized text.
            return True
        return final_end + runtime.azure_tolerance_ticks() >= target

    def _schedule_azure_turn_end(self, expected_gen: int) -> None:
        if self.runtime.closing or self.runtime.closed:
            return
        self._cancel_turn_task()
        self.runtime.flush_task = asyncio.create_task(
            self._azure_turn_end(expected_gen)
        )

    async def _azure_turn_end(self, expected_gen: int) -> None:
        try:
            await asyncio.sleep(_AZURE_TURN_QUIET_SECONDS)
        except asyncio.CancelledError:
            return
        if not self._azure_turn_is_current(expected_gen):
            return
        if not self._azure_turn_is_complete():
            # Keep the buffered speech pending for a covering final. Do not
            # flush known-incomplete text just because the deadline expired.
            return
        await self._submit_azure_turn()

    async def _submit_azure_turn(self) -> None:
        text = " ".join(self.runtime.transcript_segments).strip()
        self.runtime.transcript_segments.clear()
        self.runtime.azure_speech_end_ticks = None
        self.runtime.azure_turn_final_end_ticks = None
        self.runtime.azure_interim_since_final = False
        self.runtime.speech_generation += 1
        task = self.runtime.flush_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        if text:
            await self.runtime.handle_final_transcription(text)

    @staticmethod
    def _azure_interval(frame: Frame) -> tuple[int, int] | None:
        result = getattr(frame, "result", None)
        inner = getattr(result, "result", None)
        offset = getattr(inner, "offset", None) if inner is not None else None
        duration = getattr(inner, "duration", None) if inner is not None else None
        if offset is None:
            offset = getattr(result, "offset", None)
        if duration is None:
            duration = getattr(result, "duration", None)
        if offset is None or duration is None:
            return None
        try:
            o_int = int(offset)
            d_int = int(duration)
            if o_int < 0 or d_int < 0:
                return None
            return o_int, d_int
        except (TypeError, ValueError):
            return None

    async def _send_azure_partial_caption(self) -> None:
        text = " ".join(self.runtime.transcript_segments).strip()
        if text:
            await self.runtime._send_caption("user", text, final=False)

    async def _handle_azure_final(self, frame: Frame, raw_text: str) -> None:
        runtime = self.runtime
        interval = self._azure_interval(frame)
        if interval is not None:
            end_ticks = interval[0] + interval[1]
            accepted_end = runtime.azure_accepted_end_ticks
            if accepted_end is not None and end_ticks <= accepted_end:
                return
            # A monotonic accepted-end watermark deduplicates retransmissions
            # without collapsing repeated text that has a real new interval.
            runtime.azure_accepted_end_ticks = end_ticks
            runtime.azure_turn_final_end_ticks = max(
                runtime.azure_turn_final_end_ticks or 0, end_ticks
            )

        runtime.azure_interim_since_final = False
        if raw_text:
            runtime.transcript_segments.append(raw_text)
            total_chars = (
                sum(map(len, runtime.transcript_segments))
                + len(runtime.transcript_segments)
                - 1
            )
            if total_chars > _MAX_TRANSCRIPT_CHARS:
                raise VoiceRuntimeError(
                    "Caller speech exceeded the per-turn transcript limit."
                )
            await self._send_azure_partial_caption()

        if runtime.vad_speech_active:
            return

        task = runtime.flush_task
        if task is not None and not task.done():
            # The VAD-stop deadline owns the decision. A final arriving inside
            # the quiet window must not bypass or restart resumption
            # cancellation; it only updates the evidence the deadline checks.
            return
        if not self._azure_turn_is_complete():
            return
        await self._submit_azure_turn()


    async def _handle_transcription_frame(self, frame: Frame) -> None:
        if isinstance(frame, InterimTranscriptionFrame):
            raw_text = frame.text.strip()
            if raw_text:
                if (
                    self.runtime.definition.providers.stt_provider == "azure"
                    and self.runtime.transcript_segments
                ):
                    self.runtime.azure_interim_since_final = True
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
        finalized = bool(getattr(frame, "finalized", False))
        is_final = finalized or bool(getattr(result, "is_final", False))
        speech_final = finalized or bool(getattr(result, "speech_final", False))
        from_finalize = bool(getattr(result, "from_finalize", False))
        raw_text = getattr(frame, "text", "").strip()
        if raw_text and self.runtime.is_bot_speaking():
            self.runtime.transcript_overlapped_playback = True
            if not _is_isolated_backchannel(raw_text):
                self.runtime.note_interruption()
                await self.broadcast_interruption()

        if self.runtime.definition.providers.stt_provider == "azure":
            if is_final:
                await self._handle_azure_final(frame, raw_text)
            return

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
                gen = self.runtime.begin_llm_response()
                setattr(frame, "generation", gen)
            elif isinstance(frame, LLMTextFrame):
                full_text = self.runtime.append_llm_text(frame.text)
                if full_text:
                    await self.runtime._send_caption("assistant", full_text, final=False)
            elif isinstance(frame, LLMFullResponseEndFrame):
                setattr(frame, "generation", self.runtime.active_response_generation)
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
        self.uninterruptible_contexts: set[str] = set()
        self.current_generation: int | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        stop_due_to_error = False
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                gen = getattr(frame, "generation", None)
                if gen is not None:
                    self.current_generation = gen
            elif isinstance(frame, LLMFullResponseEndFrame):
                self.current_generation = None
            elif isinstance(frame, InterruptionFrame):
                self.current_generation = None
                # Discard only interruptible contexts; preserve uninterruptible ones
                self.audio_contexts.intersection_update(self.uninterruptible_contexts)
            elif isinstance(frame, TTSStartedFrame):
                farewell = getattr(self.runtime, "farewell", None)
                req_texts = getattr(self.runtime, "tts_request_texts", {})
                is_explicit_farewell = (
                    farewell is not None
                    and getattr(farewell, "pending", False)
                    and getattr(farewell, "mode", None) == "explicit"
                    and (
                        (
                            getattr(farewell, "context_id", None) is not None
                            and getattr(farewell, "context_id", None) == frame.context_id
                        )
                        or (
                            getattr(farewell, "message", None)
                            and req_texts.get(frame.context_id, "").strip().casefold()
                            == str(farewell.message).strip().casefold()
                        )
                    )
                )
                interruptible = not is_explicit_farewell and getattr(frame, "interruptible", True)
                if not interruptible:
                    frame.interruptible = False
                    if frame.context_id is not None:
                        self.uninterruptible_contexts.add(frame.context_id)
                try:
                    await self.runtime.on_tts_started(
                        frame,
                        generation=self.current_generation,
                        interruptible=interruptible,
                    )
                except TypeError:
                    await self.runtime.on_tts_started(frame)
            elif isinstance(frame, TTSAudioRawFrame) and frame.context_id is not None:
                self.audio_contexts.add(frame.context_id)
                if frame.context_id in self.uninterruptible_contexts or not getattr(frame, "interruptible", True):
                    self.uninterruptible_contexts.add(frame.context_id)
                    frame.interruptible = False
            elif isinstance(frame, TTSStoppedFrame) and frame.context_id is not None:
                if frame.context_id in self.uninterruptible_contexts or not getattr(frame, "interruptible", True):
                    self.uninterruptible_contexts.add(frame.context_id)
                    frame.interruptible = False
            elif isinstance(frame, ErrorFrame):
                self.audio_contexts.clear()
                self.uninterruptible_contexts.clear()
                stop_due_to_error = True
        await self.push_frame(frame, direction)
        if stop_due_to_error:
            self.runtime.request_stop("failed")


class _OutputCompletionProcessor(FrameProcessor):
    def __init__(
        self,
        runtime: CallRuntime,
        audio_contexts: set[str],
        uninterruptible_contexts: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.audio_contexts = audio_contexts
        self.uninterruptible_contexts = (
            uninterruptible_contexts if uninterruptible_contexts is not None else set()
        )

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
                    self.uninterruptible_contexts.discard(frame.context_id)
        await self.push_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSStoppedFrame):
                await self.runtime.on_tts_stopped(
                    frame,
                    transport_completed=transport_completed,
                )
            elif isinstance(frame, LLMFullResponseEndFrame):
                if hasattr(self.runtime, "on_llm_response_completed"):
                    await self.runtime.on_llm_response_completed(frame)
            elif isinstance(frame, ErrorFrame):
                self.runtime.request_stop("failed")

def create_call_runtime(
    session_id: uuid.UUID,
    call_type: Literal["test", "public"],
    snapshot: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    *,
    tts_cache: TTSAudioCache | None = None,
) -> CallRuntime:
    return CallRuntime.create(
        session_id,
        call_type,
        snapshot,
        env,
        tts_cache=tts_cache,
    )


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
