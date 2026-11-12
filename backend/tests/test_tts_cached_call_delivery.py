"""Real cached TTS providers inside the real call runtime.

The provider sockets, the model and the media transport are the only test
doubles here. The cached TTS services, the response capture, the TTS context
tracker and the output completion are the real ones, driven through the real
pipeline of a real :class:`~app.voice.CallRuntime`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSSpeakFrame,
    TTSTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

from app import voice as voice_module
from app.cached_tts import CachedCartesiaTTSService, CachedElevenLabsTTSService
from app.voice import CallDefinition, CallRuntime, ProviderConfig
from tts_cache_test_support import (
    RecordingTTSAudioCache,
    tts_cache_redis_url,
)

CACHED_PHRASE = "Hello!"
LIVE_PHRASE = "Your order shipped yesterday."
FAREWELL_PHRASE = "Thank you for calling. Goodbye!"
CALLER_TEXT = "I need to check my order."

PROVIDERS = {
    "cartesia": CachedCartesiaTTSService,
    "elevenlabs": CachedElevenLabsTTSService,
}


def _load_provider_socket_double() -> Any:
    """Load the provider socket double the adapter tests use.

    The helper lives in another test module. Importing it by name would
    depend on the order in which pytest puts the test directory on the
    import path, so it is loaded from its file instead: this module then
    works the same on its own and in any collection order.
    """
    path = Path(__file__).with_name("test_cached_tts.py")
    spec = importlib.util.spec_from_file_location("_voice_provider_socket_double", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"the provider socket double could not be loaded from {path}")
    module = importlib.util.module_from_spec(spec)
    # The module has to be known before it runs: dataclasses resolve their
    # annotations through the module they belong to.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.ProviderWebSocket


ProviderWebSocket = _load_provider_socket_double()


class RecordingCache(RecordingTTSAudioCache):
    """The cache, with the keys it was asked to store.

    Whether a clip reached the cache is what the scenarios assert, and the
    cache is the only place that knows. It records the keys it was given, so
    the test never has to look inside the client, and it removes exactly those
    keys again when the test is over.
    """


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(params=sorted(PROVIDERS))
def provider(request) -> str:
    return request.param


@pytest.fixture
async def cache():
    """A cache of the test's own, with the keys it wrote.

    The delivery tests never write into the Redis the application coordinates
    its calls with: they have an audio cache of their own.
    """
    instance = RecordingCache(redis_url=tts_cache_redis_url())
    yield instance
    try:
        await instance.remove_stored_keys()
    finally:
        await instance.close()


def make_test_setup() -> FrameProcessorSetup:
    return FrameProcessorSetup(
        clock=SystemClock(),
        task_manager=TaskManager(),
        pipeline_worker=MagicMock(),
    )


async def wait_until(predicate, *, timeout: float = 5.0) -> bool:
    """Wait for a condition, whether it is a plain or an awaitable one."""

    async def check() -> bool:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        return bool(result)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await check():
            return True
        await asyncio.sleep(0.005)
    return False


async def settle(seconds: float = 0.25) -> None:
    """Give the pipeline the chance to do the thing that must not happen.

    Assertions about something that must not occur need a window in which it
    could have occurred; the observable that proves the window was real is
    asserted next to them.
    """
    await asyncio.sleep(seconds)


def provider_transcripts(socket: Any) -> list[str]:
    """The text the provider was given."""
    texts: list[str] = []
    for msg in socket.sent:
        text = msg.get("transcript") or msg.get("text") or ""
        text = str(text).strip()
        if text:
            texts.append(text)
    return texts


def persisted_answers(call: Call) -> list[str]:
    """The answers the call has finished delivering."""
    return call.writer.assistant_messages()


#
# Test doubles: the model, the transport and the connection.
#


class RecordingWriter:
    """The session writer, without the database."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def enqueue_message(self, role: str, content: str) -> None:
        self.messages.append((role, content))

    async def flush(self) -> None:
        return None

    def assistant_messages(self) -> list[str]:
        return [content for role, content in self.messages if role == "assistant"]


