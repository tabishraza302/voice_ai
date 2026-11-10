
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection

from app.voice import (
    CallDefinition,
    CallRuntime,
    VoiceUnavailableError,
    _OutputCompletionProcessor,
    _Playback,
    _TTSContextTracker,
    provider_publish_errors,
    validate_tts_voice_selection,
)
from voice_pipeline_harness import verified_environment


@pytest.fixture
def anyio_backend():
    return "asyncio"


def provider_agent(**overrides: object) -> dict[str, object]:
    agent: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "status": "published",
        "languages": ["en"],
        "stt_provider": "deepgram",
        "stt_model": "nova-3",
        "llm_provider": "openai",
        "llm_model": "gpt-4o-mini",
        "tts_provider": "cartesia",
        "tts_model": "sonic-3.6",
        "voice_id": "cartesia-en",
    }
    agent.update(overrides)
    return agent




def voice_snapshot() -> dict[str, object]:
    return {
        "global_prompt": {"text": "Global call policy", "version": 7},
        "policy": {
            "max_call_duration_seconds": 900,
            "max_concurrent_calls_per_agent": 2,
            "max_concurrent_calls_overall": 8,
        },
        "language": "en",
        "agent": {
            **provider_agent(),
            "name": "Reception",
            "instructions": "Help the caller.",
        },
    }


def make_definition() -> CallDefinition:
    return CallDefinition.from_snapshot(
        uuid.uuid4(),
        "test",
        voice_snapshot(),
        verified_environment(),
    )

class FakeWriter:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.answers: list[tuple[str, object, bool, dict[str, object]]] = []
        self.events: list[str] = []
        self.on_drain = None

    def enqueue_message(self, role: str, content: str) -> None:
        self.messages.append((role, content))

    def enqueue_answer(
        self,
        field_key: str,
        value: object,
        accepted: bool,
        validation_result: dict[str, object],
    ) -> None:
        self.events.append(f"answer:{field_key}")
        self.answers.append((field_key, value, accepted, validation_result))

    async def drain(self) -> None:
        self.events.append("drain")
        if self.on_drain is not None:
            self.on_drain()

    async def flush(self) -> None:
        return None


class RecordingProcessor:
    def __init__(self) -> None:
        self.frames: list[tuple[Frame, FrameDirection]] = []

    async def push_frame(self, frame: Frame, direction: FrameDirection) -> None:
        self.frames.append((frame, direction))


class ToolParams:
    def __init__(
        self,
        arguments: dict[str, object],
        context: LLMContext,
        result_callback,
    ) -> None:
        self.function_name = "save_answers"
        self.arguments = arguments
        self.context = context
        self.result_callback = result_callback


def test_publishing_accepts_only_configured_provider_models_and_voice() -> None:
    assert provider_publish_errors(provider_agent(), verified_environment()) == {}


def test_publishing_rejects_arbitrary_model_ids() -> None:
    env = verified_environment()
    agent = provider_agent(llm_model="model-from-user", tts_model="invalid-tts-model")

    errors = provider_publish_errors(agent, env)

    assert "llm_model" in errors
    assert "tts_model" in errors

def test_publishing_fails_closed_without_provider_credentials() -> None:
    env = verified_environment()
    env.pop("OPENAI_API_KEY")
    env.pop("CARTESIA_API_KEY")

    errors = provider_publish_errors(provider_agent(), env)

    assert "llm_provider" in errors
    assert "voice_id" in errors


def test_publishing_fails_closed_without_voice_id_when_credentials_are_present() -> None:
    env = verified_environment()
    agent = provider_agent()
    agent["voice_id"] = ""

    errors = provider_publish_errors(agent, env)

    assert "voice_id" in errors

def test_multilingual_runtime_is_not_published_until_tts_switching_is_verified() -> None:
    errors = provider_publish_errors(provider_agent(languages=["en", "hi"]), verified_environment())

    assert "languages" in errors


def test_voice_selection_validates_provider_model_and_language() -> None:
    env = verified_environment()

    assert validate_tts_voice_selection(
        provider="cartesia",
        model="sonic-3.6",
        voice_id="any-voice",
        language="en",
        env=env,
    ) is None
    assert validate_tts_voice_selection(
        provider="cartesia",
        model="invalid-model",
        voice_id="any-voice",
        language="en",
        env=env,
    ) is not None
    assert validate_tts_voice_selection(
        provider="cartesia",
        model="sonic-3.6",
        voice_id="",
        language="en",
        env=env,
    ) is not None
    assert validate_tts_voice_selection(
        provider="elevenlabs",
        model="sonic-3.6",
        voice_id="any-voice",
        language="en",
        env=env,
    ) is not None

def test_call_definition_copies_frozen_snapshot_and_provider_keys() -> None:
    snapshot = {
        "global_prompt": {"text": "Global call policy", "version": 7},
        "policy": {
            "max_call_duration_seconds": 900,
            "max_concurrent_calls_per_agent": 2,
            "max_concurrent_calls_overall": 8,
        },
        "language": "en",
        "agent": {
            **provider_agent(),
            "name": "Reception",
            "instructions": "Help the caller.",
            "languages": ["en"],
        },
    }
    env = verified_environment()

    definition = CallDefinition.from_snapshot(uuid.uuid4(), "test", snapshot, env)

    snapshot["global_prompt"]["text"] = "Changed prompt"
    snapshot["agent"]["instructions"] = "Changed instructions"
    env["OPENAI_API_KEY"] = "rotated-key"

    assert definition.global_prompt == "Global call policy"
    assert definition.prompt_version == 7
    assert definition.agent_instructions == "Help the caller."
    assert definition.providers.llm_api_key == "openai-test-secret"
    assert definition.providers.tts_api_key == "cartesia-test-secret"


def test_call_definition_rejects_missing_provider_credentials() -> None:
    snapshot = {
        "global_prompt": {"text": "Global call policy", "version": 1},
        "policy": {
            "max_call_duration_seconds": 900,
            "max_concurrent_calls_per_agent": 1,
            "max_concurrent_calls_overall": 4,
        },
        "language": "en",
        "agent": {
            **provider_agent(),
            "name": "Reception",
            "instructions": "Help the caller.",
            "languages": ["en"],
        },
    }
    env = verified_environment()
    env.pop("DEEPGRAM_API_KEY")

    with pytest.raises(VoiceUnavailableError):
        CallDefinition.from_snapshot(uuid.uuid4(), "test", snapshot, env)


def test_publishing_accepts_azure_speech_with_region_or_endpoint() -> None:
    env = verified_environment()
    azure_agent = provider_agent(
        stt_provider="azure",
        stt_model="azure-speech",
        azure_stt_english_locale="en-IN",
    )
    # Region only
    assert provider_publish_errors(azure_agent, env) == {}

    # Endpoint only (overriding / without region)
    env_endpoint = dict(env)
    env_endpoint.pop("AZURE_SPEECH_REGION")
    env_endpoint["AZURE_SPEECH_ENDPOINT"] = "https://eastus.api.cognitive.microsoft.com"
    assert provider_publish_errors(azure_agent, env_endpoint) == {}


