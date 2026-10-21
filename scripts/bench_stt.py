#!/usr/bin/env python3
"""Measure STT speed on this machine, and write the numbers the bot needs.

Two things are measured, and both matter more than they look:

1. **Real-time factor** (RTF) — inference seconds divided by audio
   seconds. Below 1.0 means the model transcribes faster than speech
   arrives.
2. **Time to final transcript** (TTFS) — how long after the last audio sample
   the final transcript arrives.

The second number is the one that changes the bot's behaviour. Pipecat's
turn-stop strategy waits up to ``ttfs_p99_latency`` for a transcript, and
its built-in default of 1.0 s is a placeholder for unknown hardware. Feeding
the measured p99 in makes the bot stop waiting the moment the transcript
actually arrives, instead of burning a fixed second on every turn.

The benchmark goes through the **same HTTP endpoint the bot uses** — the
local ``mlx_audio.server`` — so it measures the deployed path, not a library
call that bypasses serialization and the model cache.

Usage:
    uv run python scripts/bench_stt.py --languages en hi
    uv run python scripts/bench_stt.py --model mlx-community/whisper-large-v3-turbo
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path

# The Hugging Face hub's default read timeout is 10 s, which trips on a
# slow link: a 300 MB parquet file can stall for longer than that between
# chunks and the download fails with "The read operation timed out". This
# link sustains about 4 MB/s, so give it room.
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "stt_bench.json"
FIXTURE_DIR = REPO_ROOT / "evals" / "fixtures" / "audio"

# The FLEURS configs used by the WER harness, kept in sync so a bench run
# and a WER run score the same audio.
FLEURS_CONFIGS = {
    "en": "en_us",
    "hi": "hi_in",
}

DEFAULT_MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
DEFAULT_BASE_URL = "http://127.0.0.1:8020/v1"


@dataclass
class Clip:
    path: Path
    audio_seconds: float
    language: str
    transcript: str = ""


@dataclass
class Result:
    model: str
    language: str
    samples: int = 0
    audio_seconds: float = 0.0
    inference_seconds: float = 0.0
    ttfs_samples: list[float] = field(default_factory=list)

    @property
    def rtf(self) -> float | None:
        """Real-time factor: seconds of inference per second of audio.

        Below 1.0 means the model transcribes faster than the audio
        plays. It is derived from the two measured totals, so it cannot
        drift out of step with them.
        """
        if self.audio_seconds <= 0:
            return None
        return self.inference_seconds / self.audio_seconds

    @property
    def ttfs_p99(self) -> float | None:
        if not self.ttfs_samples:
            return None
        ordered = sorted(self.ttfs_samples)
        # Nearest-rank p99: with 20 clips, the p99 is the slowest sample.
        idx = max(0, min(len(ordered) - 1, int(round(0.99 * len(ordered))) - 1))
        return ordered[idx]

    @property
    def ttfs_median(self) -> float | None:
        if not self.ttfs_samples:
            return None
        return statistics.median(self.ttfs_samples)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "language": self.language,
            "samples": self.samples,
            "audio_seconds": round(self.audio_seconds, 2),
            "inference_seconds": round(self.inference_seconds, 2),
            "rtf": round(self.rtf, 4) if self.rtf is not None else None,
            "ttfs_p50_seconds": round(self.ttfs_median, 3) if self.ttfs_median else None,
            "ttfs_p99_seconds": round(self.ttfs_p99, 3) if self.ttfs_p99 else None,
        }


def die(msg: str) -> None:
    raise SystemExit(f"bench_stt: {msg}")


def write_wav(path: Path, samples: list[float], sample_rate: int = 16000) -> None:
    """Write mono float samples in [-1, 1] as 16-bit PCM."""
    import numpy as np

    data = (np.clip(np.asarray(samples, dtype="float32"), -1.0, 1.0) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(data.tobytes())


def resample_to_16k(audio: "object", sr: int) -> list[float]:
    """Resample mono audio to 16 kHz. FLEURS is 16 kHz already; this is a guard."""
    import numpy as np
    import soxr

    arr = np.asarray(audio, dtype="float32")
    if arr.ndim > 1:
        arr = arr.mean(axis=1)
    if sr != 16000:
        arr = soxr.resample(arr, sr, 16000)
    return arr.tolist()


def load_fixtures(languages: list[str], per_language: int) -> list[Clip]:
    """Download a slice of FLEURS and write it to disk as 16 kHz WAV.

    A clip is only reusable if its reference transcript sits next to it:
    ``0000.wav`` with ``0000.txt``. The WER harness uses the same
    convention and the same cache rule, so a bench run and a WER run
    score the same audio.
    """
    from datasets import load_dataset

    clips: list[Clip] = []
    for lang in languages:
        config = FLEURS_CONFIGS.get(lang, lang)
        out_dir = FIXTURE_DIR / config

        cached = [
            path for path in sorted(out_dir.glob("*.wav"))
            if (out_dir / f"{path.stem}.txt").exists()
        ]
        if len(cached) >= per_language:
            print(f"  {config}: using {per_language} cached clips", flush=True)
            for path in cached[:per_language]:
                clips.append(
                    Clip(
                        path=path,
                        audio_seconds=audio_duration(path),
                        language=lang,
                        transcript=read_reference(out_dir, path.stem),
                    )
                )
            continue

        print(f"  downloading FLEURS {config} ...", flush=True)
        ds = load_dataset("google/fleurs", config, split="test", streaming=True)
        n = 0
        for row in ds:
            if n >= per_language:
                break
            audio = row["audio"]
            samples = resample_to_16k(audio["array"], audio["sampling_rate"])
            duration = len(samples) / 16000
            # Skip very short clips: they measure decode overhead, not the model.
            if duration < 1.0:
                continue
            path = out_dir / f"{n:04d}.wav"
            write_wav(path, samples)
            reference = row.get("transcription", "")
            (out_dir / f"{n:04d}.txt").write_text(reference, encoding="utf-8")
            clips.append(Clip(path=path, audio_seconds=duration, language=lang,
                              transcript=reference))
            n += 1
        print(f"  {config}: {n} clips", flush=True)
    return clips


def read_reference(directory: Path, stem: str) -> str:
    path = directory / f"{stem}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def audio_duration(path: Path) -> float:
    """Duration of a 16 kHz WAV in seconds, read from its header."""
    import wave

    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def transcribe(base_url: str, model: str, path: Path, language: str | None) -> tuple[str, float]:
    """POST one clip. Returns (text, elapsed_seconds).

    The elapsed time includes the HTTP round trip and the server-side model
    load on the very first call, so the first sample is reported separately
    and excluded from the aggregate.
    """
    boundary = "----voiceaibench"
    filename = path.name
    data = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + path.read_bytes() + b"\r\n"

    fields = {"model": model, "response_format": "json"}
    if language:
        fields["language"] = language
    for key, value in fields.items():
        # Each part needs its own boundary line. Without it the server
        # reads the next part's headers as part of this field's value.
        data += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
        ).encode()
    data += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/audio/transcriptions",
        data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    elapsed = time.perf_counter() - started
    return payload.get("text", ""), elapsed


def wait_for_server(base_url: str, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    root = base_url.rsplit("/v1", 1)[0]
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{root}/v1/models", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--languages", nargs="+", default=["en", "hi"],
                    help="FLEURS configs to bench (default: en hi)")
    ap.add_argument("--model", default=os.environ.get("STT_MODEL", DEFAULT_MODEL))
    ap.add_argument("--base-url", default=os.environ.get("STT_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--per-language", type=int, default=10)
    ap.add_argument("--language-hint", choices=["auto", "en", "hi"], default="auto",
                    help="language field sent to the model (default: auto)")
    args = ap.parse_args()

    print(f"model    : {args.model}")
    print(f"endpoint : {args.base_url}")

    root = args.base_url.rsplit("/v1", 1)[0]
    if not wait_for_server(f"{root}/v1"):
        die(f"no server at {root}. Start it with: scripts/voice-ai.sh stt")
    print("server   : up\n")

    clips = load_fixtures(args.languages, args.per_language)
    if not clips:
        die("no fixtures loaded")

    hint = None if args.language_hint == "auto" else args.language_hint
    results: dict[tuple[str, str], Result] = {}
    first_call = True

    for clip in clips:
        key = (args.model, clip.language)
        res = results.setdefault(key, Result(model=args.model, language=clip.language))
        try:
            text, elapsed = transcribe(args.base_url, args.model, clip.path, hint)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            die(f"HTTP {exc.code} from the STT server: {body}")
        except Exception as exc:  # noqa: BLE001
            die(f"transcription failed for {clip.path.name}: {exc}")

        if first_call:
            # The first request pays the model load. Report it, but do not
            # let it pollute the throughput number.
            print(f"  (first call, includes model load: {elapsed:.2f}s)")
            first_call = False
            continue

        res.samples += 1
        res.audio_seconds += clip.audio_seconds
        res.inference_seconds += elapsed
        res.ttfs_samples.append(elapsed)
        print(f"  {clip.language} {clip.path.name}: {elapsed:.2f}s "
              f"({clip.audio_seconds:.1f}s audio) -> {text[:48]!r}", flush=True)

    payload = {
        "endpoint": args.base_url,
        "language_hint": args.language_hint,
        "results": [r.to_dict() for r in results.values()],
    }
    all_rtf = [r.rtf for r in results.values() if r.rtf is not None]
    all_p99 = [r.ttfs_p99 for r in results.values() if r.ttfs_p99 is not None]
    if all_rtf:
        payload["rtf_worst"] = round(max(all_rtf), 4)
    if all_p99:
        payload["ttfs_p99_worst"] = round(max(all_p99), 3)
        payload["ttfs_p99_worst_language"] = max(
            results.values(), key=lambda r: r.ttfs_p99 or 0.0
        ).language

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    print("\n=== summary ===")
    for r in results.values():
        d = r.to_dict()
        print(f"  {d['language']}: n={d['samples']} rtf={d['rtf']} "
              f"ttfs_p50={d['ttfs_p50_seconds']}s ttfs_p99={d['ttfs_p99_seconds']}s")
    if payload.get("ttfs_p99_worst"):
        print(f"\n  ttfs_p99_latency to configure Pipecat with: {payload['ttfs_p99_worst']}s")
    print(f"  wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
