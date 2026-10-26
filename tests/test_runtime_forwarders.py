from unittest.mock import AsyncMock

import pytest

from pipecat.frames.frames import (
    TextFrame,
    TranscriptionFrame,
    TTSTextFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from bot.transcripts import (
    BotTranscriptForwarder,
    UserTranscriptForwarder,
    client_utterance,
    queue_client_utterance,
)


class FakeTransport:
    def __init__(self):
        self.send_message = AsyncMock()

    def output(self):
        return self


@pytest.mark.asyncio
async def test_user_forwarder_sends_only_final_transcripts_downstream():
    transport = FakeTransport()
    forwarder = UserTranscriptForwarder(transport)
    forwarder.push_frame = AsyncMock()
    frame = TranscriptionFrame("Hello", "user", "2026-01-01T00:00:00Z")

    await forwarder.process_frame(frame, FrameDirection.DOWNSTREAM)

    sent = transport.send_message.await_args.args[0]
    assert isinstance(sent, OutputTransportMessageUrgentFrame)
    assert sent.message == {"type": "transcript", "role": "user", "text": "Hello"}
    forwarder.push_frame.assert_awaited_once_with(frame, FrameDirection.DOWNSTREAM)


@pytest.mark.asyncio
async def test_assistant_forwarder_uses_tts_text_not_generated_text():
    transport = FakeTransport()
    forwarder = BotTranscriptForwarder(transport)
    forwarder.push_frame = AsyncMock()
    generated = TextFrame("Generated but not necessarily spoken")

    await forwarder.process_frame(generated, FrameDirection.DOWNSTREAM)
    transport.send_message.assert_not_awaited()

    spoken = TTSTextFrame("Spoken sentence", "sentence")
    await forwarder.process_frame(spoken, FrameDirection.DOWNSTREAM)

    sent = transport.send_message.await_args.args[0]
    assert sent.message == {
        "type": "transcript",
        "role": "assistant",
        "text": "Spoken sentence",
    }


@pytest.mark.asyncio
async def test_transcript_forwarders_do_not_emit_for_upstream_frames():
    transport = FakeTransport()
    forwarder = UserTranscriptForwarder(transport)
    forwarder.push_frame = AsyncMock()

    await forwarder.process_frame(
        TranscriptionFrame("Hello", "user", "2026-01-01T00:00:00Z"),
        FrameDirection.UPSTREAM,
    )

    transport.send_message.assert_not_awaited()


@pytest.mark.parametrize("value", [None, 1, ["hello"], {"text": "hello"}, "  "])
def test_client_utterance_rejects_non_text_and_blank_payloads(value):
    assert client_utterance({"text": value}) is None


def test_client_utterance_bounds_text_length():
    assert client_utterance({"text": "x" * 4001}) is None
    assert client_utterance({"text": "  hello  "}) == "hello"


@pytest.mark.asyncio
async def test_client_text_handler_queues_only_bounded_text_frames():
    class FakePipeline:
        def __init__(self):
            self.frames = []

        async def queue_frame(self, frame):
            self.frames.append(frame)

    pipeline = FakePipeline()
    assert await queue_client_utterance({"text": ["bad"]}, pipeline) is False
    assert pipeline.frames == []

    assert await queue_client_utterance({"text": "Hi"}, pipeline) is True
    assert [type(frame).__name__ for frame in pipeline.frames] == [
        "UserStartedSpeakingFrame",
        "TranscriptionFrame",
        "UserStoppedSpeakingFrame",
    ]
    assert pipeline.frames[1].text == "Hi"
