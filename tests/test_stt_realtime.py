from __future__ import annotations

import base64
import json
import urllib.parse

import pytest
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transcriptions.language import Language

from bot.config import Settings
from bot.language import SttLanguageRouter, select_stt_language_router
from bot.services import NEMOTRON_MODEL, WHISPER_MODEL, build_stt
from bot.stt_realtime import LocalMlxRealtimeSTTService, websocket_url


class StubWebSocket:
    """A minimal WebSocket stub that records sent JSON messages."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))


# ---------------------------------------------------------------------------
# 1. websocket_url
# ---------------------------------------------------------------------------


class TestWebsocketUrl:
    def test_http_scheme_becomes_ws(self):
        url = websocket_url("http://localhost:8020/v1", "test-model")
        assert url.startswith("ws://localhost:8020/v1/realtime?model=")

    def test_https_scheme_becomes_wss(self):
        url = websocket_url("https://localhost:8020/v1", "test-model")
        assert url.startswith("wss://localhost:8020/v1/realtime?model=")

    def test_existing_realtime_suffix_is_not_doubled(self):
        url = websocket_url("http://localhost:8020/v1/realtime", "test-model")
        parsed = urllib.parse.urlsplit(url)
        assert parsed.path == "/v1/realtime"

    def test_trailing_slash_is_handled(self):
        url_with_slash = websocket_url("http://localhost:8020/v1/", "test-model")
        url_realtime_slash = websocket_url("http://localhost:8020/v1/realtime/", "test-model")
        assert urllib.parse.urlsplit(url_with_slash).path == "/v1/realtime"
        assert urllib.parse.urlsplit(url_realtime_slash).path == "/v1/realtime"

    def test_model_id_with_slash_is_percent_encoded_once_and_decodes_back(self):
        model_id = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
        url = websocket_url("http://localhost:8020/v1", model_id)
        parsed = urllib.parse.urlsplit(url)
        query_dict = urllib.parse.parse_qs(parsed.query)
        assert query_dict["model"] == [model_id]
        # Percent-encoded once: %2F appears, not %252F
        assert "mlx-community%2Fnemotron" in parsed.query
        assert "%25" not in parsed.query


# ---------------------------------------------------------------------------
# 2. Factory (build_stt)
# ---------------------------------------------------------------------------


class TestFactory:
    def test_builds_local_mlx_realtime_stt_service_carrying_model(self):
        settings = Settings(
            _env_file=None,
            stt_provider="mlx-audio-realtime",
            stt_language="auto",
        )
        service = build_stt(settings)
        assert isinstance(service, LocalMlxRealtimeSTTService)
        parsed = urllib.parse.urlsplit(service._ws_url)
        assert urllib.parse.parse_qs(parsed.query)["model"] == [settings.stt_model]

    def test_whisper_checkpoint_swaps_to_nemotron(self):
        settings = Settings(
            _env_file=None,
            stt_provider="mlx-audio-realtime",
            stt_model=WHISPER_MODEL,
            stt_language="auto",
        )
        service = build_stt(settings)
        assert settings.stt_model == NEMOTRON_MODEL
        assert service._settings.model == NEMOTRON_MODEL
        parsed = urllib.parse.urlsplit(service._ws_url)
        assert urllib.parse.parse_qs(parsed.query)["model"] == [NEMOTRON_MODEL]

    def test_pinned_language_raises_value_error_naming_mlx_audio(self):
        settings = Settings(
            _env_file=None,
            stt_provider="mlx-audio-realtime",
            stt_language="hi",
        )
        with pytest.raises(ValueError) as excinfo:
            build_stt(settings)
        assert "mlx-audio" in str(excinfo.value)

    def test_auto_language_builds(self):
        settings = Settings(
            _env_file=None,
            stt_provider="mlx-audio-realtime",
            stt_language="auto",
        )
        service = build_stt(settings)
        assert isinstance(service, LocalMlxRealtimeSTTService)


# ---------------------------------------------------------------------------
# 3. Router
# ---------------------------------------------------------------------------


class TestRouter:
    def test_select_stt_language_router_returns_router(self):
        settings = Settings(_env_file=None, stt_provider="mlx-audio-realtime")
        router = select_stt_language_router(settings)
        assert isinstance(router, SttLanguageRouter)

    def test_delta_for_raises_value_error_mentioning_missing_language_switch(self):
        settings = Settings(_env_file=None, stt_provider="mlx-audio-realtime")
        router = select_stt_language_router(settings)
        for lang in (None, Language.HI):
            with pytest.raises(ValueError) as excinfo:
                router.delta_for(lang)
            assert "language switch" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# 4. Service wire behavior (offline)
# ---------------------------------------------------------------------------


class TestServiceWireBehavior:
    @pytest.fixture
    def service(self) -> LocalMlxRealtimeSTTService:
        srv = LocalMlxRealtimeSTTService(
            model=NEMOTRON_MODEL,
            base_url="http://localhost:8020/v1",
        )
        srv._websocket = StubWebSocket()
        return srv

    async def test_send_session_update_at_16k_emits_session_update_and_sets_flag(
        self, service: LocalMlxRealtimeSTTService
    ):
        service._sample_rate = 16000
        await service._send_session_update()

        stub: StubWebSocket = service._websocket
        assert len(stub.sent) == 1
        msg = stub.sent[0]
        assert msg["type"] == "session.update"
        session = msg["session"]
        assert session["type"] == "transcription"
        assert session["audio"]["input"]["format"] == {
            "type": "audio/pcm",
            "rate": 16000,
        }
        assert session["audio"]["input"]["transcription"]["model"] == NEMOTRON_MODEL
        assert session["audio"]["input"]["turn_detection"] is None
        assert service._rate_declared is True

    async def test_send_session_update_at_rate_zero_sends_nothing(
        self, service: LocalMlxRealtimeSTTService
    ):
        service._sample_rate = 0
        await service._send_session_update()

        stub: StubWebSocket = service._websocket
        assert len(stub.sent) == 0
        assert service._rate_declared is False

    async def test_start_sends_the_session_update_at_the_pipeline_rate(
        self, service: LocalMlxRealtimeSTTService
    ):
        # `session.created` arrives before the StartFrame, so the rate is
        # only known here; this is the one path that configures the
        # production session.
        service._sample_rate = 16000
        await service.start(
            StartFrame(audio_in_sample_rate=16000, audio_out_sample_rate=24000)
        )

        stub: StubWebSocket = service._websocket
        assert len(stub.sent) == 1
        msg = stub.sent[0]
        assert msg["type"] == "session.update"
        assert msg["session"]["audio"]["input"]["format"] == {
            "type": "audio/pcm",
            "rate": 16000,
        }
        assert service._rate_declared is True

    async def test_run_stt_buffers_preroll_then_flushes_on_speech(
        self, service: LocalMlxRealtimeSTTService
    ):
        service._sample_rate = 16000
        service._user_speaking = False

        chunk_a = b"chunk_a_12345678"
        chunk_b = b"chunk_b_87654321"

        async for _ in service.run_stt(chunk_a):
            pass
        async for _ in service.run_stt(chunk_b):
            pass

        stub: StubWebSocket = service._websocket
        assert len(stub.sent) == 0
        assert len(service._preroll) == 2

        service._user_speaking = True
        chunk_c = b"chunk_c_active_now"
        async for _ in service.run_stt(chunk_c):
            pass

        # Should have sent chunk_a, chunk_b (preroll), then chunk_c
        appended = [
            base64.b64decode(m["audio"])
            for m in stub.sent
            if m["type"] == "input_audio_buffer.append"
        ]
        assert len(appended) == 3
        assert appended[0] == chunk_a
        assert appended[1] == chunk_b
        assert appended[2] == chunk_c
        assert b"".join(appended) == chunk_a + chunk_b + chunk_c
        assert len(service._preroll) == 0
        assert service._preroll_bytes == 0

    async def test_preroll_cap_drops_oldest_chunks(
        self, service: LocalMlxRealtimeSTTService
    ):
        service._sample_rate = 16000
        service._user_speaking = False

        # 20ms at 16kHz mono 16-bit PCM = 320 samples = 640 bytes.
        # Push 3 seconds = 150 chunks.
        chunks = [f"{i:04d}".encode("ascii") * 160 for i in range(150)]
        for chunk in chunks:
            async for _ in service.run_stt(chunk):
                pass

        stub: StubWebSocket = service._websocket
        assert len(stub.sent) == 0

        # Speak and push one new chunk
        service._user_speaking = True
        speaking_chunk = b"ACTIVE_CHUNK" * 50
        async for _ in service.run_stt(speaking_chunk):
            pass

        appended = [
            base64.b64decode(m["audio"])
            for m in stub.sent
            if m["type"] == "input_audio_buffer.append"
        ]
        assert appended[-1] == speaking_chunk

        preroll_sent = appended[:-1]
        decoded_preroll = b"".join(preroll_sent)
        # Cap is max(1, int(16000 * 1.0)) * 2 = 32000 bytes (1 second of audio)
        assert len(decoded_preroll) <= 32000
        # Must end with the most recent audio before speaking (chunk 149)
        assert decoded_preroll.endswith(chunks[-1])
        # Oldest audio (chunk 0) must have been dropped
        assert not decoded_preroll.startswith(chunks[0])

    async def test_process_frame_appends_trailing_silence_before_commit(
        self, service: LocalMlxRealtimeSTTService
    ):
        service._sample_rate = 16000
        service._server_vad_enabled = False

        await service.process_frame(
            VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM
        )

        stub: StubWebSocket = service._websocket
        assert len(stub.sent) == 2
        first, second = stub.sent[0], stub.sent[1]
        assert first["type"] == "input_audio_buffer.append"
        silence_bytes = base64.b64decode(first["audio"])
        # 0.5s at 16kHz 16-bit mono = 8000 samples = 16000 zero bytes
        assert len(silence_bytes) == 16000
        assert silence_bytes == bytes(16000)

        assert second["type"] == "input_audio_buffer.commit"

    async def test_push_frame_marks_completed_transcripts_uninterruptible(
        self, service: LocalMlxRealtimeSTTService
    ):
        interim = InterimTranscriptionFrame("Oh", "user_1", "2026-10-22T00:00:00Z")
        await service.push_frame(interim)
        assert interim.interruptible is True

        frame = TranscriptionFrame("Oh really?", "user_1", "2026-10-22T00:00:00Z")
        assert not getattr(frame, "finalized", False)

        await service.push_frame(frame)
        assert frame.finalized is True
        assert frame.interruptible is False


# ---------------------------------------------------------------------------
# 5. A completed transcript survives the turn-start reset
# ---------------------------------------------------------------------------


class TestTheCompletedTranscriptSurvivesTheReset:
    """The commit flush puts the delta and the completed text in one tick.

    The server answers ``input_audio_buffer.commit`` with a delta and the
    completed transcription back to back. The delta starts the user turn,
    and the aggregator's turn-start reset drops the frames still queued
    behind it — a completed transcription is marked uninterruptible by
    ``push_frame``, so it passes the reset and the turn reaches the LLM.
    """

    async def test_a_final_right_behind_its_delta_reaches_the_llm(self):
        import asyncio
        import contextlib
        import time

        from pipecat.audio.turn.base_turn_analyzer import (
            BaseTurnAnalyzer,
            BaseTurnParams,
            EndOfTurnState,
        )
        from pipecat.frames.frames import (
            LLMContextFrame,
            StartFrame,
            VADUserStartedSpeakingFrame,
        )
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMContextAggregatorPair,
            LLMUserAggregatorParams,
        )
        from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
        from pipecat.turns.user_start.min_words_user_turn_start_strategy import (
            MinWordsUserTurnStartStrategy,
        )
        from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
        from pipecat.turns.user_turn_strategies import UserTurnStrategies
        from pipecat.utils.time import time_now_iso8601
        from pipecat.workers.runner import WorkerRunner

        class StubTurnAnalyzer(BaseTurnAnalyzer):
            """Says COMPLETE as soon as the VAD says the user stopped."""

            @property
            def speech_triggered(self) -> bool:
                return True

            @property
            def params(self) -> BaseTurnParams:
                return BaseTurnParams()

            def append_audio(self, buffer: bytes, is_speech: bool) -> EndOfTurnState:
                return EndOfTurnState.INCOMPLETE

            async def analyze_end_of_turn(self):
                return EndOfTurnState.COMPLETE, None

            def clear(self) -> None:
                pass

        class Source(FrameProcessor):
            """Replays a segment's tail: the VAD stop, then the commit flush."""

            async def process_frame(self, frame, direction: FrameDirection):
                await super().process_frame(frame, direction)
                if isinstance(frame, StartFrame):
                    self.create_task(self._script())
                await self.push_frame(frame, direction)

            async def _script(self):
                await asyncio.sleep(0.3)
                await self.push_frame(
                    VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=time.time())
                )
                await asyncio.sleep(0.02)
                await self.push_frame(
                    VADUserStoppedSpeakingFrame(stop_secs=0.2, timestamp=time.time())
                )
                await asyncio.sleep(0.02)
                # One tick: the delta, then the completed text it previewed.
                await self.push_frame(
                    InterimTranscriptionFrame("Oh", "user", time_now_iso8601())
                )
                completed = TranscriptionFrame(
                    "Oh really?", "user", time_now_iso8601()
                )
                completed.finalized = True  # what push_frame sets
                completed.interruptible = False  # ditto
                await self.push_frame(completed)

        class Capture(FrameProcessor):
            """Stands in for the LLM service."""

            def __init__(self) -> None:
                super().__init__()
                self.contexts: list = []

            async def process_frame(self, frame, direction: FrameDirection):
                await super().process_frame(frame, direction)
                if isinstance(frame, LLMContextFrame):
                    self.contexts.append(frame.context)
                await self.push_frame(frame, direction)

        context = LLMContext()
        user_agg, _ = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=UserTurnStrategies(
                    start=[MinWordsUserTurnStartStrategy(min_words=2)],
                    stop=[
                        TurnAnalyzerUserTurnStopStrategy(
                            turn_analyzer=StubTurnAnalyzer()
                        )
                    ],
                ),
            ),
        )
        capture = Capture()
        worker = PipelineWorker(
            Pipeline([Source(), user_agg, capture]), params=PipelineParams()
        )
        runner = WorkerRunner()
        await runner.add_workers(worker)
        run_task = asyncio.create_task(runner.run())

        deadline = time.monotonic() + 5.0
        while not capture.contexts and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

        # Taken down rather than ended, as in test_context: a swallowed
        # frame would swallow the EndFrame too.
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task

        assert capture.contexts, "the LLM was never asked to answer"
        assert [
            m["content"] for m in context.get_messages() if m.get("role") == "user"
        ] == ["Oh really?"]