class RecordingConnection:
    """The signalling connection, without the network."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self._handlers: dict[str, list[Any]] = {}
        self.pc = None

    def add_event_handler(self, name: str, handler: Any) -> None:
        self._handlers.setdefault(name, []).append(handler)

    def send_app_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def disconnect(self) -> None:
        return None

    def captions(self, *, role: str | None = None, final: bool | None = None) -> list[dict[str, Any]]:
        found = []
        for message in self.messages:
            if message.get("type") != "caption":
                continue
            if role is not None and message.get("role") != role:
                continue
            if final is not None and bool(message.get("final")) != final:
                continue
            found.append(message)
        return found


class PassThrough(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await FrameProcessor.process_frame(self, frame, direction)
        await self.push_frame(frame, direction)


class GatedOutput(PassThrough):
    """The transport output, which holds the frames back until the test lets them go.

    The gate is what makes the order of the delivery observable: nothing the
    runtime is told about a segment reaches it until the test releases it.
    """

    #: Only the speech frames are held back; the control frames have to reach
    #: the end of the pipeline or the pipeline never starts.
    GATED = (
        TTSStartedFrame,
        TTSAudioRawFrame,
        TTSStoppedFrame,
        TTSTextFrame,
        LLMFullResponseEndFrame,
    )

    def __init__(self, gated: tuple[type, ...] | None = None) -> None:
        super().__init__()
        self.holding = True
        self.pending: list[Frame] = []
        self.events: list[str] = []
        # The frames the test wants to hold back. A test that lets the end of
        # the response through while the audio is still held back uses this.
        self.gated = tuple(gated) if gated is not None else self.GATED

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await FrameProcessor.process_frame(self, frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, InterruptionFrame):
                # The transport drops the queued frames an interruption may
                # discard, and keeps the ones that are uninterruptible.
                self.pending = [
                    queued
                    for queued in self.pending
                    if not getattr(queued, "interruptible", True)
                ]
            if self.holding and isinstance(frame, self.gated):
                self.pending.append(frame)
                return
            self._note(frame)
        await self.push_frame(frame, direction)

    def _note(self, frame: Frame) -> None:
        if isinstance(frame, TTSAudioRawFrame):
            self.events.append("audio")
        elif isinstance(frame, TTSStartedFrame):
            self.events.append("start")
        elif isinstance(frame, TTSStoppedFrame):
            self.events.append("stop")
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.events.append("response-end")

    async def release_next(self) -> Frame | None:
        if not self.pending:
            return None
        frame = self.pending.pop(0)
        self._note(frame)
        await self.push_frame(frame, FrameDirection.DOWNSTREAM)
        return frame

    async def release_all(self) -> None:
        while self.pending:
            await self.release_next()


class FakeTransport:
    """The media transport, with a gated output."""

    def __init__(
        self,
        connection: Any = None,
        params: Any = None,
        gated: tuple[type, ...] | None = None,
    ) -> None:
        self._input = PassThrough()
        self.output_processor = GatedOutput(gated=gated)

    def input(self) -> FrameProcessor:
        return self._input

    def output(self) -> FrameProcessor:
        return self.output_processor


class FakeSTT(PassThrough):
    """The speech service, driven by the test."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__()

    async def deliver_transcription(self, text: str) -> None:
        await self.push_frame(
            TranscriptionFrame(
                text=text,
                user_id="caller",
                timestamp="0",
                result=SimpleNamespace(
                    is_final=True,
                    speech_final=True,
                    from_finalize=False,
                ),
            ),
            FrameDirection.DOWNSTREAM,
        )

    async def inject_interruption(self) -> None:
        """Interrupt the bot the way a caller does: by speaking over it."""
        await self.deliver_transcription("Wait, I have a question.")


