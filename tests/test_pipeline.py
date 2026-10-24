"""Tests for the language-routing processor.

The processor is the piece that decides *which voice* speaks each
sentence. Two things about it are easy to break and hard to notice:

* the voice delta must arrive **before** the sentence it belongs to —
  otherwise the first Hindi sentence is spoken by the English voice;
* the delta must only be pushed when the script actually changes — a
  delta per sentence would restart the TTS's audio context every time.
"""

from __future__ import annotations

import asyncio

import pytest
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TextFrame,
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

    def __init__(self, router, deadline_seconds: float = 30.0) -> None:
        super().__init__(router, deadline_seconds=deadline_seconds)
        self.pushed: list[Frame] = []

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        self.pushed.append(frame)

    @property
    def texts(self) -> list[str]:
        return [f.text for f in self.pushed if isinstance(f, TextFrame)]

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
        assert names[1] == "TextFrame"

    async def test_the_spoken_sentences_are_not_llm_text_frames(self, processor):
        """The sentences must not be reported as the model's own text.

        The LLM's frames are on the wire as the model streams them; a
        second ``LLMTextFrame`` per sentence would put the reply in the
        client's transcript twice.
        """
        await processor.process_frame(
            LLMTextFrame("Hello there. How are you?"), FrameDirection.DOWNSTREAM
        )
        assert processor.texts == ["Hello there.", "How are you?"]
        assert not any(isinstance(f, LLMTextFrame) for f in processor.pushed)

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


class TestFirstChunkDeadline:
    """The bot speaks a clause rather than waiting for a full stop.

    The bot is silent until the model finishes a sentence, and that wait
    is the largest single part of the time to the first sound. The
    deadline ends it: when the first sentence has taken too long, the
    reply is cut at its last clause boundary and spoken.
    """

    # A deadline this short has passed by the time the second token
    # arrives, which is what a slow model looks like.
    DEADLINE = 0.01

    async def test_a_slow_first_sentence_is_cut_at_a_clause(self, router):
        processor = CapturingProcessor(router, deadline_seconds=self.DEADLINE)
        await processor.process_frame(
            LLMTextFrame("Certainly, I have checked"), FrameDirection.DOWNSTREAM
        )
        await asyncio.sleep(self.DEADLINE * 2)
        await processor.process_frame(
            LLMTextFrame(" the system"), FrameDirection.DOWNSTREAM
        )
        assert processor.texts == ["Certainly,"]

    async def test_the_tail_of_a_cut_sentence_is_spoken_next(self, router):
        processor = CapturingProcessor(router, deadline_seconds=self.DEADLINE)
        await processor.process_frame(
            LLMTextFrame("Certainly, I have checked the system"), FrameDirection.DOWNSTREAM
        )
        await asyncio.sleep(self.DEADLINE * 2)
        await processor.process_frame(
            LLMTextFrame(" and everything is fine"), FrameDirection.DOWNSTREAM
        )
        await processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
        assert processor.texts == [
            "Certainly,",
            "I have checked the system and everything is fine",
        ]

    async def test_a_sentence_with_no_clause_is_still_whole(self, router):
        # A reply with no clause boundary is spoken as it arrived, not
        # held back by the deadline.
        processor = CapturingProcessor(router, deadline_seconds=self.DEADLINE)
        await processor.process_frame(
            LLMTextFrame("Everything is fine"), FrameDirection.DOWNSTREAM
        )
        await asyncio.sleep(self.DEADLINE * 2)
        await processor.process_frame(
            LLMTextFrame(" indeed"), FrameDirection.DOWNSTREAM
        )
        assert processor.texts == ["Everything is fine indeed"]

    async def test_a_fast_first_sentence_is_not_cut(self, router):
        # The deadline is a deadline, not a rule: a sentence that
        # completes in time is spoken as a sentence.
        processor = CapturingProcessor(router, deadline_seconds=30.0)
        await processor.process_frame(
            LLMTextFrame("Certainly, I have checked the system."), FrameDirection.DOWNSTREAM
        )
        assert processor.texts == ["Certainly, I have checked the system."]

    async def test_the_deadline_only_applies_to_the_first_chunk(self, router):
        processor = CapturingProcessor(router, deadline_seconds=self.DEADLINE)
        await processor.process_frame(
            LLMTextFrame("First sentence."), FrameDirection.DOWNSTREAM
        )
        await asyncio.sleep(self.DEADLINE * 2)
        await processor.process_frame(
            LLMTextFrame("Second one, with a clause"), FrameDirection.DOWNSTREAM
        )
        assert processor.texts == ["First sentence."]

    async def test_a_deadline_of_zero_turns_the_cut_off(self, router):
        # Zero is the behaviour before the deadline existed: the bot
        # waits for the whole sentence, and the clause is not cut off.
        processor = CapturingProcessor(router, deadline_seconds=0.0)
        await processor.process_frame(
            LLMTextFrame("Certainly, I have checked the system"), FrameDirection.DOWNSTREAM
        )
        await asyncio.sleep(self.DEADLINE * 2)
        await processor.process_frame(
            LLMTextFrame(" and everything"), FrameDirection.DOWNSTREAM
        )
        # A second token arrives after the deadline, and it is not a
        # sentence end: with the cut enabled this would speak "Certainly,".
        assert processor.texts == []
        await processor.process_frame(EndFrame(), FrameDirection.DOWNSTREAM)
        assert processor.texts == ["Certainly, I have checked the system and everything"]


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
        assert names.index("MetricsFrame") > names.index("TextFrame")
