"""Throwaway smoke test: synthesize Hindi, transcribe it back.

This is the end-to-end proof that the three upstream mismatches in
bot/stt_service.py are actually fixed:

1. auto-detect must not trip Pipecat's `language is not None` assert;
2. the language key must be Nemotron's, not Whisper's;
3. the response format must be json, not the server's default ndjson.

It also proves the local server serves the model at all.
"""

import asyncio
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PORT = 8023
ROOT = f"http://127.0.0.1:{PORT}"
MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"

HINDI_SENTENCES = [
    "नमस्ते, आप कैसे हैं?",
    "मौसम आज बहुत सुहावना है।",
    "यह एक परीक्षण है।",
]
ENGLISH_SENTENCES = [
    "Hello, how are you today?",
    "The weather is very pleasant today.",
]


def start_server():
    log = open("/tmp/mlx_stt_smoke.log", "wb")
    proc = subprocess.Popen(
        ["uv", "run", "mlx_audio.server", "--host", "127.0.0.1", "--port", str(PORT)],
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=Path(__file__).resolve().parent.parent,
    )
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{ROOT}/v1/models", timeout=3) as r:
                if r.status == 200:
                    print(f"server up on {ROOT}")
                    return proc
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("server did not start")


def synthesize(text: str, path: Path) -> None:
    """Kokoro, straight from the library, at 24 kHz."""
    from kokoro_onnx import Kokoro

    model = Path.home() / ".cache/pipecat/kokoro-onnx/kokoro-v1.0.onnx"
    voices = Path.home() / ".cache/pipecat/kokoro-onnx/voices-v1.0.bin"
    kokoro = Kokoro(str(model), str(voices))
    # The espeak backend needs a full locale for English ("en-us") and the
    # bare code for Hindi ("hi"). Pipecat's own
    # language_to_kokoro_language() produces exactly these.
    lang = "hi" if any(0x0900 <= ord(c) <= 0x097F for c in text) else "en-us"
    voice = "hf_alpha" if lang == "hi" else "af_heart"
    audio, sample_rate = kokoro.create(text, voice=voice, lang=lang, speed=1.0)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    print(f"  synthesized {lang}: {text!r} -> {path.name} ({len(audio)/sample_rate:.1f}s)")


async def main():
    proc = start_server()
    try:
        # Pre-load the model.
        query = urllib.parse.urlencode({"model_name": MODEL})
        req = urllib.request.Request(f"{ROOT}/v1/models?{query}", method="POST")
        with urllib.request.urlopen(req, timeout=900) as r:
            r.read()
        print(f"loaded {MODEL}\n")

        from bot.config import Settings
        from bot.services import build_stt

        settings = Settings(_env_file=None, stt_language="auto",
                            stt_base_url=f"{ROOT}/v1", stt_model=MODEL)
        stt = build_stt(settings)
        print(f"STT: {type(stt).__name__}, language={stt._settings.language!r}\n")

        tmp = Path("/tmp/voiceai_smoke")
        tmp.mkdir(exist_ok=True)

        failures = 0
        for label, sentences in (("HI", HINDI_SENTENCES), ("EN", ENGLISH_SENTENCES)):
            for index, sentence in enumerate(sentences):
                wav = tmp / f"{label}{index}.wav"
                synthesize(sentence, wav)
                transcript = await stt._transcribe(wav.read_bytes())
                text = transcript.text
                devanagari = any(0x0900 <= ord(c) <= 0x097F for c in text)
                wanted_devanagari = label == "HI"
                ok = devanagari == wanted_devanagari
                failures += 0 if ok else 1
                print(f"  [{'ok ' if ok else 'FAIL'}] {label}: {text!r}\n")

        print("SMOKE TEST:", "PASS" if failures == 0 else f"{failures} FAILURES")
        return 1 if failures else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