class ScriptedLLM(PassThrough):
    """The model, answering with scripted turns."""

    def __init__(self, script: list[Any]) -> None:
        super().__init__()
        self.script = list(script)
        self.tools: dict[str, Any] = {}
        self.user_turns = 0

    def register_function(self, name: str, callback: Any) -> None:
        self.tools[name] = callback

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await FrameProcessor.process_frame(self, frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMContextFrame):
            has_user_turn = any(
                message.get("role") == "user" for message in frame.context.messages
            )
            if has_user_turn:
                self.user_turns += 1
            if has_user_turn and self.script:
                step = self.script.pop(0)
                if callable(step):
                    await step(self, direction)
                else:
                    await self.speak(list(step), direction)
                return
        await self.push_frame(frame, direction)

    async def speak(self, texts: list[str], direction: FrameDirection) -> None:
        for text in texts:
            await self.push_frame(LLMFullResponseStartFrame(), direction)
            await self.push_frame(LLMTextFrame(text=text), direction)
            await self.push_frame(LLMFullResponseEndFrame(), direction)


#
# The call itself.
#


@dataclass
class Call:
    runtime: CallRuntime
    connection: RecordingConnection
    writer: RecordingWriter
    output: GatedOutput
    stt: FakeSTT
    llm: ScriptedLLM
    socket: ProviderWebSocket


def providers_config(provider: str) -> ProviderConfig:
    return ProviderConfig(
        stt_provider="deepgram",
        stt_model="nova-3",
        stt_api_key="deepgram-test-secret",
        stt_region=None,
        stt_endpoint=None,
        stt_language="en",
        llm_model="gpt-4o-mini",
        llm_api_key="openai-test-secret",
        tts_provider=provider,
        tts_model="sonic-3.6" if provider == "cartesia" else "eleven_flash_v2_5",
        tts_api_key=f"{provider}-test-secret",
        voice_id="voice-test",
        language="en",
    )


def call_definition(agent_id: uuid.UUID, providers: ProviderConfig) -> CallDefinition:
    return CallDefinition(
        session_id=uuid.uuid4(),
        agent_id=agent_id,
        call_type="test",
        agent_status="active",
        agent_name="Cached delivery test",
        agent_instructions="Help the caller.",
        global_prompt="Answer briefly.",
        prompt_version=1,
        language="en",
        max_call_duration_seconds=600,
        max_concurrent_calls_per_agent=1,
        max_concurrent_calls_overall=1,
        providers=providers,
    )


def service_settings(provider: str, providers: ProviderConfig) -> Any:
    from pipecat.services.cartesia.tts import CartesiaTTSSettings
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSSettings

    if provider == "cartesia":
        return CartesiaTTSSettings(
            model=providers.tts_model,
            voice=providers.voice_id,
            language=providers.language,
        )
    return ElevenLabsTTSSettings(
        model=providers.tts_model,
        voice=providers.voice_id,
        language=providers.language,
    )


async def prime_clip(
    provider: str,
    providers: ProviderConfig,
    agent_id: uuid.UUID,
    cache: RecordingCache,
    text: str,
    *,
    forced: bool = False,
) -> None:
    """Store the clip of ``text`` by really speaking it with the cached service."""
    socket = ProviderWebSocket(provider)
    service = PROVIDERS[provider](
        api_key=providers.tts_api_key,
        settings=service_settings(provider, providers),
        cache=cache,
        agent_id=agent_id,
    )
    # The provider opens its own connection while it is set up, and there is
    # no public way to hand it one. The socket double is in place before the
    # setup, so the service never dials out.
    service._websocket = socket
    await service.setup(make_test_setup())
    await service.start(StartFrame())
    try:
        before = set(cache.stored_keys)
        if forced:
            await service.process_frame(
                TTSSpeakFrame(text=text), FrameDirection.DOWNSTREAM
            )
        else:
            await service.process_frame(
                LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM
            )
            await service.process_frame(LLMTextFrame(text=text), FrameDirection.DOWNSTREAM)
            await service.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
        assert await wait_until(
            lambda: bool(cache.stored_keys - before), timeout=5.0
        ), "the clip was not stored"
    finally:
        await service.stop(EndFrame())