def test_publishing_rejects_unsupported_stt_provider() -> None:
    env = verified_environment()
    agent = provider_agent(stt_provider="unsupported-stt")
    errors = provider_publish_errors(agent, env)
    assert errors.get("stt_provider") == ["Choose Deepgram or Azure for speech recognition."]


@pytest.mark.parametrize(
    ("provider", "wrong_model", "expected_message"),
    [
        ("azure", "nova-3", "The supported azure model is azure-speech."),
        ("deepgram", "azure-speech", "The supported deepgram model is nova-3."),
    ],
)
def test_publishing_rejects_mismatched_stt_model(
    provider: str, wrong_model: str, expected_message: str
) -> None:
    env = verified_environment()
    agent = provider_agent(stt_provider=provider, stt_model=wrong_model)
    errors = provider_publish_errors(agent, env)
    assert errors.get("stt_model") == [expected_message]


def test_publishing_azure_speech_fails_closed_without_server_credentials() -> None:
    env = verified_environment()
    azure_agent = provider_agent(
        stt_provider="azure",
        stt_model="azure-speech",
        azure_stt_english_locale="en-IN",
    )

    # Missing / whitespace AZURE_SPEECH_API_KEY
    env_no_key = dict(env)
    env_no_key["AZURE_SPEECH_API_KEY"] = "   "
    errors_no_key = provider_publish_errors(azure_agent, env_no_key)
    assert "Speech recognition is unavailable because AZURE_SPEECH_API_KEY is not configured on the server." in errors_no_key.get("stt_provider", [])

    # Missing both AZURE_SPEECH_REGION and AZURE_SPEECH_ENDPOINT
    env_no_routing = dict(env)
    env_no_routing.pop("AZURE_SPEECH_REGION")
    errors_no_routing = provider_publish_errors(azure_agent, env_no_routing)
    assert "Azure speech recognition requires AZURE_SPEECH_REGION or AZURE_SPEECH_ENDPOINT on the server." in errors_no_routing.get("stt_provider", [])


@pytest.mark.parametrize("locale", ["en-IN", "en-US", "en-GB"])
def test_publishing_azure_speech_accepts_valid_english_locales(locale: str) -> None:
    env = verified_environment()
    agent = provider_agent(
        stt_provider="azure",
        stt_model="azure-speech",
        languages=["en"],
        azure_stt_english_locale=locale,
    )
    assert provider_publish_errors(agent, env) == {}


@pytest.mark.parametrize("locale", ["en-FR", "invalid", None, ""])
def test_publishing_azure_speech_rejects_invalid_english_locales(locale: str | None) -> None:
    env = verified_environment()
    agent = provider_agent(
        stt_provider="azure",
        stt_model="azure-speech",
        languages=["en"],
        azure_stt_english_locale=locale,
    )
    errors = provider_publish_errors(agent, env)
    assert errors.get("azure_stt_english_locale") == [
        "Choose an Azure English locale: en-IN, en-US, or en-GB."
    ]


def test_publishing_azure_speech_hindi_and_non_english_ignore_locale() -> None:
    env = verified_environment()
    # Hindi call language ignores dormant English locale preference
    hindi_agent = provider_agent(
        stt_provider="azure",
        stt_model="azure-speech",
        languages=["hi"],
        azure_stt_english_locale=None,
        voice_id="cartesia-hi",
    )
    assert provider_publish_errors(hindi_agent, env) == {}

    # Unsupported language produces language error without superfluous locale error
    unsupported_lang_agent = provider_agent(
        stt_provider="azure",
        stt_model="azure-speech",
        languages=["fr"],
        azure_stt_english_locale=None,
    )
    errors = provider_publish_errors(unsupported_lang_agent, env)
    assert "languages" in errors
    assert "azure_stt_english_locale" not in errors


def test_provider_credential_isolation_between_azure_and_deepgram() -> None:
    # Azure agent does not require DEEPGRAM_API_KEY
    azure_env = verified_environment()
    azure_env.pop("DEEPGRAM_API_KEY")
    azure_snapshot = {
        "global_prompt": {"text": "Global policy", "version": 1},
        "policy": {
            "max_call_duration_seconds": 900,
            "max_concurrent_calls_per_agent": 1,
            "max_concurrent_calls_overall": 4,
        },
        "language": "en",
        "agent": {
            **provider_agent(
                stt_provider="azure",
                stt_model="azure-speech",
                azure_stt_english_locale="en-GB",
            ),
            "instructions": "Help caller",
            "languages": ["en"],
        },
    }
    definition = CallDefinition.from_snapshot(uuid.uuid4(), "test", azure_snapshot, azure_env)
    assert definition.providers.stt_provider == "azure"
    assert definition.providers.stt_api_key == "azure-test-secret"

    # Deepgram agent does not require AZURE_SPEECH_API_KEY
    dg_env = verified_environment()
    dg_env.pop("AZURE_SPEECH_API_KEY", None)
    dg_snapshot = {
        "global_prompt": {"text": "Global policy", "version": 1},
        "policy": {
            "max_call_duration_seconds": 900,
            "max_concurrent_calls_per_agent": 1,
            "max_concurrent_calls_overall": 4,
        },
        "language": "en",
        "agent": {
            **provider_agent(stt_provider="deepgram", stt_model="nova-3"),
            "instructions": "Help caller",
            "languages": ["en"],
        },
    }
    dg_definition = CallDefinition.from_snapshot(uuid.uuid4(), "test", dg_snapshot, dg_env)
    assert dg_definition.providers.stt_provider == "deepgram"
    assert dg_definition.providers.stt_api_key == "deepgram-test-secret"


def test_call_definition_resolves_endpoint_over_region() -> None:
    env = verified_environment()
    env["AZURE_SPEECH_API_KEY"] = "  azure-secret  "
    env["AZURE_SPEECH_REGION"] = "  eastus  "
    env["AZURE_SPEECH_ENDPOINT"] = "  https://my-endpoint.azure.com  "
    snapshot = {
        "global_prompt": {"text": "Global policy", "version": 1},
        "policy": {
            "max_call_duration_seconds": 900,
            "max_concurrent_calls_per_agent": 1,
            "max_concurrent_calls_overall": 4,
        },
        "language": "en",
        "agent": {
            **provider_agent(
                stt_provider="azure",
                stt_model="azure-speech",
                azure_stt_english_locale="en-GB",
            ),
            "instructions": "Help caller",
            "languages": ["en"],
        },
    }
    # Both set: endpoint wins, region is None, whitespace stripped
    definition = CallDefinition.from_snapshot(uuid.uuid4(), "test", snapshot, env)
    assert definition.providers.stt_endpoint == "https://my-endpoint.azure.com"
    assert definition.providers.stt_region is None

    # Region only: region is set, endpoint is None
    env_region_only = dict(env)
    env_region_only.pop("AZURE_SPEECH_ENDPOINT")
    definition_reg = CallDefinition.from_snapshot(uuid.uuid4(), "test", snapshot, env_region_only)
    assert definition_reg.providers.stt_region == "eastus"
    assert definition_reg.providers.stt_endpoint is None


