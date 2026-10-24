#!/usr/bin/env python3
"""Measure one conversational turn, stage by stage.

The traces say the bot takes 1.514 s from the end of the user's speech to
the final transcript, and that only 0.97 s of it is accounted for. A trace
is one session on one afternoon: it cannot be re-run on demand, and the
per-stage split of a single turn needs the same audio every time.

This replays a recorded utterance through the **real** pipeline — real STT,
real TTS, real turn strategies, real VAD — with a stub LLM that streams a
fixed reply, so the measurement does not move with the model's mood. It
timestamps five events and prints the gap between consecutive ones:

* the end of the user's speech (the last audio frame that carries it),
* the VAD's decision that the user stopped,
* the transcript,
* the model's first token,
* the first sound of the reply.

The audio is fed at **real time**. The VAD counts 32 ms windows of audio,
not seconds of wall clock, so a file pushed as fast as the pipeline takes
would collapse the silence window and measure nothing at all.

Usage:
    uv run python scripts/bench_turn.py
    uv run python scripts/bench_turn.py --runs 5 --backend mlx-audio
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import statistics
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.llm_service import LLMService
from pipecat.workers.runner import WorkerRunner

from bot.bot import build_turn_strategies, build_vad_analyzer
from bot.config import Settings
from bot.context import ContextShapingProcessor
from bot.pipeline import LanguageRoutingProcessor
from bot.services import build_stt, build_tts, build_voice_router

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "turn_bench.json"
OUT_PATH_REALTIME = REPO_ROOT / "data" / "turn_bench_realtime.json"
REALTIME_PROVIDER = "mlx-audio-realtime"
DEFAULT_AUDIO = REPO_ROOT / "evals" / "fixtures" / "audio" / "en_us" / "0001.wav"

# The pipeline's rates: 16 kHz in for Silero VAD's sake, 24 kHz out for
# the TTS engines. Same as the agent.
AUDIO_IN_SAMPLE_RATE = 16000
AUDIO_OUT_SAMPLE_RATE = 24000

# The reply the stub LLM streams. It is a normal bot answer: a clause
# boundary early enough to be cut, and a full stop at the end.
STUB_REPLY = "Certainly, I have checked the system, and everything is working normally."

# How long one token takes to arrive. A 4B model on this box produces
# roughly 30 tokens a second, which is what puts the first sentence
# about 0.36 s behind the first token -- the number the traces show.
DEFAULT_TOKEN_DELAY = 0.033

# Silence floor of the FLEURS fixtures is about 0.0005 RMS; speech is
# orders of magnitude above this.
SPEECH_RMS_THRESHOLD = 0.01


def die(msg: str) -> None:
    raise SystemExit(f"bench_turn: {msg}")


def read_wav(path: Path) -> tuple[bytes, int]:
    """Read a mono 16-bit WAV as raw PCM and its sample rate."""
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getsampwidth() != 2:
                die(f"{path} is not 16-bit PCM")
            if wf.getnchannels() != 1:
                die(f"{path} is not mono")
            frames = wf.readframes(wf.getnframes())
            return frames, wf.getframerate()
    except wave.Error as exc:
        die(f"{path} is not a readable WAV: {exc}")
    return b"", 0


def _rms(chunk: bytes) -> float:
    """The root-mean-square of one frame of 16-bit PCM."""
    import numpy as np

    samples = np.frombuffer(chunk, dtype="<i2").astype("float32") / 32768.0
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)))


async def analyse_turn(
    pcm: bytes, sample_rate: int, stop_secs: float, frame_ms: int = 20
) -> tuple[int, int]:
    """Run the real VAD over the recording, offline.

    Two things come out of it:

    * the frame index at which the VAD stops classifying the audio as
      speech — its own estimate of the end of the user's words, and the
      point the silence window is counted from;
    * how many turns the VAD sees. A recording with a pause longer than
      the silence window is several turns, not one, and a benchmark of
      "one turn" that measured three would be measuring the wrong thing.

    This runs before the timed run, so it costs the measurement nothing.
    """
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams

    vad = SileroVADAnalyzer(params=VADParams(stop_secs=stop_secs))
    vad.set_sample_rate(sample_rate)

    frame_bytes = int(sample_rate * frame_ms / 1000) * 2
    state = None
    speaking = False
    counted = False
    speech_end_index = -1
    turns = 0

    for index, offset in enumerate(range(0, len(pcm), frame_bytes)):
        new_state = await vad.analyze_audio(pcm[offset : offset + frame_bytes])
        if new_state == state:
            continue
        if new_state.name == "SPEAKING":
            speaking = True
            counted = False
        elif new_state.name == "STOPPING" and speaking:
            # The VAD's end-of-speech: the first window it calls silence.
            # The silence window is counted from here.
            speech_end_index = index
        elif new_state.name == "QUIET" and speaking and not counted:
            # The turn is over: the silence window has elapsed.
            speech_end_index = index if speech_end_index < 0 else speech_end_index
            turns += 1
            counted = True
            speaking = False
        state = new_state

    return speech_end_index, turns


class AudioSource(FrameProcessor):
    """Feeds a recorded utterance into the pipeline at real time.

    The pacing is the whole point: the VAD's silence window is counted
    in 32 ms windows of *audio*, so a file pushed as fast as the pipeline
    accepts it would collapse the window and measure nothing.

    It also records when the user's speech actually ended, which is the
    point the latency budget starts from.
    """

    def __init__(
        self,
        pcm: bytes,
        sample_rate: int,
        speech_end_index: int,
        frame_ms: int = 20,
        tail_silence_seconds: float = 2.0,
    ) -> None:
        super().__init__()
        self._pcm = pcm
        self._sample_rate = sample_rate
        self._speech_end_index = speech_end_index
        self._frame_bytes = int(sample_rate * frame_ms / 1000) * 2
        self._frame_seconds = self._frame_bytes / 2 / sample_rate
        self._tail_frames = int(tail_silence_seconds / self._frame_seconds)
        self._feed_task: asyncio.Task | None = None
        # Set when the last word has been fed.
        self.speech_end_at: float | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            await self.push_frame(frame, direction)
            self._feed_task = self.create_task(self._feed())
        else:
            await self.push_frame(frame, direction)

    def stop(self) -> None:
        """Stop feeding audio."""
        if self._feed_task is not None:
            self._feed_task.cancel()
            self._feed_task = None

    async def _feed(self) -> None:
        silence = b"\x00" * self._frame_bytes
        index = 0
        for offset in range(0, len(self._pcm), self._frame_bytes):
            chunk = self._pcm[offset : offset + self._frame_bytes]
            await self._push(chunk)
            if index == self._speech_end_index:
                self.speech_end_at = time.monotonic() + self._frame_seconds
            index += 1

        # The microphone keeps running after the user stops talking, and
        # the VAD needs to hear the silence to close the turn.
        for _ in range(self._tail_frames):
            await self._push(silence)

    async def _push(self, chunk: bytes) -> None:
        await self.push_frame(
            InputAudioRawFrame(
                audio=chunk,
                sample_rate=self._sample_rate,
                num_channels=1,
            )
        )
        await asyncio.sleep(self._frame_seconds)


class StubLLMService(LLMService):
    """Streams a fixed reply, token by token.

    The measurement is about the pipeline, not about the model, so the
    model is replaced by something with a known pace. The frames are the
    ones a real LLM service pushes, in the same order: the response
    starts, the text streams, the response ends.
    """

    def __init__(self, reply: str, token_delay: float = 0.0, **kwargs) -> None:
        super().__init__(**kwargs)
        self._reply = reply
        self._token_delay = token_delay

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            await self.push_frame(LLMFullResponseStartFrame())
            words = self._reply.split(" ")
            for index, word in enumerate(words):
                text = word if index == len(words) - 1 else f"{word} "
                await self._push_llm_text(text)
                if self._token_delay:
                    await asyncio.sleep(self._token_delay)
            await self.push_frame(LLMFullResponseEndFrame())
        else:
            await self.push_frame(frame, direction)


class TurnObserver(BaseObserver):
    """Timestamps the five events that make up one turn."""

    def __init__(self) -> None:
        # One event per frame, not one per processor that passes it on.
        super().__init__(observe_every_push=False)
        self.vad_stop_at: float | None = None
        self.transcript_at: float | None = None
        self.llm_token_at: float | None = None
        self.tts_audio_at: float | None = None
        self.transcript_text = ""
        self.first_audio = asyncio.Event()

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame
        now = time.monotonic()

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            if self.vad_stop_at is None:
                self.vad_stop_at = now
        elif isinstance(frame, TranscriptionFrame):
            if self.transcript_at is None:
                self.transcript_at = now
                self.transcript_text = frame.text
        elif isinstance(frame, LLMTextFrame):
            if self.llm_token_at is None:
                self.llm_token_at = now
        elif isinstance(frame, TTSAudioRawFrame):
            if self.tts_audio_at is None:
                self.tts_audio_at = now
                self.first_audio.set()


async def run_once(
    settings: Settings,
    pcm: bytes,
    sample_rate: int,
    speech_end_index: int,
    reply: str,
    token_delay: float,
    timeout: float,
) -> dict:
    """Replay one utterance and return the stage timings."""
    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=build_vad_analyzer(settings),
            user_turn_strategies=build_turn_strategies(),
        ),
    )

    source = AudioSource(pcm, sample_rate, speech_end_index)
    observer = TurnObserver()

    pipeline = Pipeline(
        [
            source,
            build_stt(settings),
            user_aggregator,
            ContextShapingProcessor(),
            StubLLMService(reply, token_delay=token_delay),
            LanguageRoutingProcessor(
                voice_router=build_voice_router(settings),
                deadline_seconds=settings.first_chunk_deadline_seconds,
            ),
            build_tts(settings),
            assistant_aggregator,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=AUDIO_IN_SAMPLE_RATE,
            audio_out_sample_rate=AUDIO_OUT_SAMPLE_RATE,
            enable_metrics=True,
        ),
        observers=[observer],
    )

    runner = WorkerRunner()
    await runner.add_workers(worker)

    run_task = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(observer.first_audio.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("no audio came back before the timeout")
    # Let the rest of the reply out, then take the pipeline down.
    await asyncio.sleep(0.5)
    source.stop()
    run_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await run_task

    return {
        "speech_end_at": source.speech_end_at,
        "vad_stop_at": observer.vad_stop_at,
        "transcript_at": observer.transcript_at,
        "llm_token_at": observer.llm_token_at,
        "tts_audio_at": observer.tts_audio_at,
        "transcript": observer.transcript_text,
    }


def stages_of(run: dict) -> dict:
    """The gaps between the events, in seconds.

    ``vad_stop_seconds``, ``transcript_seconds`` and
    ``llm_first_token_seconds`` are measured from the end of the user's
    speech, because that is where the latency budget starts.
    ``tts_first_audio_seconds`` is the model's first token to the first
    sound -- the stage the TTS owns.
    """
    speech_end = run["speech_end_at"]
    if speech_end is None:
        raise ValueError("the audio never reached the pipeline")

    def gap(start: float | None, end: float) -> float:
        return end - start if start is not None else float("nan")

    vad_stop = run["vad_stop_at"]
    transcript = run["transcript_at"]
    llm_token = run["llm_token_at"]
    tts_audio = run["tts_audio_at"]

    return {
        "vad_stop_seconds": gap(speech_end, vad_stop) if vad_stop else float("nan"),
        "transcript_seconds": gap(speech_end, transcript) if transcript else float("nan"),
        "llm_first_token_seconds": gap(speech_end, llm_token) if llm_token else float("nan"),
        "tts_first_audio_seconds": gap(llm_token, tts_audio) if llm_token and tts_audio else float("nan"),
        "turn_total_seconds": gap(speech_end, tts_audio) if tts_audio else float("nan"),
        # The sub-gaps, which is where the unread time hides.
        "stt_seconds": gap(vad_stop, transcript) if vad_stop and transcript else float("nan"),
        "llm_wait_seconds": gap(transcript, llm_token) if transcript and llm_token else float("nan"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Measure one conversational turn, stage by stage.")
    ap.add_argument("--audio", default=str(DEFAULT_AUDIO))
    ap.add_argument("--backend", choices=["kokoro", "mlx-audio"], default="mlx-audio")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--reply", default=STUB_REPLY)
    ap.add_argument("--token-delay", type=float, default=DEFAULT_TOKEN_DELAY)
    ap.add_argument(
        "--deadline",
        type=float,
        default=None,
        help="override the first-chunk deadline in seconds (0 turns it off)",
    )
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument(
        "--out",
        default=None,
        help="artifact path; defaults to one file per STT transport",
    )
    args = ap.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.exists():
        die(f"no audio at {audio_path}. Download the fixtures with: scripts/voice-ai.sh models")
    pcm, sample_rate = read_wav(audio_path)
    if sample_rate != AUDIO_IN_SAMPLE_RATE:
        die(f"{audio_path} is {sample_rate} Hz; the pipeline takes {AUDIO_IN_SAMPLE_RATE} Hz")

    settings = Settings(_env_file=None, tts_provider=args.backend)
    if args.deadline is not None:
        settings = settings.model_copy(
            update={"first_chunk_deadline_seconds": args.deadline}
        )

    # The recording has to be one turn. A pause longer than the silence
    # window splits it, and the benchmark would then measure the second
    # turn of a two-turn conversation and call it a turn.
    speech_end_index, turns = asyncio.run(
        analyse_turn(pcm, sample_rate, settings.vad_stop_secs)
    )
    if turns != 1:
        die(
            f"{audio_path} is {turns} turns to the VAD, not one: its silence "
            f"window is {settings.vad_stop_secs}s. Pick a recording without an "
            f"internal pause that long."
        )
    if speech_end_index < 0:
        die(f"{audio_path} holds no speech the VAD can see")

    print(f"audio    : {audio_path} ({len(pcm) / 2 / sample_rate:.2f} s)")
    print(f"backend  : {args.backend}")
    print(f"runs     : {args.runs}")
    print(f"reply    : {args.reply!r}\n")

    runs: list[dict] = []
    for index in range(args.runs):
        run = asyncio.run(
            run_once(
                settings,
                pcm,
                sample_rate,
                speech_end_index,
                args.reply,
                args.token_delay,
                args.timeout,
            )
        )
        stages = stages_of(run)
        runs.append(stages)
        print(f"  run {index + 1}: {run['transcript']!r}")
        for name, value in stages.items():
            print(f"      {name:26} {value:.3f}")

    keys = list(runs[0].keys())
    medians = {
        key: round(statistics.median([run[key] for run in runs]), 3) for key in keys
    }

    payload = {
        "audio": str(audio_path.relative_to(REPO_ROOT)),
        "runs": args.runs,
        "backend": args.backend,
        "stt_provider": settings.stt_provider,
        "deadline_seconds": settings.first_chunk_deadline_seconds,
        "stages": medians,
        "samples": [{key: round(run[key], 3) for key in keys} for run in runs],
    }
    # One artifact per STT path by default: the two rows of the README's
    # turn table are different providers, and overwriting one with the
    # other would leave the README's number without the file that
    # produced it. `--out` keeps a variant (the deadline-off run) from
    # replacing the default-deadline artifact.
    out_path = (
        Path(args.out)
        if args.out
        else (
            OUT_PATH_REALTIME
            if settings.stt_provider == REALTIME_PROVIDER
            else OUT_PATH
        )
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")

    print("\n=== medians ===")
    for key, value in medians.items():
        print(f"  {key:26} {value:.3f}")
    print(f"  wrote {out_path}")
    return 0


if __name__ == "__main__":
    main()
