from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest
from websockets.protocol import State

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    ErrorFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.services.cartesia.tts import CartesiaTTSSettings
from pipecat.services.elevenlabs.tts import ElevenLabsTTSSettings
from pipecat.utils.asyncio.task_manager import TaskManager

from app.cached_tts import CachedCartesiaTTSService, CachedElevenLabsTTSService
from app.tts_cache import CachedAudioEntry, TTSAudioCache
from tts_cache_test_support import (
    RecordingTTSAudioCache,
    tts_cache_redis_url,
)

ALLOWED_PHRASE = "Hello!"
LIVE_PHRASE = "Your order shipped yesterday."
FAREWELL_PHRASE = "Thank you for calling. Goodbye!"
# Two phrases of the default allowlist, so both of them are cached.
FIRST_PHRASE = "Hello!"
SECOND_PHRASE = "How can I help you?"
SAMPLE_AUDIO = b"\x11\x22" * 2400  # 4,800 bytes: 0.1s of 24kHz mono PCM

PROVIDERS = {
    "cartesia": (
        CachedCartesiaTTSService,
        lambda: CartesiaTTSSettings(model="sonic-3.6", voice="voice-test", language="en"),
    ),
    "elevenlabs": (
        CachedElevenLabsTTSService,
        lambda: ElevenLabsTTSSettings(
            model="eleven_flash_v2_5", voice="voice-test", language="en"
        ),
    ),
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(params=sorted(PROVIDERS))
def provider(request) -> str:
    return request.param


@pytest.fixture
async def cache():
    """A cache of the test's own, with the keys it wrote.

    The cache tests never write into the Redis the application coordinates its
    calls with: they have an audio cache of their own, and they remove the
    keys they wrote when they are done.
    """
    instance = RecordingTTSAudioCache(redis_url=tts_cache_redis_url())
    yield instance
    try:
        await instance.remove_stored_keys()
    finally:
        await instance.close()


def create_test_setup() -> FrameProcessorSetup:
    return FrameProcessorSetup(
        clock=SystemClock(),
        task_manager=TaskManager(),
        pipeline_worker=MagicMock(),
    )


def build_alignment(words: list[tuple[str, float]], *, char_ms: int = 10) -> dict[str, list]:
    """Build the character alignment ElevenLabs reports for a list of words."""
    chars: list[str] = []
    starts: list[int] = []
    durations: list[int] = []
    for word, start in words:
        for char in word:
            chars.append(char)
            starts.append(int(start * 1000))
            durations.append(char_ms)
        chars.append(" ")
        starts.append(int(start * 1000) + char_ms * len(word))
        durations.append(char_ms)
    return {"chars": chars, "charStartTimesMs": starts, "charDurationsMs": durations}


class ProviderWebSocket:
    """A fake provider socket that answers the way the real providers do.

    Cartesia sends the audio of a transcript when it receives the text and
    reports the synthesis as done when the transcript is closed. ElevenLabs
    sends the audio when it receives the text and reports the synthesis as
    final when its context is closed.
    """

    def __init__(
        self,
        provider: str,
        *,
        audio: bytes = SAMPLE_AUDIO,
        words: list[tuple[str, float]] | None = None,
        auto_reply: bool = True,
        audio_for: Any = None,
        stream_first: bool = False,
        split_alignment: bool = False,
        alignment_for: Any = None,
        error: bool = False,
        fail_on_close: bool = False,
    ) -> None:
        self.provider = provider
        self.audio = audio
        self.audio_for = audio_for
        self.words = words if words is not None else [("Hello", 0.05)]
        self.auto_reply = auto_reply
        self.stream_first = stream_first
        self.split_alignment = split_alignment
        self.alignment_for = alignment_for
        self.error = error
        self.fail_on_close = fail_on_close
        self.state = State.OPEN
        self.sent: list[dict[str, Any]] = []
        self._pending: list[dict[str, Any]] = []
        self._deferred_chunks: dict[str, int] = {}
        self._incoming: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, message: str) -> None:
        msg = json.loads(message)
        if self.fail_on_close and (
            msg.get("close_context") or msg.get("continue") is False
        ):
            raise RuntimeError("the provider refused to close the context")
        self.sent.append(msg)
        immediate, deferred = self._replies_for(msg)
        if self.auto_reply:
            for reply in immediate:
                await self.put(reply)
            self._pending.extend(deferred)
        else:
            self._pending.extend(immediate)
            self._pending.extend(deferred)

    def _audio_for(self, text: str) -> bytes:
        if self.audio_for is not None:
            return self.audio_for(text)
        return self.audio

    def _replies_for(self, msg: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The replies the provider sends now and the ones it sends later."""
        if self.provider == "cartesia":
            context_id = msg.get("context_id", "")
            immediate: list[dict[str, Any]] = []
            deferred: list[dict[str, Any]] = []
            if msg.get("transcript"):
                immediate.append(
                    {
                        "type": "chunk",
                        "context_id": context_id,
                        "data": base64.b64encode(
                            self._audio_for(msg["transcript"])
                        ).decode("ascii"),
                    }
                )
                immediate.append(
                    {
                        "type": "timestamps",
                        "context_id": context_id,
                        "word_timestamps": {
                            "words": [word for word, _ in self.words],
                            "start": [start for _, start in self.words],
                            "end": [start + 0.1 for _, start in self.words],
                        },
                    }
                )
            if msg.get("continue") is False:
                if self.error:
                    deferred.append(
                        {
                            "type": "error",
                            "context_id": context_id,
                            "error": "the provider gave up",
                        }
                    )
                final = {"type": "done", "context_id": context_id}
                if self.stream_first or self.error:
                    deferred.append(final)
                else:
                    immediate.append(final)
            return immediate, deferred

        context_id = msg.get("context_id", "")
        immediate = []
        deferred = []
        if (msg.get("text") or "").strip():
            chunks = self._alignment_chunks(msg["text"])
            message: dict[str, Any] = {
                "audio": base64.b64encode(
                    self._audio_for(msg["text"])
                ).decode("ascii"),
                "contextId": context_id,
            }
            if chunks:
                message["alignment"] = chunks[0]
            immediate.append(message)
            for chunk in chunks[1:]:
                deferred.append({"contextId": context_id, "alignment": chunk})
            self._deferred_chunks[context_id] = len(chunks) - 1
            if self.error:
                deferred.append(
                    {"error": {"message": "the provider gave up"}, "contextId": context_id}
                )
        final = {"isFinal": True, "contextId": context_id}
        if msg.get("close_context"):
            # The provider reports the synthesis as final once its last
            # message has been sent, so the final waits for the rest.
            if self.stream_first or self.error or self._deferred_chunks.get(context_id):
                deferred.append(final)
            else:
                immediate.append(final)
        return immediate, deferred

    def _alignment_chunks(self, text: str) -> list[dict[str, list]]:
        """The alignment of a reply, split so the word clock has to carry over."""
        words = self.words
        if self.alignment_for is not None:
            words = self.alignment_for(text)
        if words is None:
            return []
        if not self.split_alignment or len(words) < 2:
            return [build_alignment(words)]
        half = len(words) // 2
        base = words[half][1]
        return [
            build_alignment(words[:half]),
            build_alignment([(word, start - base) for word, start in words[half:]]),
        ]

    async def flush_pending(self) -> None:
        """Deliver the replies that were held back."""
        pending, self._pending = self._pending, []
        for reply in pending:
            await self.put(reply)

    async def put(self, message: dict[str, Any]) -> None:
        await self._incoming.put(json.dumps(message))

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        message = await self._incoming.get()
        if message == "__CLOSE__":
            raise StopAsyncIteration
        return message

    async def close(self) -> None:
        self.state = State.CLOSED
        await self._incoming.put("__CLOSE__")

    async def wait_closed(self) -> None:
        pass


async def build_service(
    provider: str,
    *,
    cache: TTSAudioCache,
    agent_id: Any,
    websocket: ProviderWebSocket,
    api_key: str = "test-key",
    settings: Any = None,
) -> Any:
    service_cls, default_settings = PROVIDERS[provider]
    service = service_cls(
        api_key=api_key,
        settings=settings if settings is not None else default_settings(),
        cache=cache,
        agent_id=agent_id,
        sample_rate=24000,
    )
    # The socket is in place before setup, so the service never dials out.
    service._websocket = websocket
    await service.setup(create_test_setup())
    await service.start(StartFrame())
    return service


def capture_frames(service: Any) -> list[Any]:
    """Collect the frames the service pushes downstream."""
    frames: list[Any] = []
    original = service.push_frame

    async def push(frame: Any, direction: FrameDirection = FrameDirection.DOWNSTREAM) -> None:
        frames.append(frame)
        await original(frame, direction)

    service.push_frame = push
    return frames


async def wait_for(predicate, *, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


async def run_llm_turn(service: Any, text: str) -> None:
    await service.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await service.process_frame(LLMTextFrame(text=text), FrameDirection.DOWNSTREAM)
    await service.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)


def stopped_frames(frames: list[Any]) -> list[Any]:
    return [f for f in frames if isinstance(f, TTSStoppedFrame)]


def started_frames(frames: list[Any]) -> list[Any]:
    return [f for f in frames if isinstance(f, TTSStartedFrame)]


def error_frames(frames: list[Any]) -> list[Any]:
    return [f for f in frames if isinstance(f, ErrorFrame)]


def audio_bytes(frames: list[Any]) -> bytes:
    return b"".join(f.audio for f in frames if isinstance(f, TTSAudioRawFrame))


def audio_frames(frames: list[Any]) -> list[Any]:
    return [f for f in frames if isinstance(f, TTSAudioRawFrame)]


def word_frames(frames: list[Any]) -> list[Any]:
    return [f for f in frames if isinstance(f, TTSTextFrame)]


async def speak_and_settle(service: Any, frames: list[Any], text: str) -> None:
    await run_llm_turn(service, text)
    await wait_for(lambda: bool(stopped_frames(frames)))
    await service.drain_cache_writes()


async def stored_keys(cache: TTSAudioCache) -> set[str]:
    """The keys the cache was asked to store."""
    return set(getattr(cache, "stored_keys", set()))


def provider_texts(websocket: Any) -> list[tuple[str, str]]:
    """The text the provider was given, with the context it was given under."""
    sent: list[tuple[str, str]] = []
    for msg in websocket.sent:
        text = msg.get("transcript") or msg.get("text") or ""
        text = text.strip()
        if text:
            sent.append((str(msg.get("context_id")), text))
    return sent


@pytest.mark.anyio
async def test_ordinary_speech_completes_its_turn(cache: TTSAudioCache, provider: str):
    """A reply that is not allowlisted still has to end its turn.

    A provider only reports a synthesis as done once the context it was given
    is closed, so every segment's provider context has to be closed when that
    segment ends. Without that the turn never completes and the call hangs.
    """
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider, words=[("Your", 0.01), ("order", 0.03)])
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        await speak_and_settle(service, frames, LIVE_PHRASE)

        assert stopped_frames(frames), "the turn never produced a stop frame"
        assert audio_bytes(frames) == SAMPLE_AUDIO

        if provider == "cartesia":
            assert any(m.get("continue") is False for m in websocket.sent), (
                "the provider context of the segment was never closed"
            )
        else:
            assert any(m.get("close_context") for m in websocket.sent), (
                "the provider context of the segment was never closed"
            )
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_the_first_audio_plays_while_the_synthesis_is_still_running(
    cache: TTSAudioCache, provider: str
):
    """A cold synthesis has to start playing at once, not after the whole clip."""
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider, stream_first=True)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        await run_llm_turn(service, ALLOWED_PHRASE)

        await wait_for(lambda: bool(audio_frames(frames)))
        assert audio_bytes(frames) == SAMPLE_AUDIO
        assert not stopped_frames(frames), (
            "the segment must not wait for the whole clip before playing"
        )

        await websocket.flush_pending()
        await wait_for(lambda: bool(stopped_frames(frames)))
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_elevenlabs_keeps_the_word_clock_of_each_context_apart(cache: TTSAudioCache):
    """Contexts that interleave must not reset or inherit each other's timings.

    The alignment clock tells how far into a synthesis a word was spoken. When
    two provider contexts are open at the same time, a shared clock would
    restart the second context's words at zero, or carry the first context's
    partial word into the second one.
    """
    agent_id = uuid.uuid4()

    def alignment_for(text: str):
        # Only the first segment reports words. The second one opens a
        # context of its own while the first one is still being spoken, which
        # is what would reset a shared clock.
        if text.strip() == "How can I help you?":
            return [("How", 0.0), ("can", 0.1)]
        return None

    websocket = ProviderWebSocket(
        "elevenlabs",
        words=[("How", 0.0), ("can", 0.1)],
        split_alignment=True,
        alignment_for=alignment_for,
    )
    service = await build_service(
        "elevenlabs", cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        before = await stored_keys(cache)

        # Two sentences in one turn: the second one opens its context while the
        # first one is still being spoken.
        await run_llm_turn(service, "How can I help you? Goodbye!")
        await wait_for(lambda: len(websocket.sent) >= 2)
        await websocket.flush_pending()
        await wait_for(lambda: bool(stopped_frames(frames)))
        await service.drain_cache_writes()

        stored = await stored_keys(cache)
        new_keys = stored - before
        assert new_keys, "the segments were not stored"

        timings: dict[str, float] = {}
        for key in new_keys:
            entry = await cache.lookup(key)
            assert entry is not None
            candidate = {word: timestamp for word, timestamp, _ in entry.word_times}
            if "How" in candidate:
                timings = candidate
                break

        assert timings, "the first segment was not stored with its word timings"
        # The second word keeps the clock of its own context: it is spoken
        # after the first chunk of that context, not at the start of the other
        # context that was opened in the meantime.
        assert timings["How"] == pytest.approx(0.0, abs=0.01)
        assert timings["can"] > 0.01
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_a_cache_hit_replays_without_touching_the_provider(
    cache: TTSAudioCache, provider: str
):
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)
    requests: list[tuple[str, str]] = []
    service.add_event_handler(
        "on_tts_request", lambda _service, context_id, text: requests.append((context_id, text))
    )

    try:
        # The first turn is synthesized by the provider and stored.
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        assert len(stopped_frames(frames)) == 1
        first_context = requests[-1][0]
        provider_messages = len(websocket.sent)
        assert provider_messages > 0

        frames.clear()
        requests.clear()

        # The second turn is replayed from the cache.
        await speak_and_settle(service, frames, ALLOWED_PHRASE)

        assert len(requests) == 1, "the replay has to announce exactly one request"
        assert requests[0][1] == ALLOWED_PHRASE
        assert requests[0][0] != first_context, "the replay has to use a fresh context"
        assert len(started_frames(frames)) == 1
        assert len(stopped_frames(frames)) == 1
        assert audio_bytes(frames) == SAMPLE_AUDIO
        assert len(websocket.sent) == provider_messages, (
            "a cache hit must not send anything to the provider"
        )
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_a_replay_keeps_the_words_the_live_turn_had(
    cache: TTSAudioCache, provider: str
):
    """A replayed clip has to speak the same words, at the same times."""
    agent_id = uuid.uuid4()
    service_cls, default_settings = PROVIDERS[provider]
    settings = default_settings()
    # A language whose words carry their own spacing, so the flag that says
    # so has to survive the round trip through the cache.
    settings.language = "zh"
    websocket = ProviderWebSocket(provider, words=[("Hello", 0.01), ("world", 0.05)])
    service = await build_service(
        provider,
        cache=cache,
        agent_id=agent_id,
        websocket=websocket,
        settings=settings,
    )
    frames = capture_frames(service)

    try:
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        live_words = word_frames(frames)
        assert live_words, "the live turn reported no words"
        assert any(f.includes_inter_frame_spaces for f in live_words)

        frames.clear()
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        replayed_words = word_frames(frames)

        assert [f.text for f in replayed_words] == [f.text for f in live_words]
        assert [f.includes_inter_frame_spaces for f in replayed_words] == [
            f.includes_inter_frame_spaces for f in live_words
        ]
        live_gaps = [
            b.pts - a.pts for a, b in zip(live_words, live_words[1:])
        ]
        replayed_gaps = [
            b.pts - a.pts for a, b in zip(replayed_words, replayed_words[1:])
        ]
        assert replayed_gaps == live_gaps
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_a_hit_and_a_miss_keep_their_order(cache: TTSAudioCache, provider: str):
    """A cached segment and a live one in the same turn keep their order."""
    allowed_audio = b"\x11" * 2000
    live_audio = b"\x22" * 3000

    def audio_for(text: str) -> bytes:
        return allowed_audio if text.strip() == ALLOWED_PHRASE else live_audio

    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider, audio_for=audio_for)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        # Store the clip.
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        frames.clear()

        # One turn with a cached segment first and a live one second.
        await speak_and_settle(service, frames, f"{ALLOWED_PHRASE} {LIVE_PHRASE}")

        started = started_frames(frames)
        stopped = stopped_frames(frames)
        assert len(started) == 2, "the turn has to speak both segments"
        assert len(stopped) == 2
        assert started[0].context_id != started[1].context_id
        assert stopped[0].context_id == started[0].context_id
        assert stopped[1].context_id == started[1].context_id

        # The cached segment is played before the live one.
        assert audio_bytes(frames) == allowed_audio + live_audio
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_every_segment_of_a_turn_has_its_own_provider_context(
    cache: TTSAudioCache, provider: str
):
    """The segments of one turn are isolated from each other at the provider.

    The provider's answer to the first segment is held back while the second
    sentence is sent, so the two segments overlap at the provider. Every
    segment has to be sent under a context of its own: with one shared
    context the capture of a segment would hold the audio of the other, and a
    cached clip would no longer be one complete segment.
    """
    first_audio = b"\x11" * 2000
    second_audio = b"\x22" * 3000

    def audio_for(text: str) -> bytes:
        return first_audio if text.strip() == FIRST_PHRASE else second_audio

    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider, auto_reply=False, audio_for=audio_for)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        # One turn with two segments. The provider's answers are held back,
        # so the first segment is still open when the second one is sent.
        await service.process_frame(
            LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM
        )
        await service.process_frame(
            LLMTextFrame(text=FIRST_PHRASE), FrameDirection.DOWNSTREAM
        )
        await service.process_frame(
            LLMTextFrame(text=SECOND_PHRASE), FrameDirection.DOWNSTREAM
        )
        await service.process_frame(
            LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM
        )

        assert await wait_for(lambda: len(provider_texts(websocket)) >= 2), (
            "the turn did not send both segments to the provider"
        )
        sent = provider_texts(websocket)
        assert [text for _, text in sent] == [FIRST_PHRASE, SECOND_PHRASE]
        assert sent[0][0] != sent[1][0], (
            "the segments of one turn shared a provider context"
        )

        # The provider answers both segments now.
        await websocket.flush_pending()
        assert await wait_for(lambda: len(stopped_frames(frames)) >= 2), (
            "the turn did not finish both segments"
        )
        await service.drain_cache_writes()

        # Every segment has its own audio, under its own context.
        audio_contexts = [f.context_id for f in audio_frames(frames)]
        assert len(set(audio_contexts)) == 2, "the segments shared their audio"

        # And every segment is stored as a clip of its own.
        keys = await stored_keys(cache)
        assert len(keys) == 2, "the segments were not stored as two clips"
        stored_audio = set()
        for key in keys:
            entry = await cache.lookup(key)
            assert entry is not None, "a stored clip could not be read back"
            stored_audio.add(entry.audio)
        assert stored_audio == {first_audio, second_audio}, (
            "a stored clip holds more than one segment"
        )
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_a_hit_and_a_miss_of_one_turn_keep_their_own_context(
    cache: TTSAudioCache, provider: str
):
    """A replayed segment and a live one do not share a context.

    The replay is played while the provider is still working on the live
    segment, so the two overlap. If they shared a context the replayed audio
    and the live audio would end up in the same segment, and the cached clip
    would be overwritten by the live one.
    """
    allowed_audio = b"\x11" * 2000
    live_audio = b"\x22" * 3000

    def audio_for(text: str) -> bytes:
        return allowed_audio if text.strip() == ALLOWED_PHRASE else live_audio

    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider, audio_for=audio_for)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        # Store the clip of the first phrase.
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        frames.clear()
        websocket.sent.clear()

        # The provider's answer to the live segment is held back, so the
        # replay of the cached segment and the live segment overlap.
        websocket.auto_reply = False
        await service.process_frame(
            LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM
        )
        await service.process_frame(
            LLMTextFrame(text=ALLOWED_PHRASE), FrameDirection.DOWNSTREAM
        )
        await service.process_frame(
            LLMTextFrame(text=LIVE_PHRASE), FrameDirection.DOWNSTREAM
        )
        await service.process_frame(
            LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM
        )

        assert await wait_for(lambda: len(provider_texts(websocket)) >= 1), (
            "the live segment never reached the provider"
        )
        await websocket.flush_pending()
        assert await wait_for(lambda: len(stopped_frames(frames)) >= 2), (
            "the turn did not finish both segments"
        )
        await service.drain_cache_writes()

        # The provider was only asked for the live phrase: the cached one was
        # replayed, not synthesized again.
        assert [text for _, text in provider_texts(websocket)] == [LIVE_PHRASE]

        # The two segments have their own contexts and their own audio.
        started = started_frames(frames)
        assert len(started) == 2, "the turn has to speak both segments"
        assert started[0].context_id != started[1].context_id
        assert audio_bytes(frames) == allowed_audio + live_audio

        # The cached clip is untouched by the live segment.
        keys = await stored_keys(cache)
        assert len(keys) == 1, "the cached clip was overwritten"
        entry = await cache.lookup(next(iter(keys)))
        assert entry is not None
        assert entry.audio == allowed_audio
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_an_interrupted_synthesis_stores_nothing(cache: TTSAudioCache, provider: str):
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider, auto_reply=False)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        # The provider is given the text but answers late.
        await run_llm_turn(service, ALLOWED_PHRASE)
        await wait_for(lambda: bool(websocket.sent))

        # The caller is interrupted while the synthesis is still running.
        await service.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

        # The provider's final answer arrives after the interruption.
        await websocket.flush_pending()
        await service.drain_cache_writes()
        await asyncio.sleep(0.05)

        assert not stopped_frames(frames), (
            "an interrupted segment must not report a completed synthesis"
        )

        # Nothing was stored, so the same phrase has to be synthesized again.
        websocket.sent.clear()
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        assert websocket.sent, "the interrupted segment must not have been stored"
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_a_provider_error_is_never_stored(cache: TTSAudioCache, provider: str):
    """A failed synthesis must not leave a clip of what was said before it."""
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider, error=True)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        await run_llm_turn(service, ALLOWED_PHRASE)
        await wait_for(lambda: bool(websocket.sent))

        # The provider reports the failure and then, late, the end of the
        # synthesis it gave up on.
        await websocket.flush_pending()
        await service.drain_cache_writes()
        await asyncio.sleep(0.05)

        assert error_frames(frames), "the provider error was not reported"

        # Nothing was stored, so the same phrase has to be synthesized again.
        websocket.sent.clear()
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        assert websocket.sent, "the failed segment must not have been stored"
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_too_many_word_events_store_nothing(cache: TTSAudioCache, provider: str):
    """A provider that floods word events cannot fill the memory of a call."""
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(
        provider, words=[("w", 0.0) for _ in range(2100)]
    )
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        before = await stored_keys(cache)
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        assert stopped_frames(frames), "the call still has to speak"

        assert (await stored_keys(cache)) - before == set(), (
            "the flooded segment must not have been stored"
        )
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_a_word_that_is_too_long_stores_nothing(cache: TTSAudioCache, provider: str):
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider, words=[("x" * 5000, 0.05)])
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        before = await stored_keys(cache)
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        assert stopped_frames(frames), "the call still has to speak"

        assert (await stored_keys(cache)) - before == set(), (
            "the malformed segment must not have been stored"
        )
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_after_a_cleanup_no_clip_can_be_stored(cache: TTSAudioCache, provider: str):
    """Once the service is gone, a late answer cannot store anything."""
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider, stream_first=True)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        before = await stored_keys(cache)
        await run_llm_turn(service, ALLOWED_PHRASE)
        await wait_for(lambda: bool(started_frames(frames)))
        started = started_frames(frames)
        assert started, "the segment never started"
        context_id = started[0].context_id

        await service.cleanup()

        # The provider's final answer arrives after the service is gone.
        await websocket.flush_pending()
        await asyncio.sleep(0.05)
        assert (await stored_keys(cache)) - before == set(), (
            "the late answer stored a clip"
        )

        # A final answer that reaches the service anyway stores nothing.
        await service.append_to_audio_context(
            context_id, TTSStoppedFrame(context_id=context_id)
        )
        await asyncio.sleep(0.05)
        assert (await stored_keys(cache)) - before == set(), (
            "a clip was stored after the service was gone"
        )
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_a_failure_to_close_the_provider_context_is_reported(
    cache: TTSAudioCache, provider: str
):
    """A context the provider never closed has to be reported, not hidden."""
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider, fail_on_close=True)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        await run_llm_turn(service, ALLOWED_PHRASE)
        await wait_for(lambda: bool(error_frames(frames)))
        await asyncio.sleep(0.05)

        assert error_frames(frames), "the failure to close the context was not reported"

        websocket.sent.clear()
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        assert websocket.sent, "the failed segment must not have been stored"
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_an_uninterruptible_farewell_completes_and_is_stored(
    cache: TTSAudioCache, provider: str
):
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(
        provider, words=[("Thank", 0.01), ("you", 0.03), ("for", 0.05), ("calling.", 0.07)]
    )
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        speak = TTSSpeakFrame(text=FAREWELL_PHRASE)
        speak.interruptible = False
        await service.process_frame(speak, FrameDirection.DOWNSTREAM)

        await wait_for(lambda: bool(stopped_frames(frames)))
        await service.drain_cache_writes()

        assert len(stopped_frames(frames)) == 1
        assert audio_bytes(frames) == SAMPLE_AUDIO

        # The farewell was stored, so a second one is replayed.
        frames.clear()
        websocket.sent.clear()
        await service.process_frame(
            TTSSpeakFrame(text=FAREWELL_PHRASE), FrameDirection.DOWNSTREAM
        )
        await wait_for(lambda: bool(stopped_frames(frames)))
        assert not websocket.sent, "a cached farewell must not reach the provider"
        assert audio_bytes(frames) == SAMPLE_AUDIO
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_a_different_agent_does_not_share_a_clip(
    cache: TTSAudioCache, provider: str
):
    first = ProviderWebSocket(provider)
    first_service = await build_service(
        provider, cache=cache, agent_id=uuid.uuid4(), websocket=first
    )
    first_frames = capture_frames(first_service)

    try:
        await speak_and_settle(first_service, first_frames, ALLOWED_PHRASE)
        assert first.sent

        second = ProviderWebSocket(provider)
        second_service = await build_service(
            provider, cache=cache, agent_id=uuid.uuid4(), websocket=second
        )
        second_frames = capture_frames(second_service)
        try:
            await speak_and_settle(second_service, second_frames, ALLOWED_PHRASE)
            assert second.sent, "another agent must not reuse the clip"
        finally:
            await second.close()
    finally:
        await first.close()


@pytest.mark.anyio
async def test_another_voice_does_not_reuse_a_clip(cache: TTSAudioCache, provider: str):
    agent_id = uuid.uuid4()
    first = ProviderWebSocket(provider)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=first
    )
    frames = capture_frames(service)

    try:
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        assert first.sent

        service_cls, default_settings = PROVIDERS[provider]
        other_settings = default_settings()
        other_settings.voice = "voice-other"
        second = ProviderWebSocket(provider)
        other_service = await build_service(
            provider,
            cache=cache,
            agent_id=agent_id,
            websocket=second,
            settings=other_settings,
        )
        other_frames = capture_frames(other_service)
        try:
            await speak_and_settle(other_service, other_frames, ALLOWED_PHRASE)
            assert second.sent, "another voice must not reuse the clip"
        finally:
            await second.close()
    finally:
        await first.close()


@pytest.mark.anyio
async def test_a_corrupt_entry_is_treated_as_a_miss(cache: TTSAudioCache, provider: str):
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        # Store a clip and then damage it.
        before = await stored_keys(cache)
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        assert websocket.sent

        stored = (await stored_keys(cache)) - before
        assert stored, "the clip was not stored"
        for key in stored:
            await cache._client.set(key, b"not a clip at all")

        frames.clear()
        websocket.sent.clear()
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        assert websocket.sent, "a damaged entry must be treated as a miss"
        assert audio_bytes(frames) == SAMPLE_AUDIO
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_a_clip_in_another_format_is_not_replayed(cache: TTSAudioCache, provider: str):
    """A clip of another shape cannot be handed to this service."""
    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)

    try:
        before = await stored_keys(cache)
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        stored = (await stored_keys(cache)) - before
        assert stored, "the clip was not stored"

        # The very same clip, but in another sample rate.
        other = CachedAudioEntry(
            audio=b"\x00" * 3200,
            sample_rate=16000,
            num_channels=1,
            encoding="pcm_s16le",
            duration_seconds=0.1,
            word_times=[("Hello", 0.05, False)],
        )
        for key in stored:
            await cache._client.set(key, cache.serialize(other))

        frames.clear()
        websocket.sent.clear()
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        assert websocket.sent, "a clip of another format must be treated as a miss"
        assert audio_bytes(frames) == SAMPLE_AUDIO
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_a_broken_cache_still_speaks(cache: TTSAudioCache, provider: str):
    class BrokenClient:
        async def get(self, key):
            raise RuntimeError("redis is down")

        async def set(self, key, value, ex=None):
            raise RuntimeError("redis is down")

    agent_id = uuid.uuid4()
    websocket = ProviderWebSocket(provider)
    service = await build_service(
        provider, cache=cache, agent_id=agent_id, websocket=websocket
    )
    frames = capture_frames(service)
    cache._client = BrokenClient()

    try:
        await speak_and_settle(service, frames, ALLOWED_PHRASE)
        assert stopped_frames(frames), "a broken cache must not stop the call"
        assert audio_bytes(frames) == SAMPLE_AUDIO
    finally:
        await websocket.close()


@pytest.mark.anyio
async def test_without_a_cache_the_provider_keeps_its_own_lifecycle(provider: str):
    """With caching off the service must behave like the upstream provider."""
    websocket = ProviderWebSocket(provider)
    service_cls, default_settings = PROVIDERS[provider]
    service = service_cls(
        api_key="test-key",
        settings=default_settings(),
        cache=None,
        agent_id=None,
        sample_rate=24000,
    )
    service._websocket = websocket
    await service.setup(create_test_setup())
    await service.start(StartFrame())
    frames = capture_frames(service)

    try:
        await run_llm_turn(service, LIVE_PHRASE)
        await wait_for(lambda: bool(stopped_frames(frames)))

        assert stopped_frames(frames), "the turn never completed"
        # One context for the whole turn, closed once at the end of it.
        contexts = {m.get("context_id") for m in websocket.sent if m.get("context_id")}
        assert len(contexts) == 1
        closes = [m for m in websocket.sent if m.get("continue") is False or m.get("close_context")]
        assert len(closes) == 1
    finally:
        await websocket.close()
