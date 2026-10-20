"""Tests for the language-routing processor.

The processor is the piece that decides *which voice* speaks each
sentence. Two things about it are easy to break and hard to notice:

* the voice delta must arrive **before** the sentence it belongs to —
  otherwise the first Hindi sentence is spoken by the English voice;
* the delta must only be pushed when the script actually changes — a
  delta per sentence would restart the TTS's audio context every time.
"""

from __future__ import annotations

import pytest
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from bot.language import Script
from bot.pipeline import LanguageRoutingProcessor


class RecordingRouter:
    """A router that records what it was asked for."""

    def __init__(self) -> None:
        self.requested: list[Script] = []

    def delta_for(self, script: Script):
        self.requested.append(script)
        return {"script": script}


class CapturingProcessor(LanguageRoutingProcessor):
    """Collects the frames the processor would have pushed."""

    def __init__(self, router) -> None:
        super().__init__(router)
        self.pushed: list[Frame] = []

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        self.pushed.append(frame)

    @property
    def texts(self) -> list[str]:
        return [f.text for f in self.pushed if isinstance(f, LLMTextFrame)]

    @property
    def deltas(self) -> list[object]:
        return [f.delta for f in self.pushed if isinstance(f, TTSUpdateSettingsFrame)]

    @property
    def aggregation_times(self) -> list[object]:
        """The text-aggregation reports the processor pushed."""
        return [
            data
            for f in self.pushed
            if isinstance(f, MetricsFrame)
            for data in f.data
        ]


@pytest.fixture
def router() -> RecordingRouter:
    return RecordingRouter()


@pytest.fixture
def processor(router: RecordingRouter) -> CapturingProcessor:
    return CapturingProcessor(router)


class TestSentenceSplitting:
    async def test_splits_the_reply_into_sentences(self, processor):
        await processor.process_frame(
            LLMTextFrame("Hello there. How are you?"), FrameDirection.DOWNSTREAM
        )
        assert processor.texts == ["Hello there.", "How are you?"]

    async def test_waits_for_a_sentence_boundary(self, processor):
        await processor.process_frame(LLMTextFrame("Hello"), FrameDirection.DOWNSTREAM)
        assert processor.texts == []
        await processor.process_frame(LLMTextFrame(" there."), FrameDirection.DOWNSTREAM)
        assert processor.texts == ["Hello there."]

    async def test_splits_on_the_devanagari_danda(self, processor):
        await processor.process_frame(
            LLMTextFrame("यह पहला वाक्य है। यह दूसरा है।"), FrameDirection.DOWNSTREAM
        )
        assert processor.texts == ["यह पहला वाक्य है।", "यह दूसरा है।"]


class TestVoiceRouting:
    async def test_the_delta_precedes_its_sentence(self, processor):
        await processor.process_frame(
            LLMTextFrame("नमस्ते, आप कैसे हैं?"), FrameDirection.DOWNSTREAM
        )
        names = [f.__class__.__name__ for f in processor.pushed]
        assert names[0] == "TTSUpdateSettingsFrame"
        assert names[1] == "LLMTextFrame"

    async def test_the_router_is_asked_for_the_right_script(self, processor, router):
        await processor.process_frame(
            LLMTextFrame("नमस्ते। Hello there."), FrameDirection.DOWNSTREAM
        )
        assert router.requested == ["devanagari", "latin"]

    async def test_the_delta_is_pushed_only_when_the_script_changes(self, processor, router):
        await processor.process_frame(
            LLMTextFrame("Hello there. How are you? Fine thanks."), FrameDirection.DOWNSTREAM
        )
        # Three sentences, one script, so one delta.
        assert len(processor.texts) == 3
        assert len(processor.deltas) == 1

    async def test_a_script_change_pushes_a_second_delta(self, processor):
        await processor.process_frame(
            LLMTextFrame("Hello. नमस्ते।"), FrameDirection.DOWNSTREAM
        )
        assert len(processor.deltas) == 2


class TestEndOfReply:
    async def test_the_remainder_is_spoken_at_the_end(self, processor):
        await processor.process_frame(LLMTextFrame("no terminator"), FrameDirection.DOWNSTREAM)
        assert processor.texts == []
        await processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
        assert processor.texts == ["no terminator"]

    async def test_the_end_frame_is_passed_on(self, processor):
        await processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
        assert any(isinstance(f, EndFrame) for f in processor.pushed)

    async def test_other_frames_are_passed_through(self, processor):
        from pipecat.frames.frames import TranscriptionFrame

        frame = TranscriptionFrame("hello", "user", "2026-01-01T00:00:00Z")
        await processor.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert processor.pushed == [frame]


class TestAggregationTime:
    """The wait for the first complete sentence, measured.

    The TTS cannot measure this: it runs in ``TextAggregationMode.TOKEN``
    because this processor does the splitting, and in that mode Pipecat
    skips the TTS's own text-aggregation metric. So the router reports
    it, in the same shape the TTS would have reported.
    """

    async def test_the_first_sentence_reports_the_wait(self, processor):
        await processor.process_frame(LLMTextFrame("Hello there."), FrameDirection.DOWNSTREAM)
        assert len(processor.aggregation_times) == 1
        assert processor.aggregation_times[0].value >= 0.0

    async def test_only_the_first_sentence_of_a_reply_is_reported(self, processor):
        await processor.process_frame(
            LLMTextFrame("One. Two. Three."), FrameDirection.DOWNSTREAM
        )
        assert len(processor.aggregation_times) == 1

    async def test_a_new_reply_is_measured_again(self, processor):
        await processor.process_frame(LLMTextFrame("First reply."), FrameDirection.DOWNSTREAM)
        await processor.process_frame(
            LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM
        )
        await processor.process_frame(
            LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM
        )
        await processor.process_frame(LLMTextFrame("Second reply."), FrameDirection.DOWNSTREAM)
        assert len(processor.aggregation_times) == 2

    async def test_the_report_carries_the_processor_name(self, processor):
        await processor.process_frame(LLMTextFrame("Hello there."), FrameDirection.DOWNSTREAM)
        report = processor.aggregation_times[0]
        assert report.processor == processor.name

    async def test_the_report_comes_after_the_sentence(self, processor):
        await processor.process_frame(LLMTextFrame("Hello there."), FrameDirection.DOWNSTREAM)
        names = [f.__class__.__name__ for f in processor.pushed]
        assert names.index("MetricsFrame") > names.index("LLMTextFrame")
