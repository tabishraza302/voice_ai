#!/usr/bin/env python3
"""Measure TTS speed on this machine, and write the numbers the bot needs.

The number the user waits for is **time to first audio** (TTFB): the
gap between the bot deciding what to say and the first sound of it.
The STT and the LLM are measured by ``bench_stt.py`` and by the traces;
this is the only measurement of the stage that actually speaks.

Two backends are measured when both are available:

* ``kokoro`` -- Pipecat's in-process ONNX build, on the CPU;
* ``mlx-audio`` -- the local server, on the Metal GPU.

Both are driven through a real Pipecat pipeline, with the frames the bot
actually sends, so the number includes the service's own bookkeeping and
the resampler rather than just the model. The first request of each
backend is a warm-up and is discarded: a cold model load is not what a
conversation feels like.

The clock starts when the *text* reaches the TTS service and stops when
the first audio frame comes out of it, so the number is the wait the bot
actually imposes on itself, not the model in isolation.

Usage:
    uv run python scripts/bench_tts.py
    uv run python scripts/bench_tts.py --backend kokoro
    uv run python scripts/bench_tts.py --backend mlx-audio
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from pipecat.frames.frames import LLMTextFrame, TTSAudioRawFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.tests.utils import run_test
from pipecat.transcriptions.language import Language

from bot.config import Settings
from bot.tts_service import LocalMlxTTSService

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "tts_bench.json"

# The sentences the bot actually says: a short answer, a medium one, and
# a long one, in both languages. The first audio of a long sentence is
# slower than the first audio of a short one, because the synthesiser
# has more to chew on before it emits anything.
SENTENCES: list[tuple[str, str]] = [
    ("en", "Yes, I can hear you perfectly."),
    ("en", "I have checked the system and everything is working normally."),
    (
        "en",
        "I can walk you through the setup in a couple of minutes, "
        "starting with the configuration file and then the model download.",
    ),
    ("hi", "हाँ, मैं आपको साफ़ सुनाई दे रहा हूँ।"),
    ("hi", "मैंने सिस्टम जाँच लिया है और सब कुछ सही काम कर रहा है।"),
]

# The mlx-audio model this measures. It is the same Kokoro-82M family the
# in-process backend runs, so the two are comparable on voice quality --
# only the runtime (ONNX on the CPU vs MLX on the Metal GPU) differs.
MLX_MODEL = "mlx-community/Kokoro-82M-4bit"


@dataclass
class Timings:
    """What one synthesis cost."""

    backend: str
    language: str
    text: str
    ttfb_seconds: float = 0.0
    total_seconds: float = 0.0
    audio_seconds: float = 0.0
    samples: int = 0

    @property
    def rtf(self) -> float:
        """Real-time factor: synthesis seconds per second of audio."""
        if not self.audio_seconds:
            return 0.0
        return self.total_seconds / self.audio_seconds

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "language": self.language,
            "text": self.text,
            "ttfb_seconds": round(self.ttfb_seconds, 4),
            "total_seconds": round(self.total_seconds, 4),
            "audio_seconds": round(self.audio_seconds, 3),
            "rtf": round(self.rtf, 4),
        }


class AudioObserver(BaseObserver):
    """Records when the text reached the TTS, and when the audio came back.

    The text frame and the audio frames are both pushed through the
    pipeline, so one observer sees the whole exchange: the text frame
    marks the start of the wait, the first audio frame marks its end.
    """

    def __init__(self) -> None:
        # One event per frame, not one per processor that passes it on:
        # otherwise the same audio would be counted once per hop.
        super().__init__(observe_every_push=False)
        self.text_at: float | None = None
        self.first_audio_at: float | None = None
        self.last_audio_at: float | None = None
        self.samples = 0
        self.sample_rate = 0

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        now = time.monotonic()

        if isinstance(frame, LLMTextFrame):
            if self.text_at is None:
                self.text_at = now
            return

        if not isinstance(frame, TTSAudioRawFrame):
            return

        if self.first_audio_at is None:
            self.first_audio_at = now
        self.last_audio_at = now
        # `num_frames` is the per-channel sample count: the frame holds
        # 16-bit PCM, so `len(audio) / (channels * 2)`.
        self.samples += frame.num_frames
        self.sample_rate = frame.sample_rate or self.sample_rate


async def synthesize(service, text: str) -> tuple[float, float, float]:
    """Send one sentence to a TTS service and time it.

    Returns (ttfb, total, audio_seconds). The clock starts when the text
    reaches the service and stops when the last audio frame comes out.
    """
    observer = AudioObserver()
    await run_test(
        service,
        frames_to_send=[LLMTextFrame(text=text)],
        observers=[observer],
    )
    if observer.text_at is None:
        raise RuntimeError(f"the text never reached the service for {text!r}")
    if observer.first_audio_at is None or observer.last_audio_at is None:
        raise RuntimeError(f"no audio came back for {text!r}")
    audio_seconds = observer.samples / observer.sample_rate if observer.sample_rate else 0.0
    return (
        observer.first_audio_at - observer.text_at,
        observer.last_audio_at - observer.text_at,
        audio_seconds,
    )


def build_kokoro(settings: Settings) -> dict:
    """Pipecat's in-process ONNX Kokoro, one service per voice.

    Configured exactly as ``bot/services.py`` configures the bot's: the
    same voices, the same speed, and ``TextAggregationMode.TOKEN`` --
    the bot's own processor does the sentence splitting, so the TTS must
    not add a second wait of its own.
    """
    return {
        "en": KokoroTTSService(
            settings=KokoroTTSService.Settings(
                voice=settings.tts_voice_en,
                language=Language.EN,
                speed=settings.tts_speed,
            ),
            text_aggregation_mode=TextAggregationMode.TOKEN,
        ),
        "hi": KokoroTTSService(
            settings=KokoroTTSService.Settings(
                voice=settings.tts_voice_hi,
                language=Language.HI,
                speed=settings.tts_speed,
            ),
            text_aggregation_mode=TextAggregationMode.TOKEN,
        ),
    }


def build_mlx_audio(settings: Settings, model: str, base_url: str) -> dict:
    """The local mlx-audio server, one service per voice."""
    return {
        "en": LocalMlxTTSService(
            model=model,
            base_url=base_url,
            sample_rate=24000,
            settings=LocalMlxTTSService.Settings(
                model=model,
                voice=settings.tts_voice_en,
                language=Language.EN,
                speed=settings.tts_speed,
            ),
            text_aggregation_mode=TextAggregationMode.TOKEN,
        ),
        "hi": LocalMlxTTSService(
            model=model,
            base_url=base_url,
            sample_rate=24000,
            settings=LocalMlxTTSService.Settings(
                model=model,
                voice=settings.tts_voice_hi,
                language=Language.HI,
                speed=settings.tts_speed,
            ),
            text_aggregation_mode=TextAggregationMode.TOKEN,
        ),
    }


def _server_models(base_url: str) -> set[str]:
    """The model ids the local server currently holds resident."""
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/v1/models", timeout=5) as response:
        payload = json.loads(response.read())
    return {entry.get("id") for entry in payload.get("data", [])}


def ensure_mlx_model(model: str, base_url: str) -> bool:
    """Make sure the TTS model is resident on the local server.

    Loading it takes minutes on this link, and a benchmark that silently
    measured a cold start would be worse than no benchmark at all.
    """
    root = base_url.rstrip("/")
    try:
        if model in _server_models(root):
            return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"  mlx-audio: the server at {root} is not answering — {exc}", flush=True)
        return False

    print(f"  mlx-audio: loading {model} (minutes, not seconds)", flush=True)
    request = urllib.request.Request(f"{root}/v1/models?model_name={model}", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            response.read()
    except (urllib.error.URLError, OSError) as exc:
        print(f"  mlx-audio: FAILED — could not load {model}: {exc}", flush=True)
        return False

    try:
        if model not in _server_models(root):
            print(f"  mlx-audio: FAILED — {model} is not resident after loading", flush=True)
            return False
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"  mlx-audio: FAILED — could not check residency: {exc}", flush=True)
        return False

    return True


async def measure(backend: str, services: dict) -> list[Timings]:
    """Measure every sentence against one backend.

    The first request in each *language* is a warm-up and is discarded.
    Both backends build their phonemizer lazily per language, so a cold
    English pipeline would otherwise be charged to the first English
    sentence and a cold Hindi one to the first Hindi sentence. A cold
    model load is not what a conversation feels like.
    """
    results: list[Timings] = []
    warmed: set[str] = set()

    for language, text in SENTENCES:
        service = services[language]

        if language not in warmed:
            warmed.add(language)
            logger.info(f"{backend}: warm-up ({language})")
            try:
                await synthesize(service, text)
            except Exception as exc:  # noqa: BLE001
                print(f"  {backend:10} {language}: FAILED — {exc}", flush=True)
                return results

        try:
            ttfb, total, audio = await synthesize(service, text)
        except Exception as exc:  # noqa: BLE001
            print(f"  {backend:10} {language}: FAILED — {exc}", flush=True)
            continue

        timing = Timings(
            backend=backend,
            language=language,
            text=text,
            ttfb_seconds=ttfb,
            total_seconds=total,
            audio_seconds=audio,
        )
        results.append(timing)
        print(
            f"  {backend:10} {language} ttfb={ttfb:.3f}s total={total:.3f}s "
            f"audio={audio:.2f}s rtf={timing.rtf:.3f}  {text[:40]!r}",
            flush=True,
        )

    return results


async def main() -> int:
    settings = Settings(_env_file=None)
    ap = argparse.ArgumentParser(description="Measure TTS speed on this machine.")
    ap.add_argument("--backend", choices=["kokoro", "mlx-audio", "both"], default="both")
    ap.add_argument("--mlx-model", default=MLX_MODEL)
    ap.add_argument("--mlx-base-url", default=settings.tts_base_url)
    ap.add_argument("--voice-en", default=settings.tts_voice_en)
    ap.add_argument("--voice-hi", default=settings.tts_voice_hi)
    args = ap.parse_args()

    # The voices are arguments, so they win over the settings file.
    settings = settings.model_copy(
        update={"tts_voice_en": args.voice_en, "tts_voice_hi": args.voice_hi}
    )

    results: list[Timings] = []

    if args.backend in ("kokoro", "both"):
        print("=== kokoro (in-process ONNX, CPU) ===", flush=True)
        try:
            services = build_kokoro(settings)
        except Exception as exc:  # noqa: BLE001
            print(f"  kokoro: FAILED — {exc}", flush=True)
        else:
            results.extend(await measure("kokoro", services))

    if args.backend in ("mlx-audio", "both"):
        print(f"\n=== mlx-audio ({args.mlx_model}, Metal) ===", flush=True)
        if not ensure_mlx_model(args.mlx_model, args.mlx_base_url):
            print("  mlx-audio: FAILED — the model is not available", flush=True)
        else:
            try:
                services = build_mlx_audio(settings, args.mlx_model, args.mlx_base_url)
            except Exception as exc:  # noqa: BLE001
                print(f"  mlx-audio: FAILED — {exc}", flush=True)
            else:
                results.extend(await measure("mlx-audio", services))

    payload: dict = {
        "sentences": len(SENTENCES),
        "results": [r.to_dict() for r in results],
    }
    by_backend: dict[str, list[Timings]] = {}
    for r in results:
        by_backend.setdefault(r.backend, []).append(r)
    for backend, rs in by_backend.items():
        ttfbs = [r.ttfb_seconds for r in rs]
        payload[f"{backend}_ttfb_p50"] = round(statistics.median(ttfbs), 4)
        payload[f"{backend}_ttfb_max"] = round(max(ttfbs), 4)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    print("\n=== summary ===")
    for backend, rs in by_backend.items():
        ttfbs = [r.ttfb_seconds for r in rs]
        rtfs = [r.rtf for r in rs]
        print(
            f"  {backend:10} ttfb p50={statistics.median(ttfbs):.3f}s "
            f"max={max(ttfbs):.3f}s  rtf p50={statistics.median(rtfs):.3f}"
        )
    print(f"  wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