async def build_call(
    monkeypatch: Any,
    provider: str,
    providers: ProviderConfig,
    agent_id: uuid.UUID,
    cache: RecordingCache,
    socket: ProviderWebSocket,
    script: list[Any],
    gated: tuple[type, ...] | None = None,
) -> Call:
    """Start a real call whose only doubles are the model and the transport."""
    llm = ScriptedLLM(script)
    stt = FakeSTT()
    writer = RecordingWriter()
    connection = RecordingConnection()
    transport = FakeTransport(gated=gated)

    monkeypatch.setattr(voice_module, "SmallWebRTCTransport", lambda *a, **k: transport)
    monkeypatch.setattr(voice_module, "DeepgramSTTService", lambda *a, **k: stt)
    monkeypatch.setattr(voice_module, "AzureSTTService", lambda *a, **k: stt)
    monkeypatch.setattr(voice_module, "OpenAILLMService", lambda *a, **k: llm)
    monkeypatch.setattr(voice_module, "VADProcessor", lambda *a, **k: PassThrough())
    monkeypatch.setattr(voice_module, "SileroVADAnalyzer", lambda *a, **k: None)

    def cached_service(*args: Any, **kwargs: Any) -> Any:
        service = PROVIDERS[provider](*args, **kwargs)
        # The socket double is installed before the service is set up, so the
        # provider never opens a connection of its own.
        service._websocket = socket
        return service

    monkeypatch.setattr(voice_module, "CachedCartesiaTTSService", cached_service)
    monkeypatch.setattr(voice_module, "CachedElevenLabsTTSService", cached_service)

    async def no_session_status(*args: Any, **kwargs: Any) -> bool:
        return True

    # The session status is the only thing that would reach the database.
    monkeypatch.setattr("app.sessions.set_session_status", no_session_status)

    runtime = CallRuntime(
        call_definition(agent_id, providers), writer, tts_cache=cache
    )
    await runtime.attach(connection)
    return Call(
        runtime=runtime,
        connection=connection,
        writer=writer,
        output=transport.output_processor,
        stt=stt,
        llm=llm,
        socket=socket,
    )


async def stop_call(call: Call) -> None:
    runtime = call.runtime
    if not runtime.closing:
        runtime.request_stop("cancelled")
    if runtime.cleanup_task is not None:
        await asyncio.wait_for(asyncio.shield(runtime.cleanup_task), timeout=15)
    worker_task = runtime.worker_task
    if worker_task is not None and not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.anyio
async def test_a_cached_first_sentence_does_not_finish_the_response(
    monkeypatch: Any, cache: RecordingCache, provider: str
):
    """One cached sentence and one live sentence make one answer, delivered once.

    The first segment stops at the transport long before the second one does.
    That must not finish the answer: the answer is the whole response, and it
    is only delivered once the last audio and the end of the response have
    both passed the transport.
    """
    agent_id = uuid.uuid4()
    providers = providers_config(provider)
    await prime_clip(provider, providers, agent_id, cache, CACHED_PHRASE)

    socket = ProviderWebSocket(provider)
    call = await build_call(
        monkeypatch,
        provider,
        providers,
        agent_id,
        cache,
        socket,
        script=[(f"{CACHED_PHRASE} {LIVE_PHRASE}",)],
    )
    try:
        await call.stt.deliver_transcription(CALLER_TEXT)
        assert await wait_until(
            lambda: len(call.output.pending) >= 2, timeout=5.0
        ), "the call never started speaking"

        # Let the frames go one at a time and watch what the runtime does.
        first_stop_seen = False
        while call.output.pending:
            frame = await call.output.release_next()
            if isinstance(frame, TTSStoppedFrame):
                first_stop_seen = True
                break
        assert first_stop_seen, "the first segment never stopped"
        # The second segment is still held at the transport, so the whole
        # response cannot have been delivered yet.
        assert any(
            isinstance(frame, TTSAudioRawFrame) for frame in call.output.pending
        ), "the second segment was delivered together with the first one"
        await settle()

        assert call.writer.assistant_messages() == [], (
            "the answer was stored before the whole response was delivered"
        )
        assert call.connection.captions(role="assistant", final=True) == [], (
            "the answer was captioned before the whole response was delivered"
        )

        # The rest of the response completes the answer, exactly once.
        await call.output.release_all()
        assert await wait_until(
            lambda: len(call.writer.assistant_messages()) == 1, timeout=5.0
        )
        assert call.writer.assistant_messages() == [f"{CACHED_PHRASE} {LIVE_PHRASE}"]
        finals = call.connection.captions(role="assistant", final=True)
        assert len(finals) == 1
        assert finals[0]["text"] == f"{CACHED_PHRASE} {LIVE_PHRASE}"
    finally:
        await stop_call(call)


