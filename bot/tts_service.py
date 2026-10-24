"""TTS backend for the local `mlx-audio` server.

One service, many models. `mlx_audio` ships MLX conversions of several TTS
families — Kokoro, Chatterbox (including the multilingual v3 that speaks
Hindi), PocketTTS — and the server exposes them all through one
OpenAI-compatible endpoint, ``POST /v1/audio/speech``.

That matters for the swappable-layers requirement: without this class,
adding a TTS model would mean writing a bespoke Pipecat service per model.
With it, swapping the voice is a model id.

The default TTS runs through this class: Kokoro-82M on the Metal GPU,
through the same local server that serves the STT, because it reaches
the first sound 2.4× sooner than the in-process ONNX build
(scripts/bench_tts.py, warm, five sentences, this machine). Pipecat's own
``KokoroTTSService`` is the swap for a deployment that does not want
the server. This class also serves the multilingual Chatterbox model, so
the fallback when Kokoro's grade-C Hindi voices prove unintelligible is
one config change away rather than a project.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Any, AsyncGenerator

import numpy as np
from loguru import logger
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.tracing.service_decorators import traced_tts


@dataclass
class LocalMlxTTSSettings(TTSSettings):
    """Settings for the local mlx-audio TTS service.

    Attributes:
        model: The mlx-audio model repo id, e.g.
            ``mlx-community/chatterbox-multilingual-v3``.
        voice: An optional voice id. Models that ship per-voice packs
            (Kokoro) use it; models that do not ignore it.
        language: The language of the text, used to build ``lang_code``.
        speed: Speech rate multiplier.
        lang_code: The model's own language code, overriding ``language``.
    """

    model: str | None = None
    voice: str | None = None
    language: Language | str | None = None
    speed: float = 1.0
    lang_code: str | None = None


class LocalMlxTTSService(TTSService):
    """TTS against a local `mlx_audio.server`."""

    Settings = LocalMlxTTSSettings
    _settings: LocalMlxTTSSettings

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        sample_rate: int = 24000,
        settings: LocalMlxTTSSettings | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            settings=settings
            or self.Settings(model=model, language=Language.EN, speed=1.0),
            **kwargs,
        )
        self._base_url = base_url.rstrip("/")
        self._sample_rate = sample_rate
        self._resampler = create_stream_resampler()

    # -- language ---------------------------------------------------------

    def language_to_service_language(self, language: Language) -> str | None:
        """Map a Pipecat language to the model's own language code.

        The mlx-audio TTS families take a two-letter code (``en``, ``hi``),
        which is a different convention from the STT side's ``hi-IN``.

        This is Pipecat's input-side hook: it is called with a
        ``Language`` when the settings are written, and the *stored*
        language is the string it returns.
        """
        if language is None:
            return None
        if isinstance(language, Language):
            return language.value.split("-")[0].lower()
        return str(language).split("-")[0].lower()

    def _lang_code(self) -> str | None:
        """The two-letter language code to send with a synthesis request.

        Pipecat stores the *service-native* language: it converts the
        ``Language`` enum through ``language_to_service_language()`` when
        the settings are written, so by the time a request is made this is
        already ``"hi"`` rather than ``Language.HI``. The enum case is
        handled anyway, in case a settings object is assigned directly.
        """
        if self._settings.lang_code:
            return self._settings.lang_code
        language = self._settings.language
        if language is None:
            return None
        if isinstance(language, Language):
            return self.language_to_service_language(language)
        return str(language).split("-")[0].lower()

    def can_generate_metrics(self) -> bool:
        return True

    # -- synthesis --------------------------------------------------------

    @traced_tts
    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        """Synthesize one chunk of text and yield its audio frames."""
        try:
            await self.start_tts_usage_metrics(text)

            payload = {
                "model": self._settings.model,
                "input": text,
                "speed": self._settings.speed,
                "response_format": "wav",
            }
            if self._settings.voice:
                payload["voice"] = self._settings.voice

            lang_code = self._lang_code()
            if lang_code:
                payload["lang_code"] = lang_code

            audio_bytes, sample_rate = await self._synthesize(payload)

            await self.stop_ttfb_metrics()

            if not audio_bytes:
                return

            # Resample to the pipeline's rate so the output transport does
            # not have to.
            resampled = await self._resampler.resample(audio_bytes, sample_rate, self._sample_rate)

            yield TTSAudioRawFrame(
                audio=resampled,
                sample_rate=self._sample_rate,
                num_channels=1,
                context_id=context_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"TTS synthesis failed: {exc}")
            yield ErrorFrame(error=f"TTS synthesis failed: {exc}")
        finally:
            await self.stop_ttfb_metrics()

    async def _synthesize(self, payload: dict) -> tuple[bytes, int]:
        """POST to the local server and decode the WAV it returns."""
        import httpx

        url = f"{self._base_url}/v1/audio/speech"
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                raise RuntimeError(
                    f"TTS server returned {response.status_code}: {response.text[:400]}"
                )
            body = response.content

        with wave.open(io.BytesIO(body), "rb") as wf:
            sample_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
            width = wf.getsampwidth()
            channels = wf.getnchannels()

        if width != 2:
            raise RuntimeError(f"expected 16-bit PCM from the TTS server, got {width * 8}-bit")
        if channels > 1:
            samples = np.frombuffer(frames, dtype="<i2").reshape(-1, channels)
            pcm = samples.mean(axis=1).astype("<i2").tobytes()
        else:
            pcm = frames

        return pcm, sample_rate
