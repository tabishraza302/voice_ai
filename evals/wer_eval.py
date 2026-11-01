#!/usr/bin/env python3
"""Measure STT quality on FLEURS, per language and per model.

**Why this harness exists.** The plan's claims about the STT layer — that
Nemotron is ~5x better than Whisper on Hindi, and that it is good enough
on English that one model can serve both — are claims until this script
measures them. The published numbers come from NVIDIA's model card and
were computed on an H100 with a particular text normalization. This
harness runs on this machine, on the same audio, with the same
normalization, so the numbers in the README are this repo's own.

**Two normalizations, side by side.** They disagree materially on Hindi.
Whisper's default normalization strips Devanagari diacritics and
compresses words (`हमने उस उम्मीदवार को चुना।` -> `हमन उस उमम दव र क चन`),
which *lowers* WER while destroying the meaning. Reporting only the
Whisper-native number would flatter Hindi. The Indic normalization is
reported alongside it.

**One model resident at a time.** The STT server caches every model it
loads and never evicts it, so the harness swaps models through the
server's own `POST`/`DELETE /v1/models` API rather than holding two at
once. It runs its own server on its own port and never touches the
bot's.

Usage:
    uv run python evals/wer_eval.py --languages en hi
    uv run python evals/wer_eval.py --models nemotron nemotron-8bit whisper
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path

# The Hugging Face hub's default read timeout is 10 s, which trips on a
# slow link: a 300 MB parquet file can stall for longer than that between
# chunks and the download fails with "The read operation timed out".
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "evals" / "fixtures"
AUDIO_DIR = FIXTURE_DIR / "audio"
REPORT_PATH = FIXTURE_DIR / "wer_report.json"
LOG_PATH = Path("/tmp/mlx_stt_harness.log")

# The model matrix. Each entry is (name, mlx-audio repo id).
MODELS: dict[str, str] = {
    "nemotron": "mlx-community/nemotron-3.5-asr-streaming-0.6b",
    "nemotron-8bit": "mlx-community/nemotron-3.5-asr-streaming-0.6b-8bit",
    "whisper": "mlx-community/whisper-large-v3-turbo",
    # Mistral's natively streaming ASR. It takes no language hint - the
    # encoder does its own language ID - so the hint this harness sends is
    # ignored by it, and it is measured with auto-detection.
    "voxtral": "mlx-community/Voxtral-Mini-4B-Realtime-2602-4bit",
    # The fp16 conversion (8.87 GB) is deliberately not listed: it does not fit
    # this machine next to Ollama and the agent, so it cannot be measured here.
}

# FLEURS configs, and the language hint sent to the model for each.
LANGUAGES: dict[str, tuple[str, str | None]] = {
    "en": ("en_us", "en-US"),
    "hi": ("hi_in", "hi-IN"),
}

DEVANAGARI_RANGE = (0x0900, 0x097F)


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------


def normalize_standard(text: str) -> str:
    """The standard text normalization for WER.

    OpenAI's `whisper-normalizer`, which is what every published FLEURS
    WER is computed with — case, punctuation and numbers folded. It is
    named "standard" here, not "whisper", because Whisper is also one
    of the models in the table and the two are unrelated.
    """
    try:
        from whisper_normalizer import BasicTextNormalizer

        return BasicTextNormalizer(preserve_marks=True)(text)
    except Exception:  # noqa: BLE001
        return normalize_fallback(text)


def normalize_indic(text: str) -> str:
    """Indic normalization, for Hindi.

    Required, not optional: without it the reference and the hypothesis
    are not comparable in Devanagari, and the resulting WER is a measure
    of the normalizer rather than of the model.
    """
    try:
        from indicnlp import normalize

        return normalize.indic_normalize(text)
    except Exception:  # noqa: BLE001
        return normalize_fallback(text)


def normalize_fallback(text: str) -> str:
    """A dependency-free normalization, used if the optional ones fail."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip().lower()


def normalize_romanized_indic(text: str) -> str:
    """The standard normalization, then both scripts folded to Latin.

    Devanagari and romanized Hindi are the same language, and a model
    that transcribes one but not the other is still a working model: the
    FLEURS reference is in Devanagari, and a model may answer in Latin
    script. The whisper normalization runs first, so this view differs
    from the `whisper` one only in the script — and the WER measures the
    language, not the script.

    `indic-nlp-library` 0.92 cannot do this direction: its `to_itrans`
    drops characters. `indic-transliteration` can, and it is pinned in
    `pyproject.toml` for exactly this.
    """
    try:
        from indic_transliteration import sanscript
    except Exception:  # noqa: BLE001
        return normalize_standard(text)

    folded = []
    for token in normalize_standard(text).split():
        if any(0x0900 <= ord(c) <= 0x097F for c in token):
            token = sanscript.transliterate(token, sanscript.DEVANAGARI, sanscript.ITRANS)
        folded.append(token)
    return " ".join(folded).lower()