@pytest.mark.anyio
async def test_a_reply_whose_audio_arrives_after_the_end_of_the_response_is_kept(
    monkeypatch: Any, cache: RecordingCache, provider: str
):
    """The end of the response can reach the runtime before the audio.

    The output of the last segment can still be on its way when the end of
    the response is handed on. The reply may not be lost then: it is stored
    once the output of its segment is in as well, and not before.
    """
    agent_id = uuid.uuid4()
    providers = providers_config(provider)
    socket = ProviderWebSocket(provider)
    call = await build_call(
        monkeypatch,
        provider,
        providers,
        agent_id,
        cache,
        socket,
        script=[(LIVE_PHRASE,)],
        # Only the end of the segment is held back, so the end of the
        # response reaches the runtime first.
        gated=(TTSStoppedFrame,),
    )
    try:
        await call.stt.deliver_transcription(CALLER_TEXT)
        assert await wait_until(
            lambda: any(
                isinstance(frame, TTSStoppedFrame) for frame in call.output.pending
            ),
            timeout=5.0,
        ), "the segment never stopped"

        # The end of the response has reached the runtime by now, but the
        # output of the segment has not.
        await settle()
        assert call.writer.assistant_messages() == [], (
            "the answer was stored before the segment was delivered"
        )

        # The output of the segment arrives now, and the answer with it.
        await call.output.release_all()
        assert await wait_until(
            lambda: len(call.writer.assistant_messages()) == 1, timeout=5.0
        ), "the answer was lost when its output arrived after the end of the response"
        assert call.writer.assistant_messages() == [LIVE_PHRASE]
        finals = call.connection.captions(role="assistant", final=True)
        assert len(finals) == 1
        assert finals[0]["text"] == LIVE_PHRASE
    finally:
        await stop_call(call)


