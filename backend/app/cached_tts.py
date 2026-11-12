from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import json
import logging
import uuid
from typing import Any

from pipecat.frames.frames import (
    AggregatedTextFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterruptionFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.elevenlabs.tts import (
    ElevenLabsTTSService,
    _select_alignment,
    _strip_utterance_leading_spaces,
    _word_timestamps_include_inter_frame_spaces,
    calculate_word_times,
)
from pipecat.services.tts_service import WordTimestamp
from pipecat.utils.types import assert_given

from .tts_cache import (
    MAX_WORD_CHARS,
    MAX_WORD_EVENTS,
    MAX_WORD_METADATA_BYTES,
    CachedAudioEntry,
    TTSAudioCache,
    max_audio_bytes,
)

logger = logging.getLogger(__name__)

# A replayed clip is handed to the transport in chunks of about this length.
REPLAY_CHUNK_SECONDS = 0.05
# How long a shutdown waits for the cache writes that are still in flight.
CACHE_WRITE_DRAIN_SECONDS = 2.0


@dataclass
class AudioCaptureSession:
    """Bounded recording of one spoken segment.

    The recording is dropped as soon as it grows past the ceiling for its
    format, or past the ceiling for its word events, so a long or endless
    segment never holds on to memory. The live synthesis is never held up by
    the cache.
    """

    cache_key: str
    sample_rate: int = 24000
    num_channels: int = 1
    encoding: str = "pcm_s16le"
    audio_chunks: list[bytes] = field(default_factory=list)
    word_times: list[tuple[str, float, bool]] = field(default_factory=list)
    total_audio_bytes: int = 0
    word_metadata_bytes: int = 0
    aborted: bool = False
    interrupted: bool = False
    error: bool = False

    @property
    def max_bytes(self) -> int:
        return max_audio_bytes(self.sample_rate, self.num_channels)

    def append_audio(self, chunk: bytes) -> None:
        if self.aborted or self.interrupted or self.error:
            return
        self.total_audio_bytes += len(chunk)
        if self.total_audio_bytes > self.max_bytes:
            # Past the ceiling the recording is dropped, but the provider keeps
            # streaming: the call is not held up by the cache.
            self._drop()
            return
        self.audio_chunks.append(chunk)

    def append_words(self, words: list[tuple[str, float, bool]]) -> None:
        """Take the word events of the segment, within their ceilings."""
        if self.aborted or self.interrupted or self.error:
            return
        for word, timestamp, spacing in words:
            if len(word) > MAX_WORD_CHARS:
                # A word this long is not a word; the recording cannot be
                # trusted any more.
                self._drop()
                return
            size = len(word.encode("utf-8")) + 32
            if (
                len(self.word_times) >= MAX_WORD_EVENTS
                or self.word_metadata_bytes + size > MAX_WORD_METADATA_BYTES
            ):
                self._drop()
                return
            self.word_times.append((word, timestamp, spacing))
            self.word_metadata_bytes += size

    def _drop(self) -> None:
        self.aborted = True
        self.audio_chunks.clear()
        self.word_times.clear()

    def to_entry(self) -> CachedAudioEntry | None:
        if self.aborted or self.interrupted or self.error or not self.audio_chunks:
            return None
        full_audio = b"".join(self.audio_chunks)
        duration = len(full_audio) / (self.sample_rate * 2 * self.num_channels)
        return CachedAudioEntry(
            audio=full_audio,
            sample_rate=self.sample_rate,
            num_channels=self.num_channels,
            encoding=self.encoding,
            duration_seconds=duration,
            word_times=list(self.word_times),
        )


class _CachedTTSAdapter:
    """Shared capture and replay behaviour for the cached TTS services.

    While caching is active every spoken segment gets its own provider
    context, because the provider tags the audio and the alignment it sends
    back with the context the text was sent under. A capture is therefore
    only ever a single complete speech segment, and a clip can never contain
    text that was meant to stay uncached.

    While caching is inactive the service behaves exactly like the upstream
    provider: one context per turn, closed at the end of the turn.
    """

    def _init_cache(self, cache: TTSAudioCache | None, agent_id: str | Any | None) -> None:
        self._tts_cache = cache
        self._agent_id = str(agent_id) if agent_id else None
        self._active_captures: dict[str, AudioCaptureSession] = {}
        # Provider contexts this service opened and still has to close.
        self._provider_contexts: set[str] = set()
        # Cache writes that have not finished yet.
        self._pending_writes: set[asyncio.Task[None]] = set()
        # Set when the service is done: no late callback may enqueue a write.
        self._writes_closed = False

    @property
    def _cache_active(self) -> bool:
        if self._tts_cache is None or not self._tts_cache.enabled or not self._agent_id:
            return False
        # Only a format the cache can hand to the transport is cached at all:
        # a clip in another encoding cannot be replayed as it is.
        return self._supported_encoding() is not None

    #
    # Contexts
    #

    async def _handle_aggregated_text(
        self, src_frame: AggregatedTextFrame, context_id: str | None = None
    ) -> None:
        """Give every spoken segment a provider context of its own.

        Upstream gives the sentences of a turn the turn's context, which the
        provider keeps open until the turn ends. A cached clip has to be one
        complete segment, so the context of a segment is chosen here, where
        the segment is chosen, and it is a context of its own: the provider
        echoes it with the audio, so the capture of a segment holds that
        segment's audio only.

        A context that the caller named is left alone. That is the utterance
        of a ``TTSSpeakFrame``, which brings its own context and its own
        interruptible flag.
        """
        if self._cache_active and context_id is None:
            context_id = str(uuid.uuid4())
        await super()._handle_aggregated_text(src_frame, context_id)

    async def on_turn_context_completed(self) -> None:
        if not self._cache_active:
            await super().on_turn_context_completed()
            return
        # Every provider context is closed at the end of its own segment, so
        # the base must not close the turn context again. For a forced
        # utterance the turn context *is* a segment context that is already
        # closed, and for an LLM turn it is a context the provider never
        # opened: closing it would be traffic to the provider for nothing.
        saved_turn_context_id = self._turn_context_id
        self._turn_context_id = None
        try:
            await super().on_turn_context_completed()
        finally:
            self._turn_context_id = saved_turn_context_id

    async def run_tts(self, text: str, context_id: str):
        """Note the provider contexts the provider actually took the text for.

        The upstream method yields ``None`` once the text has been sent, so
        that is the proof that the provider opened the context and has to be
        told that the synthesis is over.
        """
        async for frame in super().run_tts(text, context_id):
            if frame is None:
                self._provider_contexts.add(context_id)
            yield frame

    #
    # Capture
    #

    async def append_to_audio_context(
        self, context_id: str | None, frame: Frame | WordTimestamp | None
    ) -> None:
        if self._cache_active and context_id and context_id in self._active_captures:
            capture = self._active_captures[context_id]
            if isinstance(frame, TTSAudioRawFrame):
                capture.append_audio(frame.audio)
            elif isinstance(frame, WordTimestamp):
                # The provider's own word event, already normalized by the
                # base class: it carries the word, its timing and whether the
                # word carries its own spacing.
                capture.append_words(
                    [(frame.word, frame.timestamp, frame.includes_inter_frame_spaces)]
                )
            elif isinstance(frame, ErrorFrame):
                capture.error = True
            elif isinstance(frame, TTSStoppedFrame):
                # The provider's own end-of-synthesis marker. Only now is the
                # segment known to be a complete synthesis.
                await self._finalize_capture(context_id)
        await super().append_to_audio_context(context_id, frame)

    async def on_audio_context_completed(self, context_id: str) -> None:
        await super().on_audio_context_completed(context_id)
        if self._cache_active:
            # A context that ends without the provider's end marker was cut
            # short (a quiet timeout, say), so its recording is not a complete
            # synthesis and must never be stored.
            self._discard_capture(context_id)

    async def _finalize_capture(self, context_id: str) -> None:
        capture = self._active_captures.pop(context_id, None)
        if capture is None:
            return
        if capture.aborted or capture.interrupted or capture.error:
            return
        entry = capture.to_entry()
        if entry is None:
            return
        self._schedule_store(capture.cache_key, entry)

    def _discard_capture(self, context_id: str | None) -> None:
        capture = self._active_captures.pop(context_id or "", None)
        if capture is not None:
            capture.interrupted = True
            capture.audio_chunks.clear()
            capture.word_times.clear()

    def _discard_all_captures(self) -> None:
        for context_id in list(self._active_captures):
            self._discard_capture(context_id)

    #
    # Errors
    #

    async def push_error(
        self,
        error_msg: str,
        exception: Exception | None = None,
        fatal: bool = False,
        category: Any = None,
        force_treat_as_permanent: bool = False,
    ) -> None:
        # A provider error means the synthesis it interrupted is not a
        # complete synthesis, so none of the open recordings may be stored.
        # The error itself is still reported.
        self._discard_all_captures()
        await super().push_error(
            error_msg,
            exception=exception,
            fatal=fatal,
            category=category,
            force_treat_as_permanent=force_treat_as_permanent,
        )

    #
    # Cache writes
    #

    def _schedule_store(self, cache_key: str, entry: CachedAudioEntry) -> None:
        """Store a clip without holding up the provider's receive loop."""
        if self._writes_closed:
            return

        async def _store() -> None:
            try:
                if self._tts_cache is not None:
                    await self._tts_cache.store(cache_key, entry)
            except Exception:
                logger.debug("TTS audio cache store failed", exc_info=True)

        try:
            task = asyncio.get_running_loop().create_task(_store())
        except RuntimeError:
            return
        self._pending_writes.add(task)
        task.add_done_callback(self._pending_writes.discard)

    async def drain_cache_writes(self) -> None:
        """Wait for the cache writes that are still in flight."""
        while self._pending_writes:
            await asyncio.gather(*list(self._pending_writes), return_exceptions=True)

    async def _close_cache_writes(self) -> None:
        """Take ownership of the pending writes when the service goes away."""
        self._writes_closed = True
        pending = list(self._pending_writes)
        if not pending:
            return
        _done, unfinished = await asyncio.wait(pending, timeout=CACHE_WRITE_DRAIN_SECONDS)
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)

    async def stop(self, frame: EndFrame) -> None:
        await super().stop(frame)
        await self._close_cache_writes()

    async def cancel(self, frame: CancelFrame) -> None:
        await super().cancel(frame)
        await self._close_cache_writes()

    async def cleanup(self) -> None:
        await super().cleanup()
        await self._close_cache_writes()

    #
    # Interruption
    #

    async def _handle_interruption(self, frame: InterruptionFrame, direction: FrameDirection) -> None:
        if self._cache_active:
            # Uninterruptible speech keeps playing, so its recording stays
            # open and the provider can still complete it.
            preserved = {
                context_id
                for context_id, context in self._tts_contexts.items()
                if not context.interruptible
            }
            for context_id, capture in list(self._active_captures.items()):
                if context_id in preserved:
                    continue
                capture.interrupted = True
                capture.audio_chunks.clear()
                self._active_captures.pop(context_id, None)
        await super()._handle_interruption(frame, direction)

    #
    # Synthesis
    #

    async def _synthesize_text(self, text: str, context_id: str) -> None:
        """Look the segment up, replay it on a hit, capture it on a miss.

        The base class calls this with the exact text that would be sent to
        the provider, after the request has been announced and the spoken text
        registered, so a hit replaces only the synthesis itself.
        """
        cache = self._tts_cache
        if not self._cache_active or cache is None:
            await super()._synthesize_text(text, context_id)
            return

        # Only an explicitly allowlisted phrase is ever looked up or stored:
        # generated speech is never assumed to be safe to keep.
        if not cache.is_eligible(text):
            await self._synthesize_with_provider(text, context_id)
            return

        cache_key = self._build_cache_key(text)
        if cache_key is not None:
            entry = await cache.lookup(cache_key)
            if entry is not None and await self._replay_cached_entry(context_id, entry):
                return
            # A clip that does not fit this service is an ordinary miss.
            self._active_captures[context_id] = AudioCaptureSession(
                cache_key=cache_key,
                sample_rate=self.sample_rate,
                num_channels=1,
                encoding=self._supported_encoding() or "pcm_s16le",
            )

        await self._synthesize_with_provider(text, context_id)

    async def _synthesize_with_provider(self, text: str, context_id: str) -> None:
        """Synthesize with the provider, streaming the audio as it arrives."""
        await super()._synthesize_text(text, context_id)
        # The provider only has to be told that the synthesis is over when it
        # actually took the text, which is what run_tts recorded.
        if context_id in self._provider_contexts:
            await self._close_provider_context(context_id)

    async def _close_provider_context(self, context_id: str) -> None:
        if context_id not in self._provider_contexts:
            return
        self._provider_contexts.discard(context_id)
        try:
            await self._close_provider_context_on_provider(context_id)
        except Exception as exc:
            # The provider never learned that the context was finished, so
            # the segment can never complete: drop the recording and report
            # the failure rather than pretending the synthesis finished.
            self._discard_capture(context_id)
            logger.warning("Failed to close the TTS provider context", exc_info=True)
            await self.append_to_audio_context(
                context_id, ErrorFrame(error=f"Failed to close the TTS provider context: {exc}")
            )

    async def _close_provider_context_on_provider(self, context_id: str) -> None:
        raise NotImplementedError

    async def _replay_cached_entry(self, context_id: str, entry: CachedAudioEntry) -> bool:
        """Play a stored clip through the normal audio context.

        The clip is replayed under the context it was asked for, with fresh
        frame and context identifiers, and its word timings are remapped onto
        that context so the captions land on the words they belong to. A clip
        that does not match this service's format is not replayed at all.
        """
        encoding = self._supported_encoding()
        if encoding is None:
            return False
        if (
            entry.sample_rate != self.sample_rate
            or entry.num_channels != 1
            or entry.encoding != encoding
        ):
            return False

        if not self.audio_context_available(context_id):
            # ElevenLabs opens the audio context while it sends the text. On a
            # hit the provider is never contacted, so the context is opened
            # here instead.
            await self.create_audio_context(context_id)
            await self.append_to_audio_context(context_id, TTSStartedFrame(context_id=context_id))

        sample_rate = entry.sample_rate
        channels = entry.num_channels
        bytes_per_second = max(sample_rate * 2 * channels, 1)
        chunk_size = max(int(bytes_per_second * REPLAY_CHUNK_SECONDS), 2)

        words = sorted(entry.word_times, key=lambda item: item[1])
        audio = entry.audio
        offset = 0
        word_index = 0
        while offset < len(audio):
            end = min(offset + chunk_size, len(audio))
            frame = TTSAudioRawFrame(
                audio=audio[offset:end],
                sample_rate=sample_rate,
                num_channels=channels,
                context_id=context_id,
            )
            await self.append_to_audio_context(context_id, frame)
            # The words of the chunk that was just queued follow it, the way
            # the provider's own timings follow the audio they describe.
            chunk_end_time = end / bytes_per_second
            while word_index < len(words) and words[word_index][1] < chunk_end_time:
                word, timestamp, spacing = words[word_index]
                word_index += 1
                await self.add_word_timestamps(
                    [(word, timestamp)], context_id, includes_inter_frame_spaces=spacing
                )
            offset = end
        while word_index < len(words):
            word, timestamp, spacing = words[word_index]
            word_index += 1
            await self.add_word_timestamps(
                [(word, timestamp)], context_id, includes_inter_frame_spaces=spacing
            )

        await self.append_to_audio_context(context_id, TTSStoppedFrame(context_id=context_id))
        await self.remove_audio_context(context_id)
        return True

    #
    # Cache identity
    #

    def _build_cache_key(self, prepared_text: str) -> str | None:
        assert self._tts_cache is not None
        settings = (
            self._settings.given_fields() if hasattr(self._settings, "given_fields") else {}
        )
        return self._tts_cache.build_cache_key(
            agent_id=self._agent_id,
            api_key=self._api_key,
            provider=self.provider_name,
            model=str(self._settings.model),
            voice_id=str(self._settings.voice),
            language=str(getattr(self._settings, "language", "") or "") or None,
            sample_rate=self.sample_rate,
            channels=1,
            encoding=self._supported_encoding() or "",
            settings=settings,
            text=prepared_text,
            endpoint=self._cache_endpoint(),
            container=self._cache_container(),
            extra_identity=self._cache_extra_identity(),
        )

    def _supported_encoding(self) -> str | None:
        """The encoding of the audio this service produces, if it can be cached."""
        raise NotImplementedError

    def _cache_endpoint(self) -> str:
        raise NotImplementedError

    def _cache_container(self) -> str:
        raise NotImplementedError

    def _cache_extra_identity(self) -> dict[str, Any]:
        return {}


