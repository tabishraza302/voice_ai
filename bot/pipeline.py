"""The language-routing processor.

This sits between the LLM and the TTS. It does four things:

1. **Splits the LLM's reply into sentences.** The LLM streams tokens; the
   TTS needs whole sentences, because a sentence is the smallest unit that
   can be given a voice and spoken at a sensible pace. The TTS service is
   configured with ``TextAggregationMode.TOKEN``, so each text frame this
   processor pushes is synthesized as-is — one sentence, one utterance.

2. **Chooses the voice per sentence.** The script of the reply can change
   mid-answer ("Sure. अब मैं आपकी मदद करता हूँ।"), so the voice is decided
   per sentence, not per turn.

3. **Cuts the first sentence short when the model is slow.** The bot is
   silent until the model finishes its first sentence, and that wait is
   the largest single part of the time to the first sound. So the first
   chunk is released at the last clause boundary once
   ``deadline_seconds`` have passed, and the rest of the sentence follows.

4. **Measures the cost of waiting for a sentence.** The TTS cannot measure
   it: in ``TOKEN`` mode Pipecat deliberately skips the TTS's own
   text-aggregation metric, because *this* processor is where the
   aggregation happens. So the measurement is taken here, and reported as
   the same ``TextAggregationMetricsData`` the TTS would have reported.

The sentences it pushes are plain ``TextFrame``s, not ``LLMTextFrame``s:
the LLM's own frames are already on the wire as the model streams them,
and Pipecat's RTVI observer reports a frame when it is first pushed, so a
second ``LLMTextFrame`` per sentence would report the reply twice — in the
eval harness's transcript as well as in a client's.

**It names no TTS backend.** The settings delta comes from the injected
``VoiceRouter``, which is the whole point: swapping the TTS provider
changes the router implementation and nothing else. The test suite
asserts this module does not import a concrete TTS class.
"""

from __future__ import annotations

import time

from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TextFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.metrics.metrics import TextAggregationMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from bot.language import SentenceChunker, Script, VoiceRouter, detect_script