def test_call_definition_resolves_effective_stt_language() -> None:
    env = verified_environment()
    # Azure English uses snapshot's exact locale without fallback
    azure_en_snapshot = {
        "global_prompt": {"text": "Global policy", "version": 1},
        "policy": {
            "max_call_duration_seconds": 900,
            "max_concurrent_calls_per_agent": 1,
            "max_concurrent_calls_overall": 4,
        },
        "language": "en",
        "agent": {
            **provider_agent(
                stt_provider="azure",
                stt_model="azure-speech",
                azure_stt_english_locale="en-GB",
            ),
            "instructions": "Help caller",
            "languages": ["en"],
        },
    }
    def_en = CallDefinition.from_snapshot(uuid.uuid4(), "test", azure_en_snapshot, env)
    assert def_en.providers.stt_language == "en-GB"

    # Azure Hindi uses hi-IN
    azure_hi_snapshot = {
        **azure_en_snapshot,
        "language": "hi",
        "agent": {
            **provider_agent(
                stt_provider="azure",
                stt_model="azure-speech",
                languages=["hi"],
                azure_stt_english_locale="en-GB",
                voice_id="cartesia-hi",
            ),
            "instructions": "Help caller",
            "languages": ["hi"],
        },
    }
    def_hi = CallDefinition.from_snapshot(uuid.uuid4(), "test", azure_hi_snapshot, env)
    assert def_hi.providers.stt_language == "hi-IN"

    # Deepgram uses call language (en or hi), region and endpoint are None
    dg_snapshot = {
        **azure_en_snapshot,
        "agent": {
            **provider_agent(stt_provider="deepgram", stt_model="nova-3"),
            "instructions": "Help caller",
            "languages": ["en"],
        },
    }
    def_dg = CallDefinition.from_snapshot(uuid.uuid4(), "test", dg_snapshot, env)
    assert def_dg.providers.stt_provider == "deepgram"
    assert def_dg.providers.stt_language == "en"
    assert def_dg.providers.stt_region is None
    assert def_dg.providers.stt_endpoint is None


class _CompletionRuntimeProbe:
    def __init__(self) -> None:
        self.started: list[str | None] = []
        self.completed: list[str | None] = []
        self.unconfirmed: list[str | None] = []

    async def on_tts_started(self, frame: TTSStartedFrame) -> None:
        self.started.append(frame.context_id)

    async def on_tts_stopped(
        self,
        frame: TTSStoppedFrame,
        *,
        transport_completed: bool,
    ) -> None:
        destination = self.completed if transport_completed else self.unconfirmed
        destination.append(frame.context_id)

    def request_stop(self, status: str) -> None:
        raise AssertionError(f"Unexpected runtime stop: {status}")


class _RecordingTTSContextTracker(_TTSContextTracker):
    def __init__(self, runtime: _CompletionRuntimeProbe) -> None:
        super().__init__(runtime)  # type: ignore[arg-type]
        self.forwarded: list[tuple[Frame, FrameDirection]] = []

    async def push_frame(self, frame: Frame, direction: FrameDirection) -> None:
        self.forwarded.append((frame, direction))


class _RecordingOutputCompletionProcessor(_OutputCompletionProcessor):
    def __init__(
        self,
        runtime: _CompletionRuntimeProbe,
        audio_contexts: set[str],
    ) -> None:
        super().__init__(runtime, audio_contexts)  # type: ignore[arg-type]
        self.forwarded: list[tuple[Frame, FrameDirection]] = []

    async def push_frame(self, frame: Frame, direction: FrameDirection) -> None:
        self.forwarded.append((frame, direction))


@pytest.mark.anyio
async def test_opening_completion_waits_for_context_audio_to_drain() -> None:
    runtime = _CompletionRuntimeProbe()
    tracker = _RecordingTTSContextTracker(runtime)
    observer = _RecordingOutputCompletionProcessor(runtime, tracker.audio_contexts)

    await tracker.process_frame(
        TTSStartedFrame(context_id="missing-audio"),
        FrameDirection.DOWNSTREAM,
    )
    await tracker.process_frame(
        TTSStoppedFrame(context_id="missing-audio"),
        FrameDirection.DOWNSTREAM,
    )
    await observer.process_frame(
        TTSStoppedFrame(context_id="missing-audio"),
        FrameDirection.DOWNSTREAM,
    )
    assert runtime.unconfirmed == ["missing-audio"]
    assert runtime.completed == []

    await tracker.process_frame(
        TTSStartedFrame(context_id="sent-audio"),
        FrameDirection.DOWNSTREAM,
    )
    await tracker.process_frame(
        TTSAudioRawFrame(
            audio=b"\x00\x00",
            sample_rate=24_000,
            num_channels=1,
            context_id="sent-audio",
        ),
        FrameDirection.DOWNSTREAM,
    )
    await tracker.process_frame(
        TTSStoppedFrame(context_id="sent-audio"),
        FrameDirection.DOWNSTREAM,
    )
    await observer.process_frame(
        TTSStoppedFrame(context_id="sent-audio"),
        FrameDirection.DOWNSTREAM,
    )

    assert runtime.completed == ["sent-audio"]


@pytest.mark.anyio
async def test_output_completion_is_bound_to_matching_tts_context() -> None:
    runtime = _CompletionRuntimeProbe()
    tracker = _RecordingTTSContextTracker(runtime)
    observer = _RecordingOutputCompletionProcessor(runtime, tracker.audio_contexts)

    await tracker.process_frame(TTSStartedFrame(context_id="old"), FrameDirection.DOWNSTREAM)
    await tracker.process_frame(TTSStartedFrame(context_id="new"), FrameDirection.DOWNSTREAM)
    await tracker.process_frame(
        TTSAudioRawFrame(
            audio=b"\x00\x00",
            sample_rate=24_000,
            num_channels=1,
            context_id="old",
        ),
        FrameDirection.DOWNSTREAM,
    )
    await tracker.process_frame(TTSStoppedFrame(context_id="new"), FrameDirection.DOWNSTREAM)
    await observer.process_frame(TTSStoppedFrame(context_id="new"), FrameDirection.DOWNSTREAM)
    await tracker.process_frame(TTSStoppedFrame(context_id="old"), FrameDirection.DOWNSTREAM)
    await observer.process_frame(TTSStoppedFrame(context_id="old"), FrameDirection.DOWNSTREAM)

    assert runtime.unconfirmed == ["new"]
    assert runtime.completed == ["old"]