@pytest.mark.anyio
async def test_a_natural_farewell_waits_for_the_audio_it_is_still_speaking(
    monkeypatch: Any, cache: RecordingCache, provider: str
):
    """A call that ends itself waits for the audio of its farewell.

    The farewell is part of the answer, so the call may only end once that
    answer has been spoken. Ending the call while the answer is still on its
    way would cut the caller off mid-sentence.
    """
    agent_id = uuid.uuid4()
    providers = providers_config(provider)
    socket = ProviderWebSocket(provider)

    async def answer_and_end(llm: Any, direction: FrameDirection) -> None:
        await llm.push_frame(LLMFullResponseStartFrame(), direction)
        await llm.push_frame(LLMTextFrame(text=LIVE_PHRASE), direction)
        await llm.push_frame(LLMFullResponseEndFrame(), direction)
        # The provider is given the answer once the call has it, so waiting
        # for that is what makes the farewell part of the answer and not an
        # utterance of its own.
        assert await wait_until(
            lambda: LIVE_PHRASE in provider_transcripts(socket), timeout=5.0
        ), "the answer never reached the provider"
        # The model ends the call while its answer is still being spoken.
        await llm.tools["end_call"](
            SimpleNamespace(arguments={"message": LIVE_PHRASE})
        )

    call = await build_call(
        monkeypatch,
        provider,
        providers,
        agent_id,
        cache,
        socket,
        script=[answer_and_end],
        # Only the end of the segment is held back, so the end of the
        # response reaches the runtime first.
        gated=(TTSStoppedFrame,),
    )
    try:
        await call.stt.deliver_transcription(CALLER_TEXT)
        assert await wait_until(
            lambda: any(
                isinstance(frame, TTSStoppedFrame) for frame in call.output.pending
            ),
            timeout=5.0,
        ), "the segment never stopped"

        # The answer is still on its way: nothing has been delivered and the
        # call has not ended.
        await settle()
        assert persisted_answers(call) == [], (
            "the answer was delivered before the segment was"
        )
        assert completed_messages(call) == [], (
            "the call ended before the answer was spoken"
        )
        assert call.connection.captions(role="assistant", final=True) == [], (
            "the answer was captioned before the segment was"
        )

        # The output of the segment arrives now: the answer is delivered and
        # the call ends.
        await call.output.release_all()
        assert await wait_until(
            lambda: len(completed_messages(call)) == 1, timeout=5.0
        ), "the answer was lost when its output arrived after the end of the response"
        assert len(completed_messages(call)) == 1, "the call hung up more than once"
        assert persisted_answers(call) == [LIVE_PHRASE]
        finals = call.connection.captions(role="assistant", final=True)
        assert len(finals) == 1
        assert finals[0]["text"] == LIVE_PHRASE
    finally:
        await stop_call(call)


@pytest.mark.anyio
async def test_an_interrupted_second_segment_is_never_recorded(
    monkeypatch: Any, cache: RecordingCache, provider: str
):
    """A turn the caller cut off is never stored as an answer."""
    agent_id = uuid.uuid4()
    providers = providers_config(provider)
    await prime_clip(provider, providers, agent_id, cache, CACHED_PHRASE)
    primed = set(cache.stored_keys)

    socket = ProviderWebSocket(provider)
    call = await build_call(
        monkeypatch,
        provider,
        providers,
        agent_id,
        cache,
        socket,
        script=[(f"{CACHED_PHRASE} {LIVE_PHRASE}",)],
    )
    try:
        await call.stt.deliver_transcription(CALLER_TEXT)
        assert await wait_until(
            lambda: len(call.output.pending) >= 2, timeout=5.0
        ), "the call never started speaking"

        # The first segment is delivered; the second one is not.
        while call.output.pending:
            frame = await call.output.release_next()
            if isinstance(frame, TTSStoppedFrame):
                break
        assert any(
            isinstance(frame, TTSAudioRawFrame) for frame in call.output.pending
        ), "the second segment was delivered together with the first one"
        await settle()
        assert call.writer.assistant_messages() == [], (
            "the answer was stored before the whole response was delivered"
        )

        # The caller interrupts while the second segment is still playing.
        await call.stt.inject_interruption()
        assert await wait_until(
            lambda: call.llm.user_turns >= 2, timeout=5.0
        ), "the caller never interrupted the bot"

        # Whatever is left of the response still reaches the transport, so the
        # runtime gets the chance to store the turn it must not store.
        await call.output.release_all()
        await settle()

        assert call.writer.assistant_messages() == [], (
            "an interrupted turn was stored as an answer"
        )
        assert call.connection.captions(role="assistant", final=True) == [], (
            "an interrupted turn was captioned as an answer"
        )
        # Only the clip of the priming is in the cache: the interrupted turn
        # is not a complete synthesis and must not be kept.
        assert cache.stored_keys == primed, (
            "an interrupted turn was kept in the cache"
        )
    finally:
        await stop_call(call)