NORMALIZERS = {
    "standard": normalize_standard,
    "indic": normalize_indic,
    "romanized": normalize_romanized_indic,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class Clip:
    path: Path
    reference: str
    language: str


def load_clips(languages: list[str], per_language: int) -> list[Clip]:
    """Download FLEURS and write the clips to disk as 16 kHz mono WAV."""
    from datasets import load_dataset

    clips: list[Clip] = []
    for language in languages:
        config, _ = LANGUAGES[language]
        out_dir = AUDIO_DIR / config
        # A clip without its reference transcript next to it is unusable
        # here: WER against an empty reference is a meaningless number.
        cached = [
            path for path in sorted(out_dir.glob("*.wav"))
            if (out_dir / f"{path.stem}.txt").exists()
        ]
        if len(cached) >= per_language:
            print(f"  {config}: using cached clips", flush=True)
            for path in cached[:per_language]:
                clips.append(
                    Clip(path=path, reference=read_reference(out_dir, path.stem), language=language)
                )
            continue

        print(f"  downloading FLEURS {config} ...", flush=True)
        dataset = load_dataset("google/fleurs", config, split="test", streaming=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for row in dataset:
            if count >= per_language:
                break
            audio = row["audio"]
            path = out_dir / f"{count:04d}.wav"
            write_wav(path, audio["array"], audio["sampling_rate"])
            (out_dir / f"{count:04d}.txt").write_text(row["transcription"], encoding="utf-8")
            clips.append(Clip(path=path, reference=row["transcription"], language=language))
            count += 1
        print(f"  {config}: {count} clips", flush=True)
    return clips


def read_reference(directory: Path, stem: str) -> str:
    path = directory / f"{stem}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_wav(path: Path, samples, sample_rate: int) -> None:
    """Write mono audio as 16-bit PCM at 16 kHz."""
    import numpy as np
    import soxr

    array = np.asarray(samples, dtype="float32")
    if array.ndim > 1:
        array = array.mean(axis=1)
    if sample_rate != 16000:
        array = soxr.resample(array, sample_rate, 16000)
    data = (np.clip(array, -1.0, 1.0) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(data.tobytes())


# ---------------------------------------------------------------------------
# The STT server
# ---------------------------------------------------------------------------


def find_free_port(preferred: int, attempts: int = 20) -> int:
    """The first free port at or after `preferred`.

    A server left over from an earlier run holds the port, and the bind
    error only appears in the server's own log — so the harness picks a
    free port instead of failing on a fixed one.
    """
    for port in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"no free port in {preferred}..{preferred + attempts - 1}")


def read_log_tail(lines: int = 20) -> str:
    """The last few lines of the server's log, for the failure message."""
    try:
        content = LOG_PATH.read_text(errors="replace").splitlines()
    except OSError:
        return "(no server log)"
    return "\n".join(content[-lines:])


class SttServer:
    """A private mlx-audio server, so the harness never disturbs the bot."""

    # The server runs under a guard that stops it if the harness dies.
    # A killed harness cannot clean up after itself: its children are
    # reparented to the system and keep running — an orphan holding a
    # port and a resident model, long after the measurement it belonged
    # to. The guard watches its own parent and takes the server down with
    # it.
    _GUARD = """
import os, subprocess, sys, time

child = subprocess.Popen(sys.argv[1:])
parent = os.getppid()
while child.poll() is None:
    if os.getppid() != parent:
        child.terminate()
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()
        sys.exit(0)
    time.sleep(0.5)
sys.exit(child.returncode)
"""

    def __init__(self, port: int = 8021) -> None:
        self.port = port
        self.process: subprocess.Popen | None = None
        self.current_model: str | None = None

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.port = find_free_port(self.port)
        log = open(LOG_PATH, "wb")
        # Its own process group, so the whole tree — the guard, `uv run`
        # and the server it spawns — can be killed at once.
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                self._GUARD,
                "uv",
                "run",
                "mlx_audio.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=REPO_ROOT,
            start_new_session=True,
        )
        atexit.register(self.stop)
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.root}/v1/models", timeout=3) as response:
                    if response.status == 200:
                        print(f"  server up on {self.root}", flush=True)
                        return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
        raise RuntimeError(
            f"the STT server did not start on port {self.port}\n{read_log_tail()}"
        )

    def use_model(self, repo: str) -> None:
        """Load a model, unloading the previous one.

        Load-then-unload, so the two are never both resident: the server
        caches every model it loads and never evicts it.
        """
        if self.current_model == repo:
            return
        # The endpoint takes model_name as a query parameter, not a body.
        query = urllib.parse.urlencode({"model_name": repo})
        request = urllib.request.Request(f"{self.root}/v1/models?{query}", method="POST")
        with urllib.request.urlopen(request, timeout=900) as response:
            response.read()
        if self.current_model:
            query = urllib.parse.urlencode({"model_name": self.current_model})
            # DELETE, not GET: GET /v1/models lists the loaded models and
            # leaves this one resident, and three resident models do not
            # fit on this box.
            request = urllib.request.Request(
                f"{self.root}/v1/models?{query}", method="DELETE"
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    response.read()
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
        self.current_model = repo

    def stop(self) -> None:
        """Stop the server, and everything it spawned.

        The server is its own process group, so the signal goes to the
        group: `uv run` and the server it execs both go down. Signalling
        only `uv run` leaves the server alive, holding its port and its
        resident model.
        """
        if self.process is None:
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self.process.terminate()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.process.kill()
        self.process = None


def transcribe(server: SttServer, model: str, clip: Clip, language: str | None) -> str:
    """POST one clip and return the transcript."""
    boundary = "----voiceaiwer"
    data = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{clip.path.name}"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + clip.path.read_bytes() + b"\r\n"

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

    request = urllib.request.Request(
        f"{server.root}/v1/audio/transcriptions",
        data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=600) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("text", "")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score(references: list[str], hypotheses: list[str]) -> dict:
    """WER and CER for one language and one model."""
    import jiwer

    wer = jiwer.wer(references, hypotheses)
    cer = jiwer.cer(references, hypotheses)
    return {
        "wer": round(wer, 4),
        "cer": round(cer, 4),
        "samples": len(references),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--languages", nargs="+", default=["en", "hi"])
    parser.add_argument("--models", nargs="+", default=["nemotron"])
    parser.add_argument("--per-language", type=int, default=40)
    parser.add_argument("--port", type=int, default=8021)
    args = parser.parse_args()

    unknown = [m for m in args.models if m not in MODELS]
    if unknown:
        print(f"unknown model(s): {unknown}; known: {sorted(MODELS)}", file=sys.stderr)
        return 2

    print(f"models:    {args.models}")
    print(f"languages: {args.languages}")
    print(f"clips:     {args.per_language} per language\n")

    clips = load_clips(args.languages, args.per_language)
    if not clips:
        print("no clips loaded", file=sys.stderr)
        return 1

    server = SttServer(port=args.port)
    server.start()

    results: list[dict] = []
    try:
        for model_name in args.models:
            model = MODELS[model_name]
            print(f"\n=== {model_name} ({model}) ===", flush=True)
            server.use_model(model)

            for language in args.languages:
                _, hint = LANGUAGES[language]
                subset = [c for c in clips if c.language == language]
                if not subset:
                    continue

                hypotheses: list[str] = []
                for clip in subset:
                    text = transcribe(server, model, clip, hint)
                    hypotheses.append(text)
                    print(f"  {clip.path.name}: {text[:60]!r}", flush=True)

                references = [c.reference for c in subset]
                per_normalizer = {}
                for norm_name, normalizer in NORMALIZERS.items():
                    per_normalizer[norm_name] = score(
                        [normalizer(r) for r in references],
                        [normalizer(h) for h in hypotheses],
                    )

                results.append(
                    {
                        "model": model_name,
                        "model_repo": model,
                        "language": language,
                        "normalizations": per_normalizer,
                        "samples": len(subset),
                    }
                )
    finally:
        server.stop()

    report = {
        "results": results,
        "notes": {
            "normalizations": {
                "standard": (
                    "whisper-normalizer BasicTextNormalizer(preserve_marks=True) — "
                    "OpenAI's normalizer, the standard every published FLEURS WER uses"
                ),
                "indic": "indicnlp normalize.indic_normalize",
                "romanized": (
                    "the standard normalization, then both scripts folded to Latin, "
                    "so a model that answers in Latin script is not punished for it"
                ),
            },
            "published_reference": {
                "nemotron_hi_in_wer": 6.81,
                "nemotron_en_us_wer": 7.91,
                "nemotron_auto_detect_hi_in_wer": 8.23,
                "nemotron_auto_detect_en_us_wer": 8.84,
                "nemotron_chunk_80ms_hi_in_wer": 8.13,
                "nemotron_chunk_80ms_auto_detect_hi_in_wer": 11.47,
                "whisper_large_v3_turbo_hi_in_wer": 35.56,
                "whisper_large_v3_turbo_en_us_wer": 4.08,
                "source": "NVIDIA Nemotron 3.5 ASR model card; Whisper figures via NGC/LoRA baselines",
            },
        },
    }
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    print("\n=== WER / CER ===")
    print(f"{'model':<16}{'lang':<6}{'norm':<12}{'WER %':>8}{'CER %':>8}{'n':>5}")
    for row in results:
        for norm_name, scores in row["normalizations"].items():
            print(
                f"{row['model']:<16}{row['language']:<6}{norm_name:<12}"
                f"{scores['wer'] * 100:>8.2f}{scores['cer'] * 100:>8.2f}{scores['samples']:>5}"
            )
    print(f"\nwrote {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