@pytest.mark.anyio
async def test_successful_call_sends_only_safe_completion_event(monkeypatch) -> None:
    messages: list[dict[str, str]] = []

    class Connection:
        def send_app_message(self, message: dict[str, str]) -> None:
            messages.append(message)

    async def no_op_cleanup(runtime: CallRuntime) -> None:
        return None

    monkeypatch.setattr("app.voice._cleanup_runtime", no_op_cleanup)
    runtime = object.__new__(CallRuntime)
    runtime.closing = False
    runtime.definition = SimpleNamespace(session_id=uuid.uuid4())
    runtime.connection = Connection()
    runtime.termination_status = None

    runtime.request_stop("completed")

    assert messages == [{"type": "call.completed"}]
    await runtime.cleanup_task

@pytest.mark.anyio

@pytest.mark.anyio
async def test_transcription_pushes_dialogue_turn() -> None:
    definition = make_definition()
    writer = FakeWriter()
    runtime = CallRuntime(definition, writer)
    recorder = RecordingProcessor()
    runtime.workflow_processor = recorder

    await runtime.handle_final_transcription("Hello, my name is Bob.")

    assert len(writer.messages) == 1
    assert writer.messages[0] == ("user", "Hello, my name is Bob.")
    assert len(runtime.recent_conversation) == 1
    assert runtime.recent_conversation[0] == {"role": "user", "content": "Hello, my name is Bob."}
    assert len(recorder.frames) == 1
    frame, direction = recorder.frames[0]
    assert isinstance(frame, LLMContextFrame)
    assert direction == FrameDirection.DOWNSTREAM
    sys_msg = frame.context.messages[0]
    assert "Help the caller" in sys_msg["content"]
    user_msg = frame.context.messages[1]
    assert user_msg["content"] == "Hello, my name is Bob."


@pytest.mark.anyio
async def test_voice_runtime_handle_end_call_waits_for_playback(monkeypatch) -> None:
    messages: list[dict[str, str]] = []

    class Connection:
        def send_app_message(self, message: dict[str, str]) -> None:
            messages.append(message)

    async def no_op_cleanup(runtime: CallRuntime) -> None:
        return None

    monkeypatch.setattr("app.voice._cleanup_runtime", no_op_cleanup)

    definition = make_definition()
    writer = FakeWriter()
    runtime = CallRuntime(definition, writer)
    runtime.connection = Connection()

    # Simulate an active TTS playback
    runtime.playbacks["farewell-ctx"] = _Playback(
        context_id="farewell-ctx",
        kind="llm",
        generation=1,
    )
    runtime.response_text[1] = ["Goodbye and take care!"]
    runtime.response_complete.add(1)

    result_calls: list[dict[str, object]] = []

    async def mock_callback(res):
        result_calls.append(res)

    params = ToolParams(arguments={"reason": "finished"}, context=None, result_callback=mock_callback)

    await runtime._handle_end_call(params)

    assert result_calls == [{"status": "call_ending"}]
    assert runtime.end_call_pending is True
    assert runtime.closing is False
    assert messages == []

    # Complete the playback
    await runtime.on_tts_stopped(
        TTSStoppedFrame(context_id="farewell-ctx"),
        transport_completed=True,
    )

    assert runtime.closing is True
    assert runtime.termination_status == "completed"
    assert {"type": "call.completed"} in messages
    assert messages[-1] == {"type": "call.completed"}
    if runtime.cleanup_task:
        await runtime.cleanup_task


@pytest.mark.anyio
async def test_voice_runtime_handle_end_call_safety_timer(monkeypatch) -> None:
    messages: list[dict[str, str]] = []

    class Connection:
        def send_app_message(self, message: dict[str, str]) -> None:
            messages.append(message)

    async def no_op_cleanup(runtime: CallRuntime) -> None:
        return None

    monkeypatch.setattr("app.voice._cleanup_runtime", no_op_cleanup)

    definition = make_definition()
    writer = FakeWriter()
    runtime = CallRuntime(definition, writer)
    runtime.connection = Connection()

    result_calls: list[dict[str, object]] = []

    async def mock_callback(res):
        result_calls.append(res)

    params = ToolParams(arguments={}, context=None, result_callback=mock_callback)

    await runtime._handle_end_call(params)
    assert result_calls == [{"status": "call_ending"}]
    assert runtime.end_call_pending is True

    # Fast wait for safety timer
    import asyncio
    await asyncio.sleep(1.6)

    assert runtime.closing is True
    assert runtime.termination_status == "completed"
    assert messages == [{"type": "call.completed"}]
    if runtime.cleanup_task:
        await runtime.cleanup_task


@pytest.mark.parametrize(
    "existing_speech",
    [
        pytest.param(False, id="delayed-start"),
        pytest.param(True, id="prior-speech-drains"),
    ],
)
@pytest.mark.anyio
async def test_farewell_waits_for_delayed_tts_start_and_audio_completion(
    monkeypatch,
    existing_speech: bool,
) -> None:
    import asyncio

    from voice_pipeline_harness import RunningVoicePipeline

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="farewell",
        existing_speech=existing_speech,
    )
    try:
        await pipeline.start(monkeypatch)
        tts = pipeline.tts
        output = pipeline.output
        connection = pipeline.connection

        await asyncio.wait_for(tts.synthesis_requested.wait(), timeout=3)
        if existing_speech:
            await asyncio.wait_for(pipeline.tool_result_received.wait(), timeout=3)
            tts.finish_ordinary.set()
            await asyncio.wait_for(output.ordinary_audio_completed.wait(), timeout=3)
            await asyncio.sleep(0.05)
        else:
            await asyncio.sleep(0.4)
        premature_completion_message = (
            "call completed after prior speech drained before farewell TTS started"
            if existing_speech
            else "call completed before the delayed farewell TTS audio finished"
        )
        assert not any(
            message.get("type") == "call.completed" for message in connection.messages
        ), premature_completion_message

        tts.start_synthesis.set()
        await asyncio.wait_for(output.audio_completed.wait(), timeout=3)
        await asyncio.wait_for(connection.call_completed.wait(), timeout=3)
        if existing_speech:
            assert pipeline.events.index("ordinary-audio-complete") < pipeline.events.index(
                "farewell-audio-start"
            )
        assert pipeline.events.index("farewell-audio-complete") < pipeline.events.index(
            "call-completed"
        )
    finally:
        await pipeline.close()