@pytest.mark.anyio
async def test_a_two_segment_farewell_finishes_once_after_the_whole_output(
    monkeypatch: Any, cache: RecordingCache, provider: str
):
    """A farewell in two segments hangs up once, after everything was delivered."""
    agent_id = uuid.uuid4()
    providers = providers_config(provider)
    socket = ProviderWebSocket(provider)
    tool_called = asyncio.Event()

    async def farewell_step(llm: ScriptedLLM, direction: FrameDirection) -> None:
        await llm.speak([FAREWELL_PHRASE], direction)
        # The tool is called while the farewell is still being spoken.
        assert await wait_until(
            lambda: any(
                str(message.get("transcript", "")).strip()
                or str(message.get("text", "")).strip()
                for message in socket.sent
            ),
            timeout=5.0,
        ), "the farewell was never sent to the provider"
        await llm.tools["end_call"](
            SimpleNamespace(
                arguments={"message": FAREWELL_PHRASE},
                result_callback=lambda result: tool_called.set(),
            )
        )

    call = await build_call(
        monkeypatch,
        provider,
        providers,
        agent_id,
        cache,
        socket,
        script=[farewell_step],
    )
    try:
        await call.stt.deliver_transcription(CALLER_TEXT)

        assert await wait_until(
            tool_called.is_set, timeout=5.0
        ), "the call never asked to end"

        # The first segment stops: the call must not hang up yet.
        while call.output.pending:
            frame = await call.output.release_next()
            if isinstance(frame, TTSStoppedFrame):
                break
        # The second segment is still held at the transport.
        assert any(
            isinstance(frame, TTSAudioRawFrame) for frame in call.output.pending
        ), "the farewell was delivered as one segment"
        await settle()
        assert completed_messages(call) == [], (
            "the call hung up after the first segment"
        )

        # The rest of the output completes the farewell, and the call ends.
        await call.output.release_all()
        assert await wait_until(
            lambda: len(completed_messages(call)) == 1, timeout=5.0
        ), "the call never completed"

        assert len(completed_messages(call)) == 1, "the call hung up more than once"
        # The call ended after the last audio and the end of the response had
        # both been delivered, not on the safety timeout.
        events = call.output.events
        last_audio = max(index for index, event in enumerate(events) if event == "audio")
        last_end = max(
            index for index, event in enumerate(events) if event == "response-end"
        )
        assert last_end > last_audio, (
            f"the call completed before the output was delivered: {events}"
        )
    finally:
        await stop_call(call)


def completed_messages(call: Call) -> list[dict[str, Any]]:
    return [
        message
        for message in call.connection.messages
        if message.get("type") == "call.completed"
    ]


@pytest.mark.anyio
async def test_a_cached_forced_farewell_never_reaches_the_provider(
    monkeypatch: Any, cache: RecordingCache, provider: str
):
    """The exact forced farewell comes from the cache and is delivered once."""
    agent_id = uuid.uuid4()
    providers = providers_config(provider)
    await prime_clip(provider, providers, agent_id, cache, FAREWELL_PHRASE, forced=True)
    primed = set(cache.stored_keys)

    async def end_call_step(llm: ScriptedLLM, direction: FrameDirection) -> None:
        await llm.tools["end_call"](
            SimpleNamespace(
                arguments={"message": FAREWELL_PHRASE},
                result_callback=lambda result: None,
            )
        )

    socket = ProviderWebSocket(provider)
    call = await build_call(
        monkeypatch,
        provider,
        providers,
        agent_id,
        cache,
        socket,
        script=[end_call_step],
    )
    try:
        await call.stt.deliver_transcription(CALLER_TEXT)
        assert await wait_until(
            lambda: bool(call.output.pending), timeout=5.0
        ), "the farewell was never spoken"

        await call.output.release_all()
        assert await wait_until(
            lambda: len(completed_messages(call)) == 1, timeout=5.0
        ), "the call never completed"

        synthesis = [
            message
            for message in socket.sent
            if any(key in message for key in ("transcript", "text", "close_context"))
        ]
        assert synthesis == [], (
            f"a cached farewell still went to the provider: {synthesis}"
        )
        # The clip came from the cache, so nothing new was stored.
        assert cache.stored_keys == primed, (
            "the cached farewell was synthesized and stored again"
        )
        assert call.writer.assistant_messages() == [FAREWELL_PHRASE]
        finals = call.connection.captions(role="assistant", final=True)
        assert len(finals) == 1
        assert finals[0]["text"] == FAREWELL_PHRASE
    finally:
        await stop_call(call)


