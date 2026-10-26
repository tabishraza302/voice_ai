"""Forward completed user and spoken assistant text to the local UI."""

from pipecat.frames.frames import (
    Frame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSTextFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.time import time_now_iso8601
from loguru import logger


class UserTranscriptForwarder(FrameProcessor):
    def __init__(self, transport):
        super().__init__()
        self._transport = transport

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, TranscriptionFrame) and frame.text:
            try:
                await self._transport.output().send_message(
                    OutputTransportMessageUrgentFrame(
                        message={"type": "transcript", "role": "user", "text": frame.text}
                    )
                )
            except Exception as exc:
                logger.debug(f"User transcript send error: {exc}")
        await self.push_frame(frame, direction)


class BotTranscriptForwarder(FrameProcessor):
    """Forward TTS output text, so generated-but-skipped text is not labeled spoken."""

    def __init__(self, transport):
        super().__init__()
        self._transport = transport

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, TTSTextFrame) and frame.text:
            try:
                await self._transport.output().send_message(
                    OutputTransportMessageUrgentFrame(
                        message={"type": "transcript", "role": "assistant", "text": frame.text}
                    )
                )
            except Exception as exc:
                logger.debug(f"Bot transcript send error: {exc}")
        await self.push_frame(frame, direction)


MAX_CLIENT_UTTERANCE_CHARS = 4000


def client_utterance(payload: dict) -> str | None:
    """Return bounded, nonblank client text suitable for a transcription frame."""
    text = payload.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > MAX_CLIENT_UTTERANCE_CHARS:
        return None
    return text


async def queue_client_utterance(payload: dict, pipeline) -> bool:
    """Queue a valid browser text turn, without contacting a model directly."""
    text = client_utterance(payload)
    if text is None:
        return False
    await pipeline.queue_frame(UserStartedSpeakingFrame())
    await pipeline.queue_frame(TranscriptionFrame(text, "client", time_now_iso8601()))
    await pipeline.queue_frame(UserStoppedSpeakingFrame())
    return True