@pytest.mark.anyio
@pytest.mark.parametrize("stt_provider", ["deepgram", "azure"])
async def test_late_final_transcription_is_captioned_answered_and_saved(
    monkeypatch, stt_provider: str
) -> None:
    import asyncio

    from voice_pipeline_harness import ASSISTANT_TEXT, CALLER_TEXT, RunningVoicePipeline

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="late-transcription",
        stt_provider=stt_provider,
    )
    try:
        await pipeline.start(monkeypatch)
        await asyncio.wait_for(pipeline.stt.vad_started.wait(), timeout=3)
        await asyncio.wait_for(pipeline.stt.vad_stopped.wait(), timeout=3)
        await asyncio.wait_for(pipeline.stt.late_transcription_sent.wait(), timeout=3)
        await asyncio.wait_for(pipeline.connection.any_user_caption.wait(), timeout=3)
        try:
            await asyncio.wait_for(pipeline.connection.user_final_caption.wait(), timeout=1.5)
        except asyncio.TimeoutError:
            pass
        caller_final_captions = [
            message
            for message in pipeline.connection.messages
            if message.get("type") == "caption"
            and message.get("role") == "user"
            and message.get("text") == CALLER_TEXT
            and message.get("final") is True
        ]
        assert len(caller_final_captions) == 1, "late final caller transcription was not surfaced as exactly one final caption"

        await asyncio.wait_for(pipeline.connection.assistant_final_caption.wait(), timeout=3)
        assert any(
            message.get("type") == "caption"
            and message.get("role") == "assistant"
            and message.get("text") == ASSISTANT_TEXT
            and message.get("final") is True
            for message in pipeline.connection.messages
        )
        assert len(pipeline.model_turn_requests) == 1

        assert pipeline.client is not None and pipeline.session_id is not None
        deadline = asyncio.get_running_loop().time() + 3
        messages: list[dict[str, object]] = []
        while True:
            saved_messages = await pipeline.client.get(
                f"/sessions/{pipeline.session_id}/messages"
            )
            assert saved_messages.status_code == 200
            messages = saved_messages.json()
            user_saved = [
                message["content"]
                for message in messages
                if message["role"] == "user"
            ] == [CALLER_TEXT]
            assistant_saved = any(
                message["role"] == "assistant" and message["content"] == ASSISTANT_TEXT
                for message in messages
            )
            if user_saved and assistant_saved:
                break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.05)
        # Assert exactly one saved caller message (prevents interim saves and duplicates)
        assert [message["content"] for message in messages if message["role"] == "user"] == [CALLER_TEXT]
        assert any(
            message["role"] == "assistant" and message["content"] == ASSISTANT_TEXT
            for message in messages
        )
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_hindi_runtime_exact_caption_and_saved_text(monkeypatch) -> None:
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline

    hindi_text = "मेरा नाम आरव है।"
    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="transcription",
        language="hi",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)
        await asyncio.wait_for(pipeline.stt.vad_started.wait(), timeout=3)
        await asyncio.wait_for(pipeline.stt.vad_stopped.wait(), timeout=3)

        # Deliver Hindi Azure interim followed by final transcription
        await pipeline.stt.deliver_transcription("मेरा नाम", is_final=False, speech_final=False)
        await pipeline.stt.deliver_transcription(hindi_text, is_final=True, speech_final=True)
        await asyncio.wait_for(pipeline.connection.user_final_caption.wait(), timeout=3)

        caller_final_captions = [
            message
            for message in pipeline.connection.messages
            if message.get("type") == "caption"
            and message.get("role") == "user"
            and message.get("final") is True
        ]
        assert len(caller_final_captions) == 1
        assert caller_final_captions[0].get("text") == hindi_text

        await asyncio.wait_for(pipeline.connection.assistant_final_caption.wait(), timeout=3)

        assert pipeline.client is not None and pipeline.session_id is not None
        deadline = asyncio.get_running_loop().time() + 3
        messages: list[dict[str, object]] = []
        while True:
            saved_messages = await pipeline.client.get(
                f"/sessions/{pipeline.session_id}/messages"
            )
            assert saved_messages.status_code == 200
            messages = saved_messages.json()
            user_saved = [
                message["content"]
                for message in messages
                if message["role"] == "user"
            ] == [hindi_text]
            assistant_saved = any(
                message["role"] == "assistant" and message["content"] == "नमस्ते आरव!"
                for message in messages
            )
            if user_saved and assistant_saved:
                break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.05)
        # Assert exactly one saved user message matching exact Hindi script
        assert [message["content"] for message in messages if message["role"] == "user"] == [hindi_text]
        assert len(pipeline.model_turn_requests) == 1
    finally:
        await pipeline.close()


async def _wait_for_saved_user_messages(
    pipeline: Any, expected_count: int, timeout: float = 3.0
) -> list[dict[str, Any]]:
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    messages: list[dict[str, Any]] = []
    while asyncio.get_running_loop().time() < deadline:
        assert pipeline.client is not None and pipeline.session_id is not None
        resp = await pipeline.client.get(f"/sessions/{pipeline.session_id}/messages")
        if resp.status_code == 200:
            messages = resp.json()
            user_messages = [m for m in messages if m["role"] == "user"]
            if len(user_messages) >= expected_count:
                return messages
        await asyncio.sleep(0.05)
    return messages


