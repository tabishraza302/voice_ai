from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from httpx import ASGITransport, AsyncClient
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from datetime import datetime, timedelta, timezone
from sqlalchemy import text, update

from app.db import Base, Session, engine
from app.main import app
from app.sessions import CallSession, load_session_snapshot, set_session_status
from app.voice import CallRuntime

CALLER_TEXT = "My account number ends in 42."
ASSISTANT_TEXT = "I can help with the account ending in 42."
FAREWELL_TEXT = "Thank you for calling. Goodbye!"
EXTENDED_FAREWELL_TEXT = "Your confirmation number is 42. Thank you for calling. Goodbye!"
ORDINARY_TEXT = "I can help with anything else."
BACKCHANNEL_TEXT = "okay"
COMMAND_TEXT = "okay wait a second"
COMMAND_REPLY_TEXT = "I will wait for you."
OUTSIDE_ACK_REPLY_TEXT = "I am listening."


def verified_environment() -> dict[str, str]:
    return {
        "DEEPGRAM_API_KEY": "deepgram-test-secret",
        "AZURE_SPEECH_API_KEY": "azure-test-secret",
        "AZURE_SPEECH_REGION": "eastus",
        "OPENAI_API_KEY": "openai-test-secret",
        "VERIFIED_OPENAI_MODELS": "gpt-4o-mini,gpt-4.1-mini",
        "CARTESIA_API_KEY": "cartesia-test-secret",
        "ELEVENLABS_API_KEY": "elevenlabs-test-secret",
        "VERIFIED_TTS_VOICE_CATALOG": json.dumps(
            {
                "cartesia": {"sonic-3.6": {"cartesia-en": ["en"], "cartesia-hi": ["hi"]}},
                "elevenlabs": {
                    "eleven_flash_v2_5": {"eleven-en": ["en"], "eleven-hi": ["hi"]}
                },
            }
        ),
    }

