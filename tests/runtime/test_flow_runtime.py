from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Any

import pytest
from pipecat.frames.frames import (
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputAudioRawFrame,
    StartFrame,
    TextFrame,
    TranscriptionFrame,
    TTSTextFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.pipeline.pipeline import PipelineSink

from bot.config import Settings
from bot.flow import compile_flow
from bot.flow.schemas import LocaleChangedEvent, OutputCancelledOutputEvent
from bot.flow_runtime import (
    FlowAudioProcessor,
    FlowContextProcessor,
    FlowInputProcessor,
    FlowOutputProcessor,
    FlowRuntime,
)
from bot.observers import PrometheusObserver
from bot.pipeline import LanguageRoutingProcessor
from bot.runtime.pipeline_builder import build_voice_pipeline
from bot.transcripts import BotTranscriptForwarder, UserTranscriptForwarder
from tests.runtime.test_pipeline_builder import FakeTransport

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _simple_flow():
    return {
        "schema_version": 1,
        "entry_step_id": "intro",
        "exit_step_id": "end",
        "steps": [
            {
                "id": "intro",
                "title": "Introduction",
                "type": "inform",
                "instruction": "Introduce the service.",
                "messages": [{"id": "hello", "en": "Hello.", "hi": "नमस्ते।"}],
                "transitions": [{"when": {"operator": "completed"}, "to": "end"}],
            },
            {
                "id": "end",
                "title": "Finish",
                "type": "end",
                "instruction": "Close politely.",
                "required": False,
                "messages": [{"id": "goodbye", "en": "Goodbye.", "hi": "नमस्ते।"}],
                "transitions": [],
            },
        ],
    }


def _record_push(processor: FrameProcessor, frames: list[Any]) -> None:
    async def record(frame: Any, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        frames.append((frame, direction))

    processor.push_frame = record


class RecordingFlowEventStore:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def commit(self, event: Any, reduction: Any) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_input_processor_reduces_final_transcription_once():
    fixture = json.loads(
        (REPO_ROOT / "tests/fixtures/flow/valid-credit-card-en-hi.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = FlowRuntime(compile_flow(fixture["flow"]), "session-1", "epoch-1")
    processor = runtime.input_processor()
    forwarded: list[Any] = []
    _record_push(processor, forwarded)
    interim = TranscriptionFrame(
        text="I am salaried and earn INR 80000 monthly.",
        user_id="browser-user",
        timestamp="2026-10-30T00:00:00Z",
        finalized=False,
    )
    await processor.process_frame(interim, FrameDirection.DOWNSTREAM)
    assert runtime.state.last_accepted_turn_id is None
    frame = TranscriptionFrame(
        text="I am salaried and earn INR 80000 monthly.",
        user_id="browser-user",
        timestamp="2026-10-30T00:00:00Z",
        finalized=True,
    )
    await processor.process_frame(frame, FrameDirection.DOWNSTREAM)
    first_turn_id = runtime.state.last_accepted_turn_id

    assert first_turn_id == "turn:1"
    assert runtime.state.generation_id == "generation:1"
    assert len(runtime.state.turn_history) == 1
    assert runtime.state.fields["employment_type"].status == "confirmed"
    assert runtime.state.fields["employment_type"].value == "salaried"
    assert runtime.state.fields["monthly_income_inr"].status == "confirmed"
    assert runtime.state.fields["monthly_income_inr"].value == 80000
    assert len([item for item, _ in forwarded if item is frame]) == 1


@pytest.mark.asyncio
async def test_interruption_discards_late_model_speech():
    fixture = json.loads(
        (REPO_ROOT / "tests/fixtures/flow/valid-credit-card-en-hi.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = FlowRuntime(compile_flow(fixture["flow"]), "session-1", "epoch-1")
    input_processor = runtime.input_processor()
    _record_push(input_processor, [])

    async def ignore_uninitialized_pipeline_task_manager() -> None:
        return None

    input_processor._start_interruption = ignore_uninitialized_pipeline_task_manager
    await input_processor.process_frame(
        TranscriptionFrame(
            "I need a card.",
            "browser-user",
            "2026-10-30T00:00:00Z",
            finalized=True,
        ),
        FrameDirection.DOWNSTREAM,
    )
    await input_processor.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    output_processor = runtime.output_processors()[0]
    forwarded: list[Any] = []
    _record_push(output_processor, forwarded)
    await output_processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await output_processor.process_frame(
        LLMTextFrame("late speech from an interrupted generation"),
        FrameDirection.DOWNSTREAM,
    )

    assert runtime.state.generation_id == "generation:1"
    assert not any(isinstance(frame, LLMTextFrame) for frame, _ in forwarded)


@pytest.mark.asyncio
async def test_start_emits_authored_welcome_as_manifest_fragments():
    fixture = json.loads(
        (REPO_ROOT / "tests/fixtures/flow/valid-credit-card-en-hi.json").read_text(
            encoding="utf-8"
        )
    )
    runtime = FlowRuntime(compile_flow(fixture["flow"]), "session-1", "epoch-1")
    processor = runtime.output_processors()[0]
    processor._enable_direct_mode = True
    forwarded: list[Any] = []
    _record_push(processor, forwarded)

    await processor.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)

    welcome_frames = [
        frame
        for frame, _ in forwarded
        if isinstance(frame, TextFrame) and frame.metadata.get("flow_fragment")
    ]
    assert len(welcome_frames) == 1
    marker = welcome_frames[0].metadata["flow_fragment"]
    assert welcome_frames[0].text == fixture["flow"]["welcome"]["en"]
    assert marker["generation_id"] == "generation:0"
    assert marker["manifest_id"] == runtime.welcome_state["manifest_id"]
    assert marker["manifest_hash"] == runtime.welcome_state["manifest_hash"]
    assert marker["artifact_hash"] == runtime.compiled.artifact_hash
    assert runtime.welcome_state["status"] == "emitted"
    assert runtime.welcome_state["played_fragment_ids"] == []


@pytest.mark.asyncio
async def test_manifest_fragments_reach_tts_audio_with_hashes_but_never_claim_playback():
    compiled = compile_flow(_simple_flow())
    runtime = FlowRuntime(compiled, "session-1", "epoch-1")
    await runtime.accept_transcription("Please begin.")
    state = runtime.state
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

    output_processor = runtime.output_processors()[0]
    forwarded: list[Any] = []
    _record_push(output_processor, forwarded)
    await output_processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await output_processor.process_frame(
        LLMTextFrame("Model output must not replace the authored disclosure."),
        FrameDirection.DOWNSTREAM,
    )
    await output_processor.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    fragment_frames = [
        frame for frame, _ in forwarded
        if isinstance(frame, TextFrame) and frame.metadata.get("flow_fragment")
    ]
    assert len(fragment_frames) == 1
    marker = fragment_frames[0].metadata["flow_fragment"]
    assert fragment_frames[0].text == "नमस्ते।"
    assert marker["manifest_id"] == runtime.state.active_manifest_id
    assert marker["content_hash"] == hashlib.sha256(fragment_frames[0].text.encode()).hexdigest()
    assert marker["artifact_hash"] == compiled.artifact_hash
    assert marker["flow_hash"] == compiled.flow_hash
    assert not any(isinstance(frame, LLMTextFrame) for frame, _ in forwarded)

    audio_processor = runtime.audio_processor()
    _record_push(audio_processor, [])
    await audio_processor.process_frame(
        TTSTextFrame(text=fragment_frames[0].text, text_type="sentence"),
        FrameDirection.DOWNSTREAM,
    )
    audio_frame = OutputAudioRawFrame(audio=b"\x00\x00" * 240, sample_rate=24000, num_channels=1)
    await audio_processor.process_frame(audio_frame, FrameDirection.DOWNSTREAM)

    assert audio_frame.metadata["flow_fragment"] == marker
    assert runtime.state.delivered_fragment_ids == []
    assert runtime.state.steps["intro"] == "active"



@pytest.mark.asyncio
async def test_audio_correlation_does_not_guess_between_overlapping_tts_fragments():
    flow = _simple_flow()
    flow["steps"][0]["messages"].append(
        {"id": "welcome", "en": "Welcome.", "hi": "आपका स्वागत है।"}
    )
    runtime = FlowRuntime(compile_flow(flow), "session-1", "epoch-1")
    await runtime.accept_transcription("Please begin.")
    state = runtime.state
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
    output = runtime.output_processors()[0]
    output_frames: list[Any] = []
    _record_push(output, output_frames)
    await output.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    authored = [
        frame
        for frame, _ in output_frames
        if isinstance(frame, TextFrame) and frame.metadata.get("flow_fragment")
    ]
    assert len(authored) == 2

    audio = runtime.audio_processor()
    _record_push(audio, [])
    for frame in authored:
        await audio.process_frame(
            TTSTextFrame(text=frame.text, text_type="sentence"),
            FrameDirection.DOWNSTREAM,
        )
    audio_frame = OutputAudioRawFrame(
        audio=b"\x00\x00" * 240,
        sample_rate=24000,
        num_channels=1,
    )
    await audio.process_frame(audio_frame, FrameDirection.DOWNSTREAM)

    assert "flow_fragment" not in audio_frame.metadata
    assert runtime.state.delivered_fragment_ids == []

@pytest.mark.asyncio
async def test_interruption_records_unplayed_fragment_ids():
    store = RecordingFlowEventStore()
    runtime = FlowRuntime(
        compile_flow(_simple_flow()),
        "session-1",
        "epoch-1",
        event_store=store,
    )
    await runtime.accept_transcription("Please begin.")
    state = runtime.state
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
    processor = runtime.output_processors()[0]
    forwarded: list[Any] = []
    _record_push(processor, forwarded)
    await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    fragment = next(
        frame.metadata["flow_fragment"]
        for frame, _ in forwarded
        if isinstance(frame, TextFrame) and frame.metadata.get("flow_fragment")
    )

    await runtime.interrupt("generation:1")

    cancelled = [
        event
        for event in store.events
        if isinstance(event, OutputCancelledOutputEvent)
    ]
    assert len(cancelled) == 1
    assert cancelled[0].turn_id == "turn:1"
    assert cancelled[0].generation_id == "generation:1"
    assert cancelled[0].discarded_fragment_ids == [fragment["fragment_id"]]
    assert runtime.state.delivered_fragment_ids == []


@pytest.mark.asyncio
async def test_context_processor_injects_hash_bound_flow_data():
    compiled = compile_flow(_simple_flow())
    runtime = FlowRuntime(compiled, "session-1", "epoch-1")
    context = LLMContext(
        messages=[
            {"role": "system", "content": "Trusted platform policy."},
            {"role": "user", "content": "Please begin."},
        ]
    )
    processor = runtime.processors()[0]

    await processor._inject_flow_context(context)

    flow_messages = [
        message
        for message in context.messages
        if message.get("role") == "system"
        and message.get("content", "").startswith("VOICE_AI_FLOW_DATA_V1:")
    ]
    assert len(flow_messages) == 1
    assert compiled.artifact_hash in flow_messages[0]["content"]
    assert compiled.flow_hash in flow_messages[0]["content"]
    assert context.messages[0]["content"] == "Trusted platform policy."


@pytest.mark.asyncio
async def test_authorized_hindi_fragment_selects_voice_without_rechunking():
    class RecordingVoiceRouter:
        def delta_for(self, script: str) -> dict[str, str]:
            return {"script": script}

    processor = LanguageRoutingProcessor(RecordingVoiceRouter())
    forwarded: list[Any] = []
    _record_push(processor, forwarded)
    fragment_frame = TextFrame("नमस्ते।")
    fragment_frame.metadata["flow_fragment"] = {
        "manifest_id": "manifest-1",
        "fragment_id": "fragment-1",
    }

    await processor.process_frame(fragment_frame, FrameDirection.DOWNSTREAM)

    assert isinstance(forwarded[0][0], TTSUpdateSettingsFrame)
    assert forwarded[0][0].delta == {"script": "devanagari"}
    assert forwarded[-1][0] is fragment_frame



@pytest.mark.asyncio
async def test_interruption_discards_unfinished_routed_model_preview():
    class RecordingVoiceRouter:
        def delta_for(self, script: str) -> dict[str, str]:
            return {"script": script}

    processor = LanguageRoutingProcessor(RecordingVoiceRouter())
    forwarded: list[Any] = []
    _record_push(processor, forwarded)

    async def ignore_uninitialized_pipeline_task_manager() -> None:
        return None

    processor._start_interruption = ignore_uninitialized_pipeline_task_manager
    preview = LLMTextFrame("unfinished preview that must not be spoken")
    preview.metadata["flow_model_preview"] = True
    await processor.process_frame(preview, FrameDirection.DOWNSTREAM)
    await processor.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)

    assert not any(isinstance(frame, TextFrame) for frame, _ in forwarded)

@pytest.mark.asyncio
async def test_builder_places_flow_processors_on_real_pipeline_boundaries(monkeypatch):
    fake_stt = FrameProcessor(name="fake-stt")
    fake_llm = FrameProcessor(name="fake-llm")
    fake_tts = FrameProcessor(name="fake-tts")
    monkeypatch.setattr("bot.runtime.pipeline_builder.build_stt", lambda settings: fake_stt)
    monkeypatch.setattr("bot.runtime.pipeline_builder.build_llm", lambda settings: fake_llm)
    monkeypatch.setattr("bot.runtime.pipeline_builder.build_tts", lambda settings: fake_tts)
    compiled = compile_flow(_simple_flow())
    runtime = FlowRuntime(compiled, "session-1", "epoch-1")
    assembly = build_voice_pipeline(
        FakeTransport(),
        Settings(_env_file=None, enable_tracing=False),
        flow_hooks=runtime,
        instruments=PrometheusObserver(),
    )
    processors = list(assembly.processors)

    def index(predicate):
        return next(i for i, processor in enumerate(processors) if predicate(processor))

    user_aggregator = index(lambda p: p.__class__.__name__ == "LLMUserAggregator")
    context_shaper = index(lambda p: p.__class__.__name__ == "ContextShapingProcessor")
    llm = index(lambda p: p is fake_llm)
    router = index(lambda p: isinstance(p, LanguageRoutingProcessor))
    tts = index(lambda p: p is fake_tts)
    transport_output = index(lambda p: p.name == "fake-output")

    assert index(lambda p: isinstance(p, FlowInputProcessor)) < user_aggregator
    assert context_shaper < index(lambda p: isinstance(p, FlowContextProcessor)) < llm
    assert llm < index(lambda p: isinstance(p, FlowOutputProcessor)) < router
    assert tts < index(lambda p: isinstance(p, FlowAudioProcessor)) < transport_output
    assert isinstance(processors[-1], PipelineSink)