@pytest.mark.anyio
async def test_azure_multiple_final_segments_during_active_speech_accumulate_into_single_turn(
    monkeypatch,
) -> None:
    """Several Azure finalized segments while speech remains active produce NO model request;
    after final speech end they combine into one user turn."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        # Caller begins speaking
        await pipeline.stt.deliver_vad_start()
        await asyncio.sleep(0.02)

        # Segment 1 arrives while speech active
        await pipeline.stt.deliver_transcription("Thank you.", is_final=True, speech_final=True)
        await asyncio.sleep(0.05)
        assert len(pipeline.model_turn_requests) == 0, (
            f"Expected 0 model requests while speech active, got {pipeline.model_turn_requests}"
        )

        # Segment 2 arrives while speech active
        await pipeline.stt.deliver_transcription(
            "That is all I need.", is_final=True, speech_final=True
        )
        await asyncio.sleep(0.05)
        assert len(pipeline.model_turn_requests) == 0, (
            f"Expected 0 model requests while speech active, got {pipeline.model_turn_requests}"
        )

        # Segment 3 arrives while speech active
        await pipeline.stt.deliver_transcription("Goodbye.", is_final=True, speech_final=True)
        await asyncio.sleep(0.05)
        assert len(pipeline.model_turn_requests) == 0, (
            f"Expected 0 model requests while speech active, got {pipeline.model_turn_requests}"
        )

        # Caller stops speaking
        await pipeline.stt.deliver_vad_stop()

        # All segments combine into one single user turn
        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        expected_turn = "Thank you. That is all I need. Goodbye."
        assert pipeline.model_turn_requests == [expected_turn]

        # Verify exactly one final caption and saved message
        saved = await _wait_for_saved_user_messages(pipeline, 1)
        assert [m["content"] for m in saved if m["role"] == "user"] == [expected_turn]

        final_captions = [
            m["text"]
            for m in pipeline.connection.messages
            if m.get("type") == "caption" and m.get("role") == "user" and m.get("final") is True
        ]
        assert final_captions == [expected_turn]
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_final_transcription_before_vs_after_vad_stop_submits_exactly_once(
    monkeypatch,
) -> None:
    """Final result before VAD stop vs after stop works exactly once."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        # Case A: Final result arrives BEFORE VAD stop
        await pipeline.stt.deliver_vad_start()
        await asyncio.sleep(0.02)
        await pipeline.stt.deliver_transcription(
            "What are your business hours?", is_final=True, speech_final=True
        )
        await pipeline.stt.deliver_vad_stop()

        # Wait for Turn 1 to complete
        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert pipeline.model_turn_requests == ["What are your business hours?"]

        # Wait for assistant response to settle
        await asyncio.wait_for(pipeline.connection.assistant_final_caption.wait(), timeout=3)
        pipeline.connection.assistant_final_caption.clear()

        # Case B: Final result arrives AFTER VAD stop
        await pipeline.stt.deliver_vad_start()
        await asyncio.sleep(0.02)
        await pipeline.stt.deliver_vad_stop()
        await asyncio.sleep(0.05)
        await pipeline.stt.deliver_transcription(
            "Where are you located?", is_final=True, speech_final=True
        )

        # Wait for Turn 2 to complete
        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert pipeline.model_turn_requests == [
            "What are your business hours?",
            "Where are you located?",
        ]

        saved = await _wait_for_saved_user_messages(pipeline, 2)
        assert [m["content"] for m in saved if m["role"] == "user"] == [
            "What are your business hours?",
            "Where are you located?",
        ]
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_early_final_with_later_interim_and_vad_stop_waits_for_later_final(
    monkeypatch,
) -> None:
    """Earlier final segment plus later audio/interim followed by VAD stop waits for that later final rather than flushing partial text."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        # Caller begins speaking
        await pipeline.stt.deliver_vad_start()
        await asyncio.sleep(0.02)
        await pipeline.stt.deliver_audio(1_500)

        # Segment 1 finalizes at 0-1.5 s while the caller keeps talking.
        await pipeline.stt.deliver_transcription(
            "What is the weather in",
            is_final=True,
            speech_final=True,
            offset=0,
            duration=15_000_000,
        )

        # Caller continues speaking, interim arrives covering speech extending beyond segment 1
        await pipeline.stt.deliver_transcription(
            "What is the weather in Paris today",
            is_final=False,
            speech_final=False,
        )
        await pipeline.stt.deliver_audio(1_000)

        # Caller stops speaking
        await pipeline.stt.deliver_vad_stop()

        # The old runtime flushes the accumulated segments 0.4 s after VAD
        # stop. Observe beyond that fallback so a partial turn cannot pass.
        await asyncio.sleep(0.55)
        assert len(pipeline.model_turn_requests) == 0, (
            f"Expected no premature turn request, got {pipeline.model_turn_requests}"
        )

        # Later final segment arrives covering the remainder
        await pipeline.stt.deliver_transcription(
            "Paris today.",
            is_final=True,
            speech_final=True,
            offset=15_000_000,
            duration=10_000_000,
        )

        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        expected_turn = "What is the weather in Paris today."
        assert pipeline.model_turn_requests == [expected_turn]

        saved = await _wait_for_saved_user_messages(pipeline, 1)
        assert [m["content"] for m in saved if m["role"] == "user"] == [expected_turn]
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_earlier_final_with_later_audio_waits_for_covering_final(
    monkeypatch,
) -> None:
    """A prior final plus later caller audio with NO interim must not flush the
    prior text; the turn waits for a final whose offset/duration covers the
    later audio."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        # Caller speaks the first clause at 0-5 s and Azure finalizes it.
        await pipeline.stt.deliver_vad_start()
        await asyncio.sleep(0.02)
        await pipeline.stt.deliver_audio(5_000)
        await pipeline.stt.deliver_transcription(
            "What is the weather in",
            is_final=True,
            speech_final=True,
            offset=0,
            duration=50_000_000,
        )

        # Caller keeps speaking for another 4 s. No interim transcription is
        # produced; only the forwarded caller audio extends the timeline.
        await pipeline.stt.deliver_audio(4_000)
        await pipeline.stt.deliver_vad_stop()

        # Past the old 0.4 s fallback the prior segment must still be pending.
        await asyncio.sleep(0.55)
        assert pipeline.model_turn_requests == [], (
            f"Expected no turn from audio not covered by the prior final, got "
            f"{pipeline.model_turn_requests}"
        )

        # The later final covers 5-9 s and completes the single turn.
        await pipeline.stt.deliver_transcription(
            "Paris today.",
            is_final=True,
            speech_final=True,
            offset=50_000_000,
            duration=40_000_000,
        )

        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        expected_turn = "What is the weather in Paris today."
        assert pipeline.model_turn_requests == [expected_turn]

        saved = await _wait_for_saved_user_messages(pipeline, 1)
        assert [m["content"] for m in saved if m["role"] == "user"] == [expected_turn]
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_resumed_speech_during_pending_turn_cancels_turn_and_combines_segments(
    monkeypatch,
) -> None:
    """Speech resumes while a turn decision is pending: no premature request, all segments combined."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        # Caller speaks first clause
        await pipeline.stt.deliver_vad_start()
        await asyncio.sleep(0.02)
        await pipeline.stt.deliver_transcription(
            "Please schedule an appointment", is_final=True, speech_final=True
        )

        # Brief pause (turn decision pending)
        await pipeline.stt.deliver_vad_stop()
        await asyncio.sleep(0.02)

        # Caller resumes speaking before turn is flushed/completed
        await pipeline.stt.deliver_vad_start()
        await asyncio.sleep(0.05)

        # Verify no request was dispatched for the first segment
        assert len(pipeline.model_turn_requests) == 0, (
            f"Premature model turn request before speech resumed: {pipeline.model_turn_requests}"
        )

        # Caller finishes clause
        await pipeline.stt.deliver_transcription(
            "for tomorrow morning.", is_final=True, speech_final=True
        )

        # Caller stops speaking
        await pipeline.stt.deliver_vad_stop()

        # Both segments combine into one turn
        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        expected_turn = "Please schedule an appointment for tomorrow morning."
        assert pipeline.model_turn_requests == [expected_turn]

        saved = await _wait_for_saved_user_messages(pipeline, 1)
        assert [m["content"] for m in saved if m["role"] == "user"] == [expected_turn]
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_late_final_and_short_utterance_without_vad_complete_turn(
    monkeypatch,
) -> None:
    """Late final still completes, and short utterance without VAD works."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        # Case A: Short utterance without VAD frames (e.g. quick "Yes.")
        await pipeline.stt.deliver_transcription("Yes.", is_final=True, speech_final=True)

        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert pipeline.model_turn_requests == ["Yes."], (
            f"Short utterance without VAD was not submitted: {pipeline.model_turn_requests}"
        )

        await asyncio.wait_for(pipeline.connection.assistant_final_caption.wait(), timeout=3)
        pipeline.connection.assistant_final_caption.clear()

        # Case B: Late final after VAD stop
        await pipeline.stt.deliver_vad_start()
        await asyncio.sleep(0.02)
        await pipeline.stt.deliver_vad_stop()
        await asyncio.sleep(0.1)

        await pipeline.stt.deliver_transcription(
            "Can you help me with that?", is_final=True, speech_final=True
        )

        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert pipeline.model_turn_requests == ["Yes.", "Can you help me with that?"]

        saved = await _wait_for_saved_user_messages(pipeline, 2)
        assert [m["content"] for m in saved if m["role"] == "user"] == [
            "Yes.",
            "Can you help me with that?",
        ]
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_stale_or_repeated_final_does_not_duplicate_turn(
    monkeypatch,
) -> None:
    """Repeated/stale final results do not duplicate a submitted Azure turn when real offset/duration identifies the segment."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        # Speech starts and ends
        await pipeline.stt.deliver_vad_start()
        await asyncio.sleep(0.02)
        await pipeline.stt.deliver_transcription(
            "Hello I need assistance.",
            is_final=True,
            speech_final=True,
            offset=10_000_000,
            duration=15_000_000,
        )
        await pipeline.stt.deliver_vad_stop()

        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert pipeline.model_turn_requests == ["Hello I need assistance."]

        # Deliver the exact same final frame again (duplicate / retransmission)
        await pipeline.stt.deliver_transcription(
            "Hello I need assistance.",
            is_final=True,
            speech_final=True,
            offset=10_000_000,
            duration=15_000_000,
        )
        await asyncio.sleep(0.1)

        # Turn must NOT be duplicated
        assert pipeline.model_turn_requests == ["Hello I need assistance."], (
            f"Duplicate final frame triggered repeated turn: {pipeline.model_turn_requests}"
        )

        # Identical text on a genuinely new interval is a new utterance, not a
        # retransmission. The watermark accepts it without merging by text.
        await pipeline.stt.deliver_vad_start()
        await pipeline.stt.deliver_audio(2_000)
        await pipeline.stt.deliver_transcription(
            "Hello I need assistance.",
            is_final=True,
            speech_final=True,
            offset=20_000_000,
            duration=10_000_000,
        )
        await pipeline.stt.deliver_vad_stop()

        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert pipeline.model_turn_requests == [
            "Hello I need assistance.",
            "Hello I need assistance.",
        ], (
            "Repeated text with a distinct interval must not be dropped: "
            f"{pipeline.model_turn_requests}"
        )

        saved = await _wait_for_saved_user_messages(pipeline, 2)
        assert [m["content"] for m in saved if m["role"] == "user"] == [
            "Hello I need assistance.",
            "Hello I need assistance.",
        ]
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_delayed_suffix_keeps_turn_pending_past_deadline(
    monkeypatch,
) -> None:
    """A late suffix final still completes one turn; expired deadlines never
    flush text that does not cover the recorded speech window."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        await pipeline.stt.deliver_vad_start()
        await pipeline.stt.deliver_audio(2_000)
        await pipeline.stt.deliver_transcription(
            "I need to change",
            is_final=True,
            speech_final=True,
            offset=0,
            duration=10_000_000,
        )
        await pipeline.stt.deliver_vad_stop()

        # The prior final ends at 1 s but speech ran to 2 s. Stay pending well
        # past the old one-second partial-flush grace.
        await asyncio.sleep(1.15)
        assert pipeline.model_turn_requests == [], (
            f"Expired deadline flushed incomplete speech: {pipeline.model_turn_requests}"
        )

        await pipeline.stt.deliver_transcription(
            "my appointment.",
            is_final=True,
            speech_final=True,
            offset=10_000_000,
            duration=10_000_000,
        )

        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        expected_turn = "I need to change my appointment."
        assert pipeline.model_turn_requests == [expected_turn]

        saved = await _wait_for_saved_user_messages(pipeline, 1)
        assert [m["content"] for m in saved if m["role"] == "user"] == [expected_turn]
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_final_during_quiet_window_does_not_bypass_resumed_speech(
    monkeypatch,
) -> None:
    """A covering final inside the VAD-stop quiet window must not submit before
    the single deadline; resumed speech cancels that deadline instead."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        await pipeline.stt.deliver_vad_start()
        await pipeline.stt.deliver_audio(1_000)
        await pipeline.stt.deliver_vad_stop()

        # Final arrives while the VAD-stop deadline is pending.
        await asyncio.sleep(0.05)
        await pipeline.stt.deliver_transcription(
            "Please schedule",
            is_final=True,
            speech_final=True,
            offset=0,
            duration=10_000_000,
        )
        await asyncio.sleep(0.02)

        # Caller resumes before the deadline; nothing may have been submitted.
        await pipeline.stt.deliver_vad_start()
        await asyncio.sleep(0.3)
        assert pipeline.model_turn_requests == [], (
            f"Final in quiet window bypassed resumption: {pipeline.model_turn_requests}"
        )

        await pipeline.stt.deliver_audio(1_000)
        await pipeline.stt.deliver_transcription(
            "an appointment.",
            is_final=True,
            speech_final=True,
            offset=10_000_000,
            duration=10_000_000,
        )
        await pipeline.stt.deliver_vad_stop()

        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        expected_turn = "Please schedule an appointment."
        assert pipeline.model_turn_requests == [expected_turn]

        saved = await _wait_for_saved_user_messages(pipeline, 1)
        assert [m["content"] for m in saved if m["role"] == "user"] == [expected_turn]
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_stream_timeline_survives_post_stop_silence_and_next_turn(
    monkeypatch,
) -> None:
    """The speech-end target is snapshotted at VAD stop, and Azure offsets stay
    on one continuous stream timeline across silence and turns."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        # Speech ran to 2 s but the final only covers 0-1 s.
        await pipeline.stt.deliver_vad_start()
        await pipeline.stt.deliver_audio(2_000)
        await pipeline.stt.deliver_transcription(
            "What is the weather in",
            is_final=True,
            speech_final=True,
            offset=0,
            duration=10_000_000,
        )
        await pipeline.stt.deliver_vad_stop()

        # Deadline expires without a covering final.
        await asyncio.sleep(0.3)
        assert pipeline.model_turn_requests == []

        # Post-stop silence extends the stream to 4 s but must not move the
        # recorded 2 s speech-end target.
        await pipeline.stt.deliver_audio(2_000)
        await pipeline.stt.deliver_transcription(
            "Paris today.",
            is_final=True,
            speech_final=True,
            offset=10_000_000,
            duration=10_000_000,
        )

        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert pipeline.model_turn_requests == [
            "What is the weather in Paris today."
        ], (
            "Post-stop silence moved the speech-end target: "
            f"{pipeline.model_turn_requests}"
        )

        # Turn two starts at 4 s on the same stream and ends at 5 s.
        await pipeline.stt.deliver_vad_start()
        await pipeline.stt.deliver_audio(1_000)
        await pipeline.stt.deliver_transcription(
            "Thanks.",
            is_final=True,
            speech_final=True,
            offset=40_000_000,
            duration=10_000_000,
        )
        await pipeline.stt.deliver_vad_stop()

        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert pipeline.model_turn_requests == [
            "What is the weather in Paris today.",
            "Thanks.",
        ]
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_call_termination_with_pending_speech_cleans_up_cleanly(
    monkeypatch,
) -> None:
    """When a call terminates while a complete turn is buffered and quiet task
    is pending, the pending task is cancelled, runtime closes, and no post-hangup
    model request or persisted user turn is dispatched."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        # Deliver speech and covering final
        await pipeline.stt.deliver_vad_start()
        await pipeline.stt.deliver_audio(1_000)
        await pipeline.stt.deliver_transcription(
            "I need help with my reservation.",
            is_final=True,
            speech_final=True,
            offset=0,
            duration=10_000_000,
        )
        await pipeline.stt.deliver_vad_stop()

        # The covering final arrived and quiet timer is actively pending.
        # Hang up before the quiet deadline expires.
        await asyncio.sleep(0.02)
        assert pipeline.runtime.flush_task is not None
        assert not pipeline.runtime.flush_task.done()
        pipeline.runtime.request_stop("completed")

        # Allow time past the quiet deadline to ensure no late dispatch occurs
        await asyncio.sleep(0.55)
        assert pipeline.model_turn_requests == [], (
            f"Orphaned task dispatched model turn after call termination: {pipeline.model_turn_requests}"
        )

        await pipeline.close(close_client=False)

        assert pipeline.runtime.closed is True
        assert pipeline.runtime.termination_status == "completed"
        assert pipeline.model_turn_requests == []
        resp = await pipeline.client.get(f"/sessions/{pipeline.session_id}/messages")
        assert resp.status_code == 200
        user_msgs = [m for m in resp.json() if m["role"] == "user"]
        assert user_msgs == [], f"User turn was persisted after termination: {user_msgs}"
    finally:
        await pipeline.close(close_client=True)


@pytest.mark.anyio
async def test_azure_duplicate_vad_stop_during_quiet_window_preserves_single_turn(
    monkeypatch,
) -> None:
    """start->final/stop->duplicate stop during quiet window->covering final while
    timer pending must yield exactly one turn without deadline invalidation."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        await pipeline.stt.deliver_vad_start()
        await pipeline.stt.deliver_audio(2_000)
        # First final segment arrives
        await pipeline.stt.deliver_transcription(
            "Please schedule",
            is_final=True,
            speech_final=True,
            offset=0,
            duration=10_000_000,
        )
        await pipeline.stt.deliver_vad_stop()

        # Duplicate/redundant stop arrives during the active quiet window
        await asyncio.sleep(0.05)
        assert pipeline.runtime.flush_task is not None
        assert not pipeline.runtime.flush_task.done()
        await pipeline.stt.deliver_vad_stop()

        # Covering final arrives while the timer is still pending
        await asyncio.sleep(0.05)
        await pipeline.stt.deliver_transcription(
            "an appointment.",
            is_final=True,
            speech_final=True,
            offset=10_000_000,
            duration=10_000_000,
        )

        # Wait for the quiet deadline to complete the single combined turn
        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        expected_turn = "Please schedule an appointment."
        assert pipeline.model_turn_requests == [expected_turn], (
            f"Expected exactly one combined turn after duplicate stop, got: {pipeline.model_turn_requests}"
        )

        saved = await _wait_for_saved_user_messages(pipeline, 1)
        assert [m["content"] for m in saved if m["role"] == "user"] == [expected_turn]
    finally:
        await pipeline.close()