class RunningVoicePipeline:
    """Runs a real CallRuntime pipeline with deterministic external boundaries."""

    def __init__(
        self,
        environment: Mapping[str, str],
        *,
        scenario: str,
        existing_speech: bool = False,
        language: str = "en",
        stt_provider: str = "deepgram",
        azure_stt_english_locale: str = "en-IN",
        backchannel_text: str = BACKCHANNEL_TEXT,
        command_text: str = COMMAND_TEXT,
    ) -> None:
        if scenario not in {
            "transcription",
            "farewell",
            "late-transcription",
            "duplicate-farewell",
            "extended-farewell",
            "natural-farewell-delayed",
            "prior-speech-starts-late",
            "backchannel-during-playback",
            "turn-handling",
        }:
            raise ValueError(f"Unsupported pipeline test scenario: {scenario}")
        self.environment = environment
        self.scenario = scenario
        self.existing_speech = existing_speech
        self.language = language
        self.stt_provider = stt_provider
        self.azure_stt_english_locale = azure_stt_english_locale
        self.backchannel_text = backchannel_text
        self.command_text = command_text
        self.events: list[str] = []
        self.background_tasks: list[asyncio.Task[Any]] = []
        self.client: AsyncClient | None = None
        self.session_id: uuid.UUID | None = None
        self.snapshot: Mapping[str, Any] | None = None
        self.runtime: CallRuntime | None = None
        self.connection: Any = None
        self.stt: Any = None
        self.tts: Any = None
        self.output: Any = None
        self.tool_result_received = asyncio.Event()
        self.model_turn_requests: list[str] = []
        self.model_context_turns: list[list[str]] = []

    def _track(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self.background_tasks.append(task)
        return task

    async def start(self, monkeypatch: Any) -> None:
        await self._create_session()

        harness = self

        class PassThroughProcessor(FrameProcessor):
            async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
                await super().process_frame(frame, direction)
                await self.push_frame(frame, direction)

        class RecordingOutput(PassThroughProcessor):
            def __init__(self) -> None:
                super().__init__()
                self.recorded_frames: list[Frame] = []
                self.audio_completed = asyncio.Event()
                self.ordinary_audio_started = asyncio.Event()
                self.ordinary_audio_completed = asyncio.Event()
                self.assistant_audio_completed = asyncio.Event()
                self.assistant_playback_started = asyncio.Event()
                self.assistant_playback_completed = asyncio.Event()
                self.user_started_speaking = asyncio.Event()

            async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
                await FrameProcessor.process_frame(self, frame, direction)
                if direction == FrameDirection.DOWNSTREAM:
                    self.recorded_frames.append(frame)
                    if isinstance(frame, TTSStartedFrame) and frame.context_id == "ordinary-audio":
                        harness.events.append("ordinary-audio-start")
                        self.ordinary_audio_started.set()
                    elif isinstance(frame, TTSStartedFrame) and frame.context_id == "farewell-audio":
                        harness.events.append("farewell-audio-start")
                    elif isinstance(frame, TTSStartedFrame) and frame.context_id == "assistant-reply":
                        harness.events.append("assistant-audio-start")
                    elif isinstance(frame, TTSStartedFrame) and frame.context_id == "assistant-playback":
                        harness.events.append("assistant-playback-start")
                        self.assistant_playback_started.set()
                    elif isinstance(frame, TTSAudioRawFrame):
                        harness.events.append(f"{frame.context_id}-audio")
                    elif isinstance(frame, TTSStoppedFrame):
                        if frame.context_id == "ordinary-audio":
                            harness.events.append("ordinary-audio-complete")
                            self.ordinary_audio_completed.set()
                        elif frame.context_id == "farewell-audio":
                            harness.events.append("farewell-audio-complete")
                            self.audio_completed.set()
                        elif frame.context_id == "assistant-reply":
                            harness.events.append("assistant-audio-complete")
                            self.assistant_audio_completed.set()
                        elif frame.context_id == "assistant-playback":
                            harness.events.append("assistant-playback-complete")
                            self.assistant_playback_completed.set()
                    elif isinstance(frame, UserStartedSpeakingFrame):
                        self.user_started_speaking.set()
                await self.push_frame(frame, direction)
        class FakeInput(PassThroughProcessor):
            async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
                await super().process_frame(frame, direction)
                if (
                    harness.scenario in {"late-transcription", "transcription"}
                    and direction == FrameDirection.DOWNSTREAM
                    and isinstance(frame, StartFrame)
                ):
                    await self.push_frame(VADUserStartedSpeakingFrame(), direction)
                    await self.push_frame(VADUserStoppedSpeakingFrame(), direction)

        class FakeTransport:
            def __init__(self, connection: Any, params: Any) -> None:
                self._input = FakeInput()
                self._output = RecordingOutput()
                harness.output = self._output

            def input(self) -> FrameProcessor:
                return self._input

            def output(self) -> FrameProcessor:
                return self._output

        class FakeSTT(PassThroughProcessor):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__()
                self._sent_farewell_transcript = False
                self._sent_late_transcript = False
                self.late_transcription_sent = asyncio.Event()
                self.vad_started = asyncio.Event()
                self.vad_stopped = asyncio.Event()
                harness.stt = self

            async def deliver_vad_start(self) -> None:
                await self.push_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)

            async def deliver_vad_stop(self, stop_secs: float = 0.0) -> None:
                await self.push_frame(
                    VADUserStoppedSpeakingFrame(stop_secs=stop_secs),
                    FrameDirection.DOWNSTREAM,
                )

            async def deliver_transcription(
                self,
                text: str,
                *,
                is_final: bool = True,
                speech_final: bool = True,
                offset: int | None = None,
                duration: int | None = None,
            ) -> None:
                if harness.stt_provider == "azure":
                    if is_final or speech_final:
                        result_inner = SimpleNamespace(
                            reason="RecognizedSpeech",
                            text=text,
                        )
                        if offset is not None:
                            result_inner.offset = offset
                        if duration is not None:
                            result_inner.duration = duration
                        await self.push_frame(
                            TranscriptionFrame(
                                text=text,
                                user_id="caller",
                                timestamp="0",
                                finalized=True,
                                result=SimpleNamespace(
                                    result=result_inner,
                                    offset=offset,
                                    duration=duration,
                                ),
                            ),
                            FrameDirection.DOWNSTREAM,
                        )
                    else:
                        await self.push_frame(
                            InterimTranscriptionFrame(
                                text=text,
                                user_id="caller",
                                timestamp="0",
                            ),
                            FrameDirection.DOWNSTREAM,
                        )
                else:
                    await self.push_frame(
                        TranscriptionFrame(
                            text=text,
                            user_id="caller",
                            timestamp="0",
                            result=SimpleNamespace(
                                is_final=is_final,
                                speech_final=speech_final,
                                from_finalize=False,
                            ),
                        ),
                        FrameDirection.DOWNSTREAM,
                    )

            async def deliver_audio(
                self,
                duration_ms: int,
                *,
                sample_rate: int = 16_000,
            ) -> None:
                """Forward caller audio frames downstream without a transcript.

                Real STT services pass input audio through by default. This lets
                turn-handling tests advance the audio timeline (as Azure sees
                it) when no interim transcription arrives.
                """
                remaining = max(0, duration_ms)
                while remaining > 0:
                    chunk_ms = min(20, remaining)  # 20 ms, matching WebRTC
                    chunk_samples = max(1, sample_rate * chunk_ms // 1000)
                    await self.push_frame(
                        InputAudioRawFrame(
                            audio=b"\x00\x00" * chunk_samples,
                            sample_rate=sample_rate,
                            num_channels=1,
                        ),
                        FrameDirection.DOWNSTREAM,
                    )
                    remaining -= chunk_ms

            async def _send_late_transcription(self) -> None:
                await asyncio.sleep(0.6)
                if harness.stt_provider == "azure":
                    await self.push_frame(
                        InterimTranscriptionFrame(
                            text="My account number",
                            user_id="caller",
                            timestamp="0",
                        ),
                        FrameDirection.DOWNSTREAM,
                    )
                    await asyncio.sleep(0.05)
                    await self.push_frame(
                        TranscriptionFrame(
                            text=CALLER_TEXT,
                            user_id="caller",
                            timestamp="0",
                            finalized=True,
                            result=SimpleNamespace(
                                result=SimpleNamespace(
                                    reason="RecognizedSpeech",
                                    text=CALLER_TEXT,
                                )
                            ),
                        ),
                        FrameDirection.DOWNSTREAM,
                    )
                else:
                    await self.push_frame(
                        TranscriptionFrame(
                            text=CALLER_TEXT,
                            user_id="caller",
                            timestamp="0",
                            result=SimpleNamespace(
                                is_final=True,
                                speech_final=False,
                                from_finalize=False,
                            ),
                        ),
                        FrameDirection.DOWNSTREAM,
                    )
                self.late_transcription_sent.set()

            async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
                await FrameProcessor.process_frame(self, frame, direction)
                await self.push_frame(frame, direction)
                if direction != FrameDirection.DOWNSTREAM:
                    return
                if isinstance(frame, VADUserStartedSpeakingFrame):
                    self.vad_started.set()
                elif isinstance(frame, VADUserStoppedSpeakingFrame):
                    self.vad_stopped.set()
                    if harness.scenario == "late-transcription" and not self._sent_late_transcript:
                        self._sent_late_transcript = True
                        harness._track(self._send_late_transcription())
                elif (
                    harness.scenario in {"farewell", "duplicate-farewell", "extended-farewell", "natural-farewell-delayed", "prior-speech-starts-late"}
                    and isinstance(frame, StartFrame)
                    and not self._sent_farewell_transcript
                ):
                    self._sent_farewell_transcript = True
                    if harness.stt_provider == "azure":
                        await self.push_frame(
                            TranscriptionFrame(
                                text="Goodbye.",
                                user_id="caller",
                                timestamp="0",
                                finalized=True,
                                result=SimpleNamespace(
                                    result=SimpleNamespace(
                                        reason="RecognizedSpeech",
                                        text="Goodbye.",
                                    )
                                ),
                            ),
                            direction,
                        )
                    else:
                        await self.push_frame(
                            TranscriptionFrame(
                                text="Goodbye.",
                                user_id="caller",
                                timestamp="0",
                                result=SimpleNamespace(
                                    is_final=True,
                                    speech_final=True,
                                    from_finalize=False,
                                ),
                            ),
                            direction,
                        )
                elif (
                    harness.scenario == "backchannel-during-playback"
                    and isinstance(frame, StartFrame)
                    and not self._sent_farewell_transcript
                ):
                    self._sent_farewell_transcript = True
                    await self.deliver_transcription(CALLER_TEXT, speech_final=True)

        class FakeOpenAI(PassThroughProcessor):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__()
                self._end_call = None

            def register_function(self, name: str, callback: Any) -> None:
                if name == "end_call":
                    self._end_call = callback

            async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
                await FrameProcessor.process_frame(self, frame, direction)
                if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMContextFrame):
                    user_messages = [
                        message.get("content")
                        for message in frame.context.messages
                        if message.get("role") == "user"
                    ]
                    if user_messages:
                        harness.model_turn_requests.append(user_messages[-1])
                        harness.model_context_turns.append(list(user_messages))
                    if harness.scenario in {"late-transcription", "transcription"} and any(msg in user_messages for msg in (CALLER_TEXT, "मेरा नाम आरव है।")):
                        reply = "नमस्ते आरव!" if "मेरा नाम आरव है।" in user_messages else ASSISTANT_TEXT
                        await self.push_frame(LLMFullResponseStartFrame(), direction)
                        await self.push_frame(LLMTextFrame(text=reply), direction)
                        await self.push_frame(LLMFullResponseEndFrame(), direction)
                        return
                    if harness.scenario == "turn-handling" and user_messages:
                        await self.push_frame(LLMFullResponseStartFrame(), direction)
                        await self.push_frame(LLMTextFrame(text=ASSISTANT_TEXT), direction)
                        await self.push_frame(LLMFullResponseEndFrame(), direction)
                        return
                    if harness.scenario == "farewell" and "Goodbye." in user_messages:
                        if harness.existing_speech:
                            await self.push_frame(LLMFullResponseStartFrame(), direction)
                            await self.push_frame(
                                LLMTextFrame(text="I can help with anything else."),
                                direction,
                            )
                            await self.push_frame(LLMFullResponseEndFrame(), direction)
                            await asyncio.wait_for(
                                harness.output.ordinary_audio_started.wait(),
                                timeout=3,
                            )
                        await self._end_call(
                            SimpleNamespace(
                                arguments={"message": FAREWELL_TEXT},
                                result_callback=lambda result: harness.tool_result_received.set(),
                            )
                        )
                        return
                    if harness.scenario == "duplicate-farewell" and "Goodbye." in user_messages:
                        await self.push_frame(LLMFullResponseStartFrame(), direction)
                        await self.push_frame(LLMTextFrame(text=FAREWELL_TEXT), direction)
                        await self.push_frame(LLMFullResponseEndFrame(), direction)
                        await asyncio.wait_for(
                            harness.output.assistant_audio_completed.wait(),
                            timeout=3,
                        )
                        await asyncio.wait_for(
                            harness.connection.farewell_caption.wait(),
                            timeout=3,
                        )
                        await self._end_call(
                            SimpleNamespace(
                                arguments={"message": FAREWELL_TEXT},
                                result_callback=lambda result: harness.tool_result_received.set(),
                            )
                        )
                        return
                    if harness.scenario == "extended-farewell" and "Goodbye." in user_messages:
                        await self.push_frame(LLMFullResponseStartFrame(), direction)
                        await self.push_frame(LLMTextFrame(text=FAREWELL_TEXT), direction)
                        await self.push_frame(LLMFullResponseEndFrame(), direction)
                        await asyncio.wait_for(
                            harness.output.assistant_audio_completed.wait(),
                            timeout=3,
                        )
                        await asyncio.wait_for(
                            harness.connection.farewell_caption.wait(),
                            timeout=3,
                        )
                        await self._end_call(
                            SimpleNamespace(
                                arguments={"message": EXTENDED_FAREWELL_TEXT},
                                result_callback=lambda result: harness.tool_result_received.set(),
                            )
                        )
                        return
                    if harness.scenario == "natural-farewell-delayed" and "Goodbye." in user_messages:
                        await self.push_frame(LLMFullResponseStartFrame(), direction)
                        await self.push_frame(LLMTextFrame(text=FAREWELL_TEXT), direction)
                        await self.push_frame(LLMFullResponseEndFrame(), direction)
                        await asyncio.wait_for(
                            harness.connection.assistant_partial_caption.wait(),
                            timeout=3,
                        )
                        await asyncio.wait_for(
                            harness.tts.normal_synthesis_requested.wait(),
                            timeout=3,
                        )
                        await self._end_call(
                            SimpleNamespace(
                                arguments={"message": FAREWELL_TEXT},
                                result_callback=lambda result: harness.tool_result_received.set(),
                            )
                        )
                        return
                    if harness.scenario == "prior-speech-starts-late" and "Goodbye." in user_messages:
                        await self.push_frame(LLMFullResponseStartFrame(), direction)
                        await self.push_frame(LLMTextFrame(text=ORDINARY_TEXT), direction)
                        await self.push_frame(LLMFullResponseEndFrame(), direction)
                        await asyncio.wait_for(
                            harness.connection.assistant_partial_caption.wait(),
                            timeout=3,
                        )
                        await asyncio.wait_for(
                            harness.tts.normal_synthesis_requested.wait(),
                            timeout=3,
                        )
                        await self._end_call(
                            SimpleNamespace(
                                arguments={"message": FAREWELL_TEXT},
                                result_callback=lambda result: harness.tool_result_received.set(),
                            )
                        )
                    if harness.scenario == "backchannel-during-playback":
                        latest_user = user_messages[-1] if user_messages else ""
                        if latest_user == CALLER_TEXT:
                            await self.push_frame(LLMFullResponseStartFrame(), direction)
                            await self.push_frame(LLMTextFrame(text=ASSISTANT_TEXT), direction)
                            await self.push_frame(LLMFullResponseEndFrame(), direction)
                            return
                        if latest_user == harness.command_text:
                            await self.push_frame(LLMFullResponseStartFrame(), direction)
                            await self.push_frame(LLMTextFrame(text=COMMAND_REPLY_TEXT), direction)
                            await self.push_frame(LLMFullResponseEndFrame(), direction)
                            return
                        if latest_user == harness.backchannel_text:
                            await self.push_frame(LLMFullResponseStartFrame(), direction)
                            await self.push_frame(LLMTextFrame(text=OUTSIDE_ACK_REPLY_TEXT), direction)
                            await self.push_frame(LLMFullResponseEndFrame(), direction)
                            return
                await self.push_frame(frame, direction)

        class FakeCartesia(PassThroughProcessor):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__()
                self.synthesis_requested = asyncio.Event()
                self.start_synthesis = asyncio.Event()
                self.finish_ordinary = asyncio.Event()
                self.normal_synthesis_requested = asyncio.Event()
                self.release_normal_synthesis = asyncio.Event()
                self.spoken_requests: list[str] = []
                self._register_event_handler("on_tts_request")
                self.finish_assistant_playback = asyncio.Event()
                self.open_reply_context: str | None = None
                self._playback_task: asyncio.Task[None] | None = None
                harness.tts = self

            async def _push_audio(self, context_id: str, *, stop: bool) -> None:
                await self.push_frame(TTSStartedFrame(context_id=context_id), FrameDirection.DOWNSTREAM)
                await self.push_frame(
                    TTSAudioRawFrame(
                        audio=b"\x00\x00",
                        sample_rate=24_000,
                        num_channels=1,
                        context_id=context_id,
                    ),
                    FrameDirection.DOWNSTREAM,
                )
                if stop:
                    await self.push_frame(
                        TTSStoppedFrame(context_id=context_id),
                        FrameDirection.DOWNSTREAM,
                    )

            async def _finish_ordinary_audio(self) -> None:
                await self.finish_ordinary.wait()
                await self.push_frame(
                    TTSStoppedFrame(context_id="ordinary-audio"),
                    FrameDirection.DOWNSTREAM,
                )
            async def _run_assistant_playback(self) -> None:
                try:
                    await self.finish_assistant_playback.wait()
                    await self.push_frame(
                        TTSAudioRawFrame(
                            audio=b"\x00\x00",
                            sample_rate=24_000,
                            num_channels=1,
                            context_id="assistant-playback",
                        ),
                        FrameDirection.DOWNSTREAM,
                    )
                    await self.push_frame(
                        TTSStoppedFrame(context_id="assistant-playback"),
                        FrameDirection.DOWNSTREAM,
                    )
                except asyncio.CancelledError:
                    await self.push_frame(
                        TTSStoppedFrame(context_id="assistant-playback"),
                        FrameDirection.DOWNSTREAM,
                    )

            async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
                await FrameProcessor.process_frame(self, frame, direction)
                if direction == FrameDirection.DOWNSTREAM and isinstance(frame, InterruptionFrame):
                    if self._playback_task and not self._playback_task.done():
                        self._playback_task.cancel()
                if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMTextFrame):
                    if harness.scenario in {"late-transcription", "transcription", "duplicate-farewell", "extended-farewell", "turn-handling"}:
                        await self._call_event_handler("on_tts_request", "assistant-reply", frame.text)
                        await self._push_audio("assistant-reply", stop=True)
                    elif harness.scenario in {"natural-farewell-delayed", "prior-speech-starts-late"}:
                        context_id = "ordinary-audio" if harness.scenario == "prior-speech-starts-late" else "assistant-reply"
                        await self._call_event_handler("on_tts_request", context_id, frame.text)
                        self.normal_synthesis_requested.set()
                        await self.release_normal_synthesis.wait()
                        await self._push_audio(context_id, stop=True)
                    elif harness.existing_speech:
                        await self._call_event_handler("on_tts_request", "ordinary-audio", frame.text)
                        await self._push_audio("ordinary-audio", stop=False)
                        harness._track(self._finish_ordinary_audio())
                    elif harness.scenario == "backchannel-during-playback":
                        if frame.text == ASSISTANT_TEXT:
                            await self._call_event_handler("on_tts_request", "assistant-playback", frame.text)
                            await self.push_frame(TTSStartedFrame(context_id="assistant-playback"), FrameDirection.DOWNSTREAM)
                            self._playback_task = harness._track(self._run_assistant_playback())
                        else:
                            context_id = "outside-reply" if frame.text == OUTSIDE_ACK_REPLY_TEXT else "command-reply"
                            self.open_reply_context = context_id
                            await self._call_event_handler("on_tts_request", context_id, frame.text)
                            await self._push_audio(context_id, stop=False)
                        return
                    return
                if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMFullResponseEndFrame):
                    if harness.scenario == "backchannel-during-playback" and self.open_reply_context:
                        ctx = self.open_reply_context
                        self.open_reply_context = None
                        await self.push_frame(
                            TTSStoppedFrame(context_id=ctx),
                            FrameDirection.DOWNSTREAM,
                        )
                if direction == FrameDirection.DOWNSTREAM and isinstance(frame, TTSSpeakFrame):
                    self.synthesis_requested.set()
                    self.spoken_requests.append(frame.text)
                    await self._call_event_handler("on_tts_request", "farewell-audio", frame.text)
                    if harness.scenario in {"duplicate-farewell", "extended-farewell"}:
                        await self._push_audio("farewell-audio", stop=True)
                        return
                    await self.start_synthesis.wait()
                    await self._push_audio("farewell-audio", stop=True)
                    return
                await self.push_frame(frame, direction)

        class Connection:
            def __init__(self) -> None:
                self.handlers: dict[str, Any] = {}
                self.messages: list[dict[str, object]] = []
                self.call_completed = asyncio.Event()
                self.farewell_caption = asyncio.Event()
                self.extended_farewell_caption = asyncio.Event()
                self.ack_final_caption = asyncio.Event()
                self.any_user_caption = asyncio.Event()
                self.user_final_caption = asyncio.Event()
                self.command_reply_caption = asyncio.Event()
                self.assistant_final_caption = asyncio.Event()

                self.ack_partial_caption = asyncio.Event()
                self.outside_ack_reply_caption = asyncio.Event()
                self.assistant_partial_caption = asyncio.Event()
            def add_event_handler(self, name: str, handler: Any) -> None:
                self.handlers[name] = handler

            def send_app_message(self, message: dict[str, object]) -> None:
                self.messages.append(message)
                if message.get("type") == "caption":
                    if message.get("role") == "user":
                        self.any_user_caption.set()
                        if message.get("final") is True:
                            self.user_final_caption.set()
                    if (
                        message.get("role") == "user"
                        and message.get("text") == harness.backchannel_text
                        and message.get("final") is True
                    ):
                        self.ack_final_caption.set()
                    if (
                        message.get("role") == "user"
                        and message.get("text") == harness.backchannel_text
                        and message.get("final") is False
                    ):
                        self.ack_partial_caption.set()
                    if (
                        message.get("role") == "assistant"
                        and message.get("text") == OUTSIDE_ACK_REPLY_TEXT
                        and message.get("final") is True
                    ):
                        self.outside_ack_reply_caption.set()
                    if (
                        message.get("role") == "assistant"
                        and message.get("text") == COMMAND_REPLY_TEXT
                        and message.get("final") is True
                    ):
                        self.command_reply_caption.set()
                    if (
                        message.get("role") == "assistant"
                        and message.get("text") in {ASSISTANT_TEXT, "नमस्ते आरव!"}
                        and message.get("final") is True
                    ):
                        self.assistant_final_caption.set()
                    if (
                        message.get("role") == "assistant"
                        and message.get("text") == FAREWELL_TEXT
                        and message.get("final") is True
                    ):
                        self.farewell_caption.set()
                    if (
                        message.get("role") == "assistant"
                        and message.get("text") in {FAREWELL_TEXT, ORDINARY_TEXT}
                        and message.get("final") is False
                    ):
                        self.assistant_partial_caption.set()
                    if (
                        message.get("role") == "assistant"
                        and message.get("text") == EXTENDED_FAREWELL_TEXT
                        and message.get("final") is True
                    ):
                        self.extended_farewell_caption.set()
                if message.get("type") == "call.completed":
                    harness.events.append("call-completed")
                    self.call_completed.set()

            async def disconnect(self) -> None:
                return None

        monkeypatch.setattr("app.voice.SmallWebRTCTransport", FakeTransport)
        monkeypatch.setattr("app.voice.DeepgramSTTService", FakeSTT)
        monkeypatch.setattr("app.voice.AzureSTTService", FakeSTT)
        monkeypatch.setattr("app.voice.OpenAILLMService", FakeOpenAI)
        monkeypatch.setattr("app.voice.CartesiaTTSService", FakeCartesia)

        assert self.session_id is not None and self.snapshot is not None
        self.runtime = CallRuntime.create(
            self.session_id,
            "test",
            self.snapshot,
            self.environment,
            claim=getattr(self, "claim", None),
            coordination=getattr(self, "coordination", None),
        )
        self.connection = Connection()
        await self.runtime.attach(self.connection)

    async def _create_session(self) -> None:
        async with engine.begin() as db_connection:
            await db_connection.run_sync(Base.metadata.create_all)
        self._lifespan_cm = None
        if getattr(app.state, "coordination", None) is None:
            self._lifespan_cm = app.router.lifespan_context(app)
            await self._lifespan_cm.__aenter__()
        self.client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://localhost:3000",
        )
        email = f"{uuid.uuid4()}@example.com"
        password = "correct horse battery staple"
        registration = await self.client.post(
            "/auth/register",
            json={"email": email, "password": password},
        )
        assert registration.status_code == 201
        login = await self.client.post(
            "/auth/login",
            json={"email": email, "password": password},
        )
        assert login.status_code == 200
        headers = {
            "Origin": "http://localhost:3000",
            "X-CSRF-Token": login.json()["csrf_token"],
        }
        voice_id = "cartesia-hi" if self.language == "hi" else "cartesia-en"
        stt_model = "azure-speech" if self.stt_provider == "azure" else "nova-3"
        agent_response = await self.client.post(
            "/agents",
            headers=headers,
            json={
                "name": "Voice pipeline test",
                "slug": f"voice-pipeline-{uuid.uuid4().hex}",
                "instructions": "Help the caller.",
                "languages": [self.language],
                "default_language": self.language,
                "stt_provider": self.stt_provider,
                "stt_model": stt_model,
                "azure_stt_english_locale": self.azure_stt_english_locale,
                "llm_provider": "openai",
                "llm_model": "gpt-4o-mini",
                "tts_provider": "cartesia",
                "tts_model": "sonic-3.6",
                "voice_id": voice_id,
            },
        )
        assert agent_response.status_code == 201
        call_response = await self.client.post(
            f"/agents/{agent_response.json()['id']}/test-calls",
            headers=headers,
            json={"language": self.language},
        )
        assert call_response.status_code == 201
        self.session_id = uuid.UUID(call_response.json()["session_id"])
        signaling_token = call_response.json()["signaling_token"]
        self.snapshot = await load_session_snapshot(self.session_id)
        assert self.snapshot is not None

        coordination = getattr(app.state, "coordination", None)
        if coordination is not None:
            claim = await coordination.consume_token(signaling_token)
            obs = await coordination.begin_setup(claim)
            async with Session() as db:
                await db.execute(text("SELECT pg_advisory_xact_lock(42000001)"))
                await db.execute(
                    update(CallSession)
                    .where(CallSession.id == self.session_id, CallSession.status == "created")
                    .values(
                        worker_id=coordination.worker_id,
                        claim_generation=obs.claim.generation,
                        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=45),
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                await db.commit()
            assert await set_session_status(self.session_id, "active", claim=obs.claim)
            active_obs = await coordination.activate(obs.claim, "test-harness-pc")
            self.claim = active_obs.claim
            self.coordination = coordination
        else:
            assert await set_session_status(self.session_id, "active")
            self.claim = None
            self.coordination = None

    async def close(self, *, close_client: bool = True) -> None:
        if self.tts is not None:
            self.tts.finish_ordinary.set()
            self.tts.start_synthesis.set()
            self.tts.release_normal_synthesis.set()
            self.tts.finish_assistant_playback.set()
        if self.runtime is not None:
            if (
                not self.runtime.closing
                and self.connection is not None
                and "closed" in self.connection.handlers
            ):
                await self.connection.handlers["closed"]()
            if self.runtime.cleanup_task is not None:
                await asyncio.wait_for(asyncio.shield(self.runtime.cleanup_task), timeout=5)
        pending_tasks = [task for task in self.background_tasks if not task.done()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        if close_client:
            if self.client is not None:
                await self.client.aclose()
                self.client = None
            if getattr(self, "_lifespan_cm", None) is not None:
                await self._lifespan_cm.__aexit__(None, None, None)
                self._lifespan_cm = None
