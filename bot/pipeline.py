"""The language-routing processor.

This sits between the LLM and the TTS. It does three things:

1. **Splits the LLM's reply into sentences.** The LLM streams tokens; the
   TTS needs whole sentences, because a sentence is the smallest unit that
   can be given a voice and spoken at a sensible pace. The TTS service is
   configured with ``TextAggregationMode.TOKEN``, so each text frame this
   processor pushes is synthesized as-is — one sentence, one utterance.

2. **Chooses the voice per sentence.** The script of the reply can change
   mid-answer ("Sure. अब मैं आपकी मदद करता हूँ।"), so the voice is decided
   per sentence, not per turn.

3. **Measures the cost of waiting for a sentence.** The first sentence of
   a reply is not spoken until it is complete, and that wait is the price
   of this design. The TTS cannot measure it: in ``TOKEN`` mode Pipecat
   deliberately skips the TTS's own text-aggregation metric, because
   *this* processor is where the aggregation happens. So the measurement
   is taken here, and reported as the same
   ``TextAggregationMetricsData`` the TTS would have reported.

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
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TTSUpdateSettingsFrame,
)
from pipecat.metrics.metrics import TextAggregationMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from bot.language import SentenceChunker, Script, VoiceRouter, detect_script


class LanguageRoutingProcessor(FrameProcessor):
    """Splits LLM text into sentences and routes each one to the right voice."""

    def __init__(self, voice_router: VoiceRouter) -> None:
        super().__init__()
        self._router = voice_router
        self._chunker = SentenceChunker()
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

    @property
    def current_script(self) -> Script | None:
        """The script of the sentence currently being spoken."""
        return self._current_script

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._start_response()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMTextFrame):
            if self._first_token_at is None and not self._aggregation_reported:
                self._first_token_at = time.monotonic()
            for sentence in self._chunker.feed(frame.text):
                await self._speak(sentence)
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

        if script != self._current_script:
            delta = self._router.delta_for(script)
            # A control frame: processed in order, never dropped by an
            # interruption, so the voice is always set before the text it
            # belongs to reaches the TTS.
            await self.push_frame(TTSUpdateSettingsFrame(delta=delta))
            self._current_script = script

        await self.push_frame(LLMTextFrame(sentence))

        # After the sentence, not before: the delta-then-text order is a
        # hard constraint — the voice must be set before the text it
        # belongs to reaches the TTS — and a metrics frame must not come
        # between them.
        await self._report_aggregation_time()

    # -- aggregation time ---------------------------------------------------

    def _start_response(self) -> None:
        """A new reply is starting: the next first token starts the clock."""
        self._first_token_at = None
        self._aggregation_reported = False

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
