"""Streaming STT against the local `mlx_audio.server` realtime endpoint.

``/v1/realtime`` is the OpenAI Realtime API's transcription subset: one
WebSocket per session, ``input_audio_buffer.append`` for audio,
``input_audio_buffer.commit`` at the end of a turn, and
``conversation.item.input_audio_transcription.completed`` with the text.

Pipecat's ``OpenAIRealtimeSTTService`` is the right base — its local-VAD
mode commits on ``VADUserStoppedSpeakingFrame``, which is exactly the
contract this pipeline wants: Silero plus Smart Turn v3 still decide
where a turn ends, and the server only transcribes. Five things differ
from OpenAI's service, and each is a fact about the local server:

1. **No API key, and the model rides in the query string.** The server
   picks the model with ``?model=<id>`` and ignores auth headers.
2. **The session has no language field.** ``session.update`` here reads
   ``model``, ``format.rate`` and ``turn_detection`` only. A language
   would be accepted and ignored — the silent no-op this repo refuses —
   so the factory rejects a pinned ``stt_language`` and the language
   router refuses to send one.
3. **The declared rate is the pipeline's rate.** The stock service
   resamples 16 kHz to 24 kHz, and the server then resamples 24 kHz down
   to the model's own 16 kHz. Declaring the pipeline rate skips both
   conversions.
4. **The rate is known only after ``StartFrame``.** ``setup()`` connects
   and the server's ``session.created`` arrives before the pipeline
   starts, so the session update is sent from ``start()`` instead.
5. **Silence is not sent, and the two edges of a turn are padded.** In
   manual-commit mode the server feeds the model every chunk it receives
   and decodes it; silence would keep the GPU busy and invite hallucinated
   text. So audio is only streamed while the VAD says the user is
   speaking. Two cushions match the batch path in ``SegmentedSTTService``:
   one second of audio from *before* the VAD fired is sent when speech is
   confirmed, and half a second of silence is sent before the commit so
   the model can finish the last word.
"""

from __future__ import annotations

import base64
import urllib.parse
from collections.abc import AsyncGenerator
from typing import Any

from pipecat.frames.frames import (
    Frame,
    StartFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.openai.stt import OpenAIRealtimeSTTService
from pipecat.utils.types import assert_given
from websockets.protocol import State


def websocket_url(base_url: str, model: str) -> str:
    """The ``/v1/realtime`` URL for an ``http(s)://host/v1`` base URL."""
    parts = urllib.parse.urlsplit(base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = parts.path.rstrip("/")
    if not path.endswith("/realtime"):
        path = f"{path}/realtime"
    query = urllib.parse.urlencode({"model": model})
    return urllib.parse.urlunsplit((scheme, parts.netloc, path, query, ""))


class LocalMlxRealtimeSTTService(OpenAIRealtimeSTTService):
    """Streaming STT against the local server's ``/v1/realtime``."""

    # The local server needs no key, but the OpenAI client requires one.
    _requires_api_key = False

    # The batch path's two quality safeguards, matched. See the module
    # docstring: the VAD confirms speech late, and models garble a segment
    # that ends mid-word.
    PREROLL_SECS = 1.0
    TRAILING_SILENCE_SECS = 0.5

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        ttfs_p99_latency: float | None = None,
        **kwargs: Any,
    ) -> None:
        self._ws_url = websocket_url(base_url, model)
        self._preroll: list[bytes] = []
        self._preroll_bytes = 0
        # Set when the session has been told the pipeline's rate. Kept
        # separate from the parent's `_session_ready`, which it sets even
        # when no update was sent (see `_send_session_update`).
        self._rate_declared = False
        super().__init__(
            api_key="local",  # placeholder: the local server ignores it
            base_url=base_url,
            settings=self.Settings(model=model, language=None),
            turn_detection=False,
            ttfs_p99_latency=ttfs_p99_latency,
            **kwargs,
        )

    # -- connection -------------------------------------------------------

    async def _connect_websocket(self) -> None:
        """Open the local socket. The parent's URL carries OpenAI's query."""
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return
            self._session_ready = False
            # The flag describes this socket's session, so a reconnect
            # starts with it unset and the update is sent again.
            self._rate_declared = False
            self._websocket = await self._websocket_connect(uri=self._ws_url)
            await self._call_event_handler("on_connected")
        except Exception as e:  # noqa: BLE001
            await self.push_error(
                error_msg=f"Error connecting to the local realtime STT: {e}",
                exception=e,
            )
            self._websocket = None

    async def start(self, frame: StartFrame) -> None:
        """Send the session update once the pipeline rate is known."""
        await super().start(frame)
        if self._websocket and not self._rate_declared:
            await self._send_session_update()

    async def _send_session_update(self) -> None:
        """Configure the session with the three fields this server reads.

        ``session.created`` arrives during ``setup()``, before the
        ``StartFrame`` sets the rate; with no rate there is nothing
        honest to declare, so ``start()`` sends this instead.
        """
        if self.sample_rate <= 0:
            return
        model = assert_given(self._settings.model)
        await self._ws_send(
            {
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {
                        "input": {
                            "format": {"type": "audio/pcm", "rate": self.sample_rate},
                            "transcription": {"model": model},
                            "turn_detection": None,
                        }
                    },
                },
            }
        )
        self._rate_declared = True

    async def _send_audio(self, audio: bytes) -> None:
        """Send PCM as it arrived: the session declares the pipeline rate."""
        if not audio:
            return
        await self._ws_send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(audio).decode("ascii"),
            }
        )

    # -- audio ------------------------------------------------------------

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Stream audio while the user speaks, with the two turn cushions."""
        if not self._user_speaking:
            self._remember_preroll(audio)
            yield None
            return

        for chunk in self._take_preroll():
            await self._send_audio(chunk)
        await self._send_audio(audio)
        yield None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Pad the end of a turn before the parent commits it."""
        if isinstance(frame, VADUserStoppedSpeakingFrame) and not self._server_vad_enabled:
            await self._send_audio(self._trailing_silence())
        await super().process_frame(frame, direction)

    async def push_frame(
        self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM
    ) -> None:
        """Mark completed transcripts as final, and keep them through a reset.

        ``finalized`` tells the turn strategy this text is the whole
        utterance. ``interruptible = False`` tells the aggregator's
        turn-start reset to keep it: the commit flush sends the last delta
        and the completed text back to back, so the delta starts the turn
        while the completed frame is still queued behind it, and a reset
        drops queued interruptible frames — the transcript would never
        reach the LLM.
        """
        if isinstance(frame, TranscriptionFrame):
            frame.finalized = True
            frame.interruptible = False
        await super().push_frame(frame, direction)

    # -- buffers ----------------------------------------------------------

    def _remember_preroll(self, audio: bytes) -> None:
        """Keep at most ``PREROLL_SECS`` of pre-speech audio."""
        if not audio:
            return
        self._preroll.append(audio)
        self._preroll_bytes += len(audio)
        limit = max(1, int(self.sample_rate * self.PREROLL_SECS)) * 2
        while self._preroll and self._preroll_bytes > limit:
            self._preroll_bytes -= len(self._preroll.pop(0))

    def _take_preroll(self) -> list[bytes]:
        """Hand over the buffered pre-speech audio and clear the buffer."""
        chunks = self._preroll
        self._preroll = []
        self._preroll_bytes = 0
        return chunks

    def _trailing_silence(self) -> bytes:
        """Silence appended before the commit, as 16-bit mono PCM."""
        num_samples = max(0, int(self.sample_rate * self.TRAILING_SILENCE_SECS))
        return bytes(num_samples * 2)