class CachedCartesiaTTSService(_CachedTTSAdapter, CartesiaTTSService):
    provider_name = "cartesia"

    def __init__(
        self,
        *args: Any,
        cache: TTSAudioCache | None = None,
        agent_id: str | Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._init_cache(cache, agent_id)

    async def _close_provider_context_on_provider(self, context_id: str) -> None:
        # Ending Cartesia's transcript is what makes it report the synthesis
        # as done, which is the marker a capture is completed on.
        await self.flush_audio(context_id=context_id)

    def _supported_encoding(self) -> str | None:
        # Only 16-bit PCM can be replayed as it was captured.
        return "pcm_s16le" if self._output_encoding == "pcm_s16le" else None

    def _cache_endpoint(self) -> str:
        return f"{self._url}|{self._cartesia_version}"

    def _cache_container(self) -> str:
        return self._output_container

    def _cache_extra_identity(self) -> dict[str, Any]:
        return {
            "max_buffer_delay_ms": self._max_buffer_delay_ms,
            "output_encoding": self._output_encoding,
            # The handshake headers can change which account and endpoint the
            # provider talks to, so they are part of the identity. They are
            # only ever hashed, never logged.
            "extra_headers": self._extra_headers,
        }


class CachedElevenLabsTTSService(_CachedTTSAdapter, ElevenLabsTTSService):
    provider_name = "elevenlabs"

    def __init__(
        self,
        *args: Any,
        cache: TTSAudioCache | None = None,
        agent_id: str | Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._init_cache(cache, agent_id)
        # Alignment clock per provider context. Upstream keeps a single clock
        # for the whole service, which is only safe while one context is open
        # at a time; with a context per segment the contexts interleave, and a
        # shared clock would hand one context the timestamps of another.
        self._context_word_clocks: dict[str, dict[str, Any]] = {}

    async def _close_provider_context_on_provider(self, context_id: str) -> None:
        # Closing the context makes ElevenLabs report the synthesis as final.
        await self._close_context(context_id)

    def _supported_encoding(self) -> str | None:
        # The output format is a 16-bit PCM format for every supported rate.
        return "pcm_s16le" if self._output_format.startswith("pcm_") else None

    def _cache_endpoint(self) -> str:
        return self._url

    def _cache_container(self) -> str:
        return self._output_format

    def _cache_extra_identity(self) -> dict[str, Any]:
        return {
            "auto_mode": self._auto_mode,
            "enable_ssml_parsing": self._enable_ssml_parsing,
            "enable_logging": self._enable_logging,
            "pronunciation_dictionary_locators": self._pronunciation_dictionary_locators,
        }

    def _word_clock(self, context_id: str | None) -> dict[str, Any]:
        clock = self._context_word_clocks.get(context_id or "")
        if clock is None:
            clock = {
                "cumulative_time": 0.0,
                "partial_word": "",
                "partial_word_start_time": 0.0,
            }
            self._context_word_clocks[context_id or ""] = clock
        return clock

    async def on_audio_context_completed(self, context_id: str) -> None:
        await super().on_audio_context_completed(context_id)
        self._context_word_clocks.pop(context_id, None)

    async def _receive_messages(self) -> None:
        if not self._cache_active:
            await super()._receive_messages()
            return
        # Same as the upstream receive loop, with the alignment clock kept per
        # context so that contexts which interleave cannot reset or inherit
        # each other's word timings.
        try:
            async for message in self._get_websocket():
                msg = json.loads(message)
                received_ctx_id = msg.get("contextId")

                if msg.get("error"):
                    # The provider reported a failure: the segment is not a
                    # complete synthesis and must never be stored.
                    self._discard_capture(received_ctx_id)
                    await self.push_error(error_msg=f"ElevenLabs reported an error: {msg['error']}")
                    continue

                if msg.get("isFinal") is True:
                    if self.audio_context_available(received_ctx_id):
                        await self.append_to_audio_context(
                            received_ctx_id, TTSStoppedFrame(context_id=received_ctx_id)
                        )
                        await self.remove_audio_context(received_ctx_id)
                    continue

                if msg.get("audio"):
                    audio = base64.b64decode(msg["audio"])
                    frame = TTSAudioRawFrame(audio, self.sample_rate, 1, context_id=received_ctx_id)
                    await self.append_to_audio_context(received_ctx_id, frame)

                raw_alignment = _select_alignment(
                    msg,
                    normalized_key="normalizedAlignment",
                    alignment_key="alignment",
                    prefer_normalized=bool(self._pronunciation_dictionary_locators),
                )
                if raw_alignment:
                    alignment = _strip_utterance_leading_spaces(
                        raw_alignment,
                        ("chars", "charStartTimesMs", "charDurationsMs"),
                        received_ctx_id not in self._alignment_started_context_ids,
                    )
                    self._alignment_started_context_ids.add(received_ctx_id)
                    clock = self._word_clock(received_ctx_id)
                    word_times, clock["partial_word"], clock["partial_word_start_time"] = (
                        calculate_word_times(
                            alignment,
                            clock["cumulative_time"],
                            clock["partial_word"],
                            clock["partial_word_start_time"],
                        )
                    )

                    if word_times:
                        await self.add_word_timestamps(
                            word_times,
                            received_ctx_id,
                            includes_inter_frame_spaces=(
                                True
                                if _word_timestamps_include_inter_frame_spaces(
                                    assert_given(self._settings.language)
                                )
                                else None
                            ),
                        )

                    char_start_times_ms = alignment.get("charStartTimesMs", [])
                    char_durations_ms = alignment.get("charDurationsMs", [])
                    if char_start_times_ms and char_durations_ms:
                        chunk_end_time_ms = char_start_times_ms[-1] + char_durations_ms[-1]
                        clock["cumulative_time"] += chunk_end_time_ms / 1000.0
                    elif word_times:
                        clock["cumulative_time"] = word_times[-1][1]
        except Exception:
            # The receive loop gave up: nothing that is still open can be a
            # complete synthesis.
            self._discard_all_captures()
            raise