class LanguageRoutingProcessor(FrameProcessor):
    """Splits LLM text into sentences and routes each one to the right voice."""

    def __init__(self, voice_router: VoiceRouter, deadline_seconds: float = 0.25) -> None:
        super().__init__()
        self._router = voice_router
        self._chunker = SentenceChunker()
        # How long the first sentence of a reply may take before the bot
        # speaks the first clause instead. The bot is silent until the
        # model finishes a sentence, and that wait is the largest single
        # part of the time to the first sound.
        self._deadline_seconds = deadline_seconds
        # The voice currently in force, so a delta is only pushed when the
        # script actually changes. Pushing an unchanged delta on every
        # sentence would restart the TTS's audio context each time.
        self._current_script: Script | None = None
        # When the current reply's first token arrived, and whether its
        # aggregation time has already been reported. Only the first
        # sentence of a reply is measured: that is the one the user waits
        # for, and the later ones cost nothing.
        self._first_token_at: float | None = None
        self._aggregation_reported = False
        # Whether this reply's first chunk has been spoken yet. Until it
        # has, the deadline can cut the reply short.
        self._first_chunk_spoken = False
        self._flow_model_preview = False

    @property
    def current_script(self) -> Script | None:
        """The script of the sentence currently being spoken."""
        return self._current_script

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InterruptionFrame):
            self._chunker = SentenceChunker()
            self._start_response()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._start_response()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TextFrame) and direction == FrameDirection.DOWNSTREAM:
            marker = frame.metadata.get("flow_fragment")
            if (
                isinstance(marker, dict)
                and marker.get("manifest_id")
                and marker.get("fragment_id")
            ):
                await self._set_script(detect_script(frame.text), direction)
                await self.push_frame(frame, direction)
                self._first_chunk_spoken = True
                await self._report_aggregation_time()
                return
        if isinstance(frame, LLMTextFrame):
            if frame.metadata.get("flow_model_preview"):
                self._flow_model_preview = True
            if self._first_token_at is None and not self._aggregation_reported:
                self._first_token_at = time.monotonic()
            chunks = self._chunker.feed(frame.text)
            if not chunks and self._first_chunk_pending() and self._deadline_passed():
                chunks = self._chunker.force_cut()
            for chunk in chunks:
                await self._speak(chunk)
            return

        if isinstance(frame, (LLMFullResponseEndFrame, EndFrame)):
            # The reply ended mid-sentence: speak what is left rather than
            # dropping it.
            for sentence in self._chunker.flush():
                await self._speak(sentence)
            self._end_response()
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)

    async def _speak(self, sentence: str) -> None:
        """Route one sentence: set the voice if it changed, then send it."""
        script = detect_script(sentence)
        await self._set_script(script)

        # Not an ``LLMTextFrame``, deliberately. Pipecat's RTVI observer
        # reports every LLM text frame the moment it is pushed — and the
        # LLM's own frames are pushed before this processor can consume
        # them — so re-emitting an ``LLMTextFrame`` here would put the
        # reply on the wire twice: once as the model streamed it, once as
        # the sentences that are actually spoken. The spoken sentences are
        # plain text; the model's frames keep their meaning as the model's
        # text.
        speech_frame = TextFrame(sentence)
        if self._flow_model_preview:
            speech_frame.append_to_context = False
            speech_frame.metadata["flow_model_preview"] = True
        await self.push_frame(speech_frame)
        self._first_chunk_spoken = True

        # After the sentence, not before: the delta-then-text order is a
        # hard constraint — the voice must be set before the text it
        # belongs to reaches the TTS — and a metrics frame must not come
        # between them.
        await self._report_aggregation_time()

    async def _set_script(
        self,
        script: Script,
        direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        """Set the TTS voice before speaking text in a different script."""
        if script == self._current_script:
            return
        delta = self._router.delta_for(script)
        await self.push_frame(TTSUpdateSettingsFrame(delta=delta), direction)
        self._current_script = script

    # -- aggregation time ---------------------------------------------------

    def _start_response(self) -> None:
        """A new reply is starting: the next first token starts the clock."""
        self._first_token_at = None
        self._aggregation_reported = False
        self._first_chunk_spoken = False
        self._flow_model_preview = False

    def _first_chunk_pending(self) -> bool:
        """Whether this reply's first chunk has not been spoken yet."""
        return self._first_token_at is not None and not self._first_chunk_spoken

    def _deadline_passed(self) -> bool:
        """Whether the first sentence has taken too long.

        The deadline is checked when text arrives rather than on a
        timer: there is nothing to cut until there is text, and the
        model's own pace is what the deadline is about.

        A deadline of zero turns the cut off, which is the behaviour
        before the deadline existed: the bot waits for the whole
        sentence.
        """
        if self._first_token_at is None or self._deadline_seconds <= 0:
            return False
        return time.monotonic() - self._first_token_at >= self._deadline_seconds

    def _end_response(self) -> None:
        """The reply is over: the next one gets a fresh measurement."""
        self._first_token_at = None
        self._aggregation_reported = False

    async def _report_aggregation_time(self) -> None:
        """Report how long the reply's first sentence took to complete.

        This is the wait the user experiences between the LLM starting
        and the bot starting to speak, caused by not speaking a partial
        sentence. It is reported as ``TextAggregationMetricsData`` — the
        same shape the TTS would have reported — so the Prometheus
        observer records it without knowing where it came from.

        The model is left unset: the observer resolves it from the
        processor name, and this processor's tokens come from the LLM.
        """
        if self._first_token_at is None or self._aggregation_reported:
            return

        elapsed = time.monotonic() - self._first_token_at
        self._aggregation_reported = True

        await self.push_frame(
            MetricsFrame(
                data=[
                    TextAggregationMetricsData(
                        processor=self.name,
                        value=elapsed,
                        model=None,
                    )
                ]
            )
        )