@pytest.mark.anyio
async def test_azure_internal_pause_hysteresis_does_not_prematurely_split_turn(
    monkeypatch,
) -> None:
    """An internal pause of ~410ms where VAD stop occurs at 350ms and resumed
    speech start is confirmed ~410ms after stop must not prematurely flush the
    prior clause before resumed speech cancels the turn."""
    import asyncio
    from voice_pipeline_harness import RunningVoicePipeline, verified_environment

    pipeline = RunningVoicePipeline(
        verified_environment(),
        scenario="turn-handling",
        stt_provider="azure",
    )
    try:
        await pipeline.start(monkeypatch)

        # Caller speaks first clause ("Thank you. That is all I need.")
        await pipeline.stt.deliver_vad_start()
        await pipeline.stt.deliver_audio(2_000)
        await pipeline.stt.deliver_transcription(
            "Thank you. That is all I need.",
            is_final=True,
            speech_final=True,
            offset=0,
            duration=20_000_000,
        )

        # 410 ms pause begins. At 350 ms of silence, VAD reports stopped speaking.
        await pipeline.stt.deliver_audio(350)
        await pipeline.stt.deliver_vad_stop(stop_secs=0.35)

        # The remaining 60 ms of the 410 ms pause passes, then caller speaks
        # "Goodbye.", which requires 350 ms of speech before VAD detects start.
        # Total time from VAD stop to VAD start is 60ms + 350ms = 410ms.
        # Check at 250 ms after stop that no premature turn request was dispatched.
        await asyncio.sleep(0.25)
        assert pipeline.model_turn_requests == [], (
            f"Prior clause prematurely flushed during internal pause: {pipeline.model_turn_requests}"
        )

        # Audio advances through the remaining pause (60ms) and speech (350ms).
        # At 410 ms after stop, VAD analyzer confirms resumed speech.
        # Ensure a real 160 ms elapses so total wall-clock delay reaches 410 ms.
        await asyncio.sleep(0.16)
        await pipeline.stt.deliver_audio(160)
        await pipeline.stt.deliver_audio(250)
        await pipeline.stt.deliver_vad_start()

        # Caller finishes "Goodbye." and stops
        await pipeline.stt.deliver_audio(500)
        await pipeline.stt.deliver_transcription(
            "Goodbye.",
            is_final=True,
            speech_final=True,
            offset=24_100_000,
            duration=8_500_000,
        )
        await pipeline.stt.deliver_vad_stop(stop_secs=0.35)

        # Wait for the single combined turn
        deadline = asyncio.get_running_loop().time() + 3.0
        while len(pipeline.model_turn_requests) < 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)

        expected_turn = "Thank you. That is all I need. Goodbye."
        assert pipeline.model_turn_requests == [expected_turn], (
            f"Expected single combined turn, got: {pipeline.model_turn_requests}"
        )

        saved = await _wait_for_saved_user_messages(pipeline, 1)
        assert [m["content"] for m in saved if m["role"] == "user"] == [expected_turn]
    finally:
        await pipeline.close()


