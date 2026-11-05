
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import (
    Frame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
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


@pytest.fixture
def anyio_backend():
    return "asyncio"


def verified_environment() -> dict[str, str]:
    return {
        "DEEPGRAM_API_KEY": "deepgram-test-secret",
        "OPENAI_API_KEY": "openai-test-secret",
        "VERIFIED_OPENAI_MODELS": "gpt-4o-mini,gpt-4.1-mini",
        "CARTESIA_API_KEY": "cartesia-test-secret",
        "ELEVENLABS_API_KEY": "elevenlabs-test-secret",
        "VERIFIED_TTS_VOICE_CATALOG": json.dumps(
            {
                "cartesia": {"sonic-3.6": {"cartesia-en": ["en"], "cartesia-hi": ["hi"]}},
                "elevenlabs": {
                    "eleven_flash_v2_5": {"eleven-en": ["en"], "eleven-hi": ["hi"]}
                },
            }
        ),
    }


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


def test_publishing_accepts_only_configured_provider_models_and_voice() -> None:
    assert provider_publish_errors(provider_agent(), verified_environment()) == {}


def test_publishing_rejects_arbitrary_model_and_voice_ids() -> None:
    env = verified_environment()
    agent = provider_agent(llm_model="model-from-user", voice_id="voice-from-user")

    errors = provider_publish_errors(agent, env)

    assert "llm_model" in errors
    assert "voice_id" in errors


def test_publishing_fails_closed_without_provider_credentials() -> None:
    env = verified_environment()
    env.pop("OPENAI_API_KEY")
    env.pop("CARTESIA_API_KEY")

    errors = provider_publish_errors(provider_agent(), env)

    assert "llm_provider" in errors
    assert "voice_id" in errors


def test_publishing_fails_closed_without_catalog_when_credentials_are_present() -> None:
    env = verified_environment()
    env.pop("VERIFIED_TTS_VOICE_CATALOG")

    errors = provider_publish_errors(provider_agent(), env)

    assert "voice_id" in errors


def test_multilingual_runtime_is_not_published_until_tts_switching_is_verified() -> None:
    errors = provider_publish_errors(provider_agent(languages=["en", "hi"]), verified_environment())

    assert "languages" in errors


def test_voice_catalog_is_scoped_to_provider_model_and_language() -> None:
    env = verified_environment()

    assert validate_tts_voice_selection(
        provider="cartesia",
        model="sonic-3.6",
        voice_id="cartesia-hi",
        language="hi",
        env=env,
    ) is None
    assert validate_tts_voice_selection(
        provider="cartesia",
        model="sonic-3.6",
        voice_id="cartesia-hi",
        language="en",
        env=env,
    ) is not None
    assert validate_tts_voice_selection(
        provider="elevenlabs",
        model="sonic-3.6",
        voice_id="cartesia-en",
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
        "steps": [{"id": "step-1", "nested": {"title": "Question"}}],
    }
    env = verified_environment()

    definition = CallDefinition.from_snapshot(uuid.uuid4(), "test", snapshot, env)

    snapshot["global_prompt"]["text"] = "Changed prompt"
    snapshot["agent"]["instructions"] = "Changed instructions"
    snapshot["steps"][0]["nested"]["title"] = "Changed step"
    env["OPENAI_API_KEY"] = "rotated-key"

    assert definition.global_prompt == "Global call policy"
    assert definition.prompt_version == 7
    assert definition.agent_instructions == "Help the caller."
    assert definition.steps[0]["nested"]["title"] == "Question"
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
        "steps": [],
    }
    env = verified_environment()
    env.pop("DEEPGRAM_API_KEY")

    with pytest.raises(VoiceUnavailableError):
        CallDefinition.from_snapshot(uuid.uuid4(), "test", snapshot, env)


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
async def test_tts_without_matching_output_audio_fails_closed() -> None:
    runtime = object.__new__(CallRuntime)
    runtime.playbacks = {
        "context": _Playback(
            context_id="context",
            kind="llm",
            generation=1,
        )
    }
    failures: list[str] = []
    runtime.request_stop = failures.append

    await runtime.on_tts_stopped(
        TTSStoppedFrame(context_id="context"),
        transport_completed=False,
    )

    assert failures == ["failed"]


@pytest.mark.anyio
async def test_interrupted_exact_opening_stays_interrupted_after_transport_drain() -> None:
    runtime = object.__new__(CallRuntime)
    runtime.playbacks = {
        "opening": _Playback(
            context_id="opening",
            kind="exact",
            generation=1,
            step_id="step-1",
            interrupted=True,
        )
    }
    runtime.pending_exact_step = "step-1"
    runtime.exact_generation = 1
    runtime.workflow_state = SimpleNamespace(opening_states={"step-1": "interrupted"})

    await runtime.on_tts_stopped(
        TTSStoppedFrame(context_id="opening"),
        transport_completed=True,
    )

    assert runtime.workflow_state.opening_states["step-1"] == "interrupted"
    assert runtime.pending_exact_step == "step-1"
    assert runtime.playbacks == {}

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
    runtime.termination_outcome = None

    runtime.request_stop("completed", "private-outcome")

    assert messages == [{"type": "call.completed"}]
    await runtime.cleanup_task