@pytest.mark.anyio
async def test_an_interruption_after_the_last_audio_keeps_the_farewell(
    monkeypatch: Any, cache: RecordingCache, provider: str
):
    """An uninterruptible farewell survives an interruption after its last audio."""
    agent_id = uuid.uuid4()
    providers = providers_config(provider)
    await prime_clip(provider, providers, agent_id, cache, FAREWELL_PHRASE, forced=True)

    async def end_call_step(llm: ScriptedLLM, direction: FrameDirection) -> None:
        await llm.tools["end_call"](
            SimpleNamespace(
                arguments={"message": FAREWELL_PHRASE},
                result_callback=lambda result: None,
            )
        )

    socket = ProviderWebSocket(provider)
    call = await build_call(
        monkeypatch,
        provider,
        providers,
        agent_id,
        cache,
        socket,
        script=[end_call_step],
    )
    try:
        await call.stt.deliver_transcription(CALLER_TEXT)
        assert await wait_until(
            lambda: any(
                isinstance(frame, TTSAudioRawFrame) for frame in call.output.pending
            ),
            timeout=5.0,
        ), "the farewell was never spoken"

        # Let the audio out, but hold the stop frame back.
        while call.output.pending and not isinstance(
            call.output.pending[0], TTSStoppedFrame
        ):
            await call.output.release_next()
        assert any(
            isinstance(frame, TTSStoppedFrame) for frame in call.output.pending
        ), "the farewell never reached the transport"

        # The caller interrupts after the last audio chunk.
        await call.stt.inject_interruption()
        assert await wait_until(
            lambda: call.llm.user_turns >= 2, timeout=5.0
        ), "the caller never interrupted the bot"

        # The farewell was not interrupted: its stop still arrives.
        await call.output.release_all()
        await settle()

        assert call.writer.assistant_messages() == [FAREWELL_PHRASE], (
            "the uninterruptible farewell was lost after an interruption"
        )
    finally:
        await stop_call(call)


@pytest.mark.anyio
async def test_overlapping_turns_keep_their_own_generation(
    monkeypatch: Any, cache: RecordingCache, provider: str
):
    """A queued context belongs to the turn that asked for it."""
    agent_id = uuid.uuid4()
    providers = providers_config(provider)
    await prime_clip(provider, providers, agent_id, cache, CACHED_PHRASE)

    socket = ProviderWebSocket(provider)
    call = await build_call(
        monkeypatch,
        provider,
        providers,
        agent_id,
        cache,
        socket,
        script=[(CACHED_PHRASE,), ("A second answer.",)],
    )
    try:
        await call.stt.deliver_transcription(CALLER_TEXT)
        assert await wait_until(
            lambda: len(call.output.pending) >= 1, timeout=5.0
        ), "the call never started speaking"

        # A second turn starts while the first one is still queued.
        await call.runtime.workflow_processor.push_frame(
            LLMContextFrame(LLMContext(messages=[{"role": "user", "content": "And more?"}])),
            FrameDirection.DOWNSTREAM,
        )
        # Both turns are queued at the transport before anything is released.
        assert await wait_until(
            lambda: sum(
                1
                for frame in call.output.pending
                if isinstance(frame, LLMFullResponseEndFrame)
            )
            >= 2,
            timeout=5.0,
        ), "the second turn never reached the transport"

        await call.output.release_all()
        assert await wait_until(
            lambda: "A second answer." in call.writer.assistant_messages(), timeout=5.0
        ), f"the second answer was lost: {call.writer.assistant_messages()}"

        delivered = call.writer.assistant_messages()
        assert CACHED_PHRASE in delivered, (
            f"the first answer was lost: {delivered}"
        )
    finally:
        await stop_call(call)
