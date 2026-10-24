#!/usr/bin/env python3
"""Measure one LLM across the quantizations Ollama publishes for it.

The question this answers is the one a deployment actually has: **what
does the precision buy, and what does it cost?** A 4-bit build is
smaller and usually faster; it may also be a different model in
practice. The only way to know is to measure all three axes — speed,
size and what the model actually says — for every build.

Two axes are measured, because Ollama publishes both:

* **Precision** — fp16 (the base build), q8_0 and q4_K_M. Those are
  the three Ollama publishes for this model; there is no 7, 6 or 5-bit
  build to measure.
* **Variant** — the same weights ship as a *hybrid* model that thinks by
  default, an *instruct* model that does not, and a *thinking* model
  that only does. The variant decides whether the model burns the
  latency budget on reasoning before it answers, which is a bigger
  difference than any quantization.

Three paths are measured per build, because the endpoint decides what
the bot experiences:

* `/api/chat` with ``think: false`` — the path that works.
* `/api/chat` with thinking on — what the model does by default.
* `/v1/chat/completions`` — the path the bot actually uses, where
  Ollama ignores ``think``.

Usage:
    uv run python scripts/bench_llm_quant.py
    uv run python scripts/bench_llm_quant.py --tags qwen3:4b-q4_K_M --runs 6
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from loguru import logger

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = REPO_ROOT / "data" / "llm_quant_bench.json"
OUT_LOG = REPO_ROOT / "data" / "llm_quant_bench.log"

# The two Hindi prompts the behavioural evals use, sampled repeatedly.
# A model can hold the persona and still answer in fluent nonsense, and
# a single sample hides that: the replies are recorded verbatim so they
# can be read.
HINDI_PROMPTS = [
    "पिज्ज़ा कैसे बनाते हैं?",
    "नमस्ते, आप कैसे हैं?",
]
HINDI_SAMPLES = 5

OLLAMA = "http://localhost:11434"

# The bot's own system prompt, from bot/config.py. The model is measured
# against the prompt it will actually be given.
SYSTEM = (
    "You are a helpful voice assistant. Reply in the same language the user "
    "speaks: English or Hindi. Keep replies under two sentences. Speak numbers "
    "and units naturally. Never use emoji, markdown, bullet points, or code "
    "formatting — your output is read aloud."
)

QUESTION = "Can you hear me?"
HINDI = "क्या आप मुझे सुन सकते हैं?"

# Every build Ollama publishes for qwen3:4b: the three precisions, of
# each of the three variants.
BUILDS = [
    "qwen3:4b-fp16",
    "qwen3:4b-q8_0",
    "qwen3:4b-q4_K_M",
    "qwen3:4b-instruct-2507-fp16",
    "qwen3:4b-instruct-2507-q8_0",
    "qwen3:4b-instruct-2507-q4_K_M",
]

# A reply that opens by answering the question, rather than talking about
# what it is.
AFFIRMATIVE = re.compile(
    r"^\W*(yes|sure|of course|haan|haan? main|हाँ|हाँ,)", re.IGNORECASE
)


def log_line(handle, text: str) -> None:
    """Write a line to the log file and to stdout."""
    handle.write(text + "\n")
    handle.flush()
    print(text, flush=True)


def pull(tag: str) -> tuple[bool, float, str]:
    """Pull a model. Returns (ok, seconds, output)."""
    started = time.monotonic()
    result = subprocess.run(
        ["ollama", "pull", tag], capture_output=True, text=True, timeout=3600
    )
    return (
        result.returncode == 0,
        time.monotonic() - started,
        (result.stdout + result.stderr).strip(),
    )


def remove(tag: str) -> None:
    """Delete a model, so the sweep does not fill the disk."""
    subprocess.run(["ollama", "rm", tag], capture_output=True, text=True)


def local_tags() -> set[str]:
    """The tags that are already on this disk."""
    out = subprocess.run(["ollama", "list"], capture_output=True, text=True).stdout
    tags = set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if parts and parts[0] != "NAME":
            tags.add(parts[0])
    return tags


def disk_size_gb(tag: str) -> float | None:
    """The size of a published build, in GB.

    The registry manifest is the source: it is the size of the build as
    published, and it does not depend on what happens to be on this disk.
    ``ollama list`` is the fallback when the registry is unreachable; it
    prints the size in two columns ("1.9 GB"), so it is parsed with a
    pattern rather than by column position.
    """
    name, _, build = tag.partition(":")
    request = urllib.request.Request(
        f"https://registry.ollama.ai/v2/library/{name}/manifests/{build}",
        headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
    )
    try:
        manifest = json.load(urllib.request.urlopen(request, timeout=30))
        total = sum(layer.get("size", 0) for layer in manifest.get("layers", []))
        if total:
            return round(total / 1024**3, 2)
    except Exception:  # noqa: BLE001
        pass

    out = subprocess.run(["ollama", "list"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        match = re.match(rf"^{re.escape(tag)}\s+\S+\s+([\d.]+)\s*(GB|MB|KB)\b", line)
        if match:
            value, unit = float(match.group(1)), match.group(2)
            factor = {"GB": 1.0, "MB": 1 / 1024, "KB": 1 / 1024**2}[unit]
            return round(value * factor, 2)
    return None


def resident_gb(tag: str) -> float | None:
    """The resident size of a model in memory, in GB."""
    try:
        models = httpx.get(f"{OLLAMA}/api/ps", timeout=10).json().get("models", [])
    except Exception:  # noqa: BLE001
        return None
    for model in models:
        if model.get("name") == tag or model.get("model") == tag:
            return round(model.get("size", 0) / 1024**3, 2)
    return None


def native_sample(tag: str, prompt: str, think: bool) -> dict:
    """One streamed request on Ollama's native endpoint."""
    body = {
        "model": tag,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        "think": think,
    }
    started = time.monotonic()
    first = None
    text = ""
    thinking = ""
    tokens = 0
    error = None
    with httpx.Client(timeout=900) as client:
        with client.stream("POST", f"{OLLAMA}/api/chat", json=body) as response:
            for line in response.iter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    error = chunk["error"]
                    break
                message = chunk.get("message", {})
                if message.get("thinking"):
                    thinking += message["thinking"]
                if message.get("content"):
                    if first is None:
                        first = time.monotonic() - started
                    text += message["content"]
                if chunk.get("done"):
                    tokens = chunk.get("eval_count", 0)
                    break
    total = time.monotonic() - started
    return {
        "first_token_seconds": round(first, 4) if first else None,
        "total_seconds": round(total, 4),
        "tokens": tokens,
        "reply": text.strip(),
        "thinking_chars": len(thinking),
        # A model that has no thinking mode rejects the request, which
        # is not a measurement of anything.
        "error": error,
    }


def openai_sample(tag: str, prompt: str, reasoning_effort: str | None = None) -> dict:
    """One streamed request on the OpenAI-compatible endpoint the bot uses.

    ``reasoning_effort`` is the OpenAI-standard way to ask a thinking
    model not to think. Ollama's endpoint forwards it; ``think`` and
    ``enable_thinking`` are not forwarded.
    """
    body = {
        "model": tag,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "stream": True,
        # Ollama only sends the token counts when asked for them.
        "stream_options": {"include_usage": True},
    }
    if reasoning_effort:
        body["reasoning_effort"] = reasoning_effort
    started = time.monotonic()
    first = None
    text = ""
    reasoning = ""
    tokens = 0
    with httpx.Client(timeout=900) as client:
        with client.stream("POST", f"{OLLAMA}/v1/chat/completions", json=body) as response:
            for line in response.iter_lines():
                if not line.startswith("data: ") or line[6:].strip() == "[DONE]":
                    continue
                chunk = json.loads(line[6:])
                # The usage-only chunk that ends the stream carries an
                # empty choices list.
                choices = chunk.get("choices") or []
                delta = choices[0].get("delta", {}) if choices else {}
                if delta.get("reasoning"):
                    reasoning += delta["reasoning"]
                if delta.get("content"):
                    if first is None:
                        first = time.monotonic() - started
                    text += delta["content"]
                usage = chunk.get("usage")
                if usage:
                    tokens = usage.get("completion_tokens", 0)
    total = time.monotonic() - started
    return {
        "first_token_seconds": round(first, 4) if first else None,
        "total_seconds": round(total, 4),
        "tokens": tokens,
        "reply": text.strip(),
        "reasoning_chars": len(reasoning),
    }


def summarize_bot_samples(samples: list[dict]) -> dict:
    """The bot's own path, summarized over several samples.

    The bot talks to Ollama over the OpenAI-compatible endpoint, so this
    is the path whose latency the user waits on. It gets the same
    treatment as the native one: a median over warm samples, not a
    single number. The reasoning count is the worst case seen, because
    a model that thinks on this path thinks on every request.
    """
    warm = samples[1:] if len(samples) > 1 else samples
    firsts = [s["first_token_seconds"] for s in warm if s["first_token_seconds"]]
    totals = [s["total_seconds"] for s in warm]
    tokens = [s["tokens"] for s in warm]
    reasoning = [s["reasoning_chars"] for s in warm]
    return {
        "first_token_seconds": round(statistics.median(firsts), 4) if firsts else None,
        "total_seconds": round(statistics.median(totals), 4) if totals else None,
        "tokens": round(statistics.median(tokens), 1) if tokens else None,
        "reasoning_chars": max(reasoning) if reasoning else 0,
        "samples": len(warm),
    }


def measure_build(tag: str, runs: int) -> dict:
    """Measure one build on every path that matters."""
    samples = [native_sample(tag, QUESTION, False) for _ in range(runs)]
    # The first request loads the model, so it is not a sample.
    warm = samples[1:] if len(samples) > 1 else samples

    firsts = [s["first_token_seconds"] for s in warm if s["first_token_seconds"]]
    totals = [s["total_seconds"] for s in warm]
    replies = [s["reply"] for s in warm]
    in_voice = sum(1 for r in replies if AFFIRMATIVE.match(r))

    return {
        "tag": tag,
        "disk_gb": disk_size_gb(tag),
        "resident_gb": resident_gb(tag),
        "samples": samples,
        "median_first_token_seconds": round(statistics.median(firsts), 4) if firsts else None,
        "median_total_seconds": round(statistics.median(totals), 4) if totals else None,
        "median_tokens": round(statistics.median([s["tokens"] for s in warm]), 1) if warm else None,
        "answered_in_voice": f"{in_voice}/{len(replies)}" if replies else None,
        "thinking_on": native_sample(tag, QUESTION, True),
        "bot_endpoint": summarize_bot_samples(
            [openai_sample(tag, QUESTION) for _ in range(runs)]
        ),
        "bot_endpoint_no_think": summarize_bot_samples(
            [openai_sample(tag, QUESTION, reasoning_effort="none") for _ in range(runs)]
        ),
        "hindi": native_sample(tag, HINDI, False),
        "hindi_replies": [
            {
                "prompt": prompt,
                "replies": [
                    openai_sample(tag, prompt, reasoning_effort="none")["reply"]
                    for _ in range(HINDI_SAMPLES)
                ],
            }
            for prompt in HINDI_PROMPTS
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tags", nargs="+", default=BUILDS)
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--keep", action="store_true", help="keep the models on disk")
    ap.add_argument(
        "--out",
        default=str(OUT_JSON),
        help="where to write the results (the transcript goes alongside)",
    )
    args = ap.parse_args()

    out_json = Path(args.out)
    out_log = out_json.with_suffix(".log")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    results = []
    started_at = time.time()
    # What is already on this disk, so the run only removes what it pulled.
    before = local_tags()

    with out_log.open("w") as handle:
        log_line(
            handle,
            f"LLM quantization sweep — {time.strftime('%Y-%m-%d %H:%M:%S')} on this machine",
        )
        log_line(handle, f"runs per build: {args.runs}   endpoint: {OLLAMA}")
        log_line(handle, "")

        for tag in args.tags:
            log_line(handle, f"=== {tag} ===")
            if tag in local_tags():
                # Already on disk: the pull would only re-verify the
                # blobs, which takes minutes and measures nothing.
                log_line(handle, "pull: skipped (already on disk)")
            else:
                logger.info(f"pulling {tag}")
                ok, seconds, output = pull(tag)
                log_line(handle, f"pull: {'ok' if ok else 'FAILED'} in {seconds:.1f}s")
                if not ok:
                    log_line(handle, output[-600:])
                    results.append({"tag": tag, "error": output[-600:]})
                    continue

            # One warm-up request, so the samples are not the model load.
            native_sample(tag, QUESTION, False)

            result = measure_build(tag, args.runs)
            results.append(result)
            log_line(
                handle,
                f"disk {result['disk_gb']} GB   resident {result['resident_gb']} GB",
            )
            log_line(
                handle,
                f"thinking off: first token {result['median_first_token_seconds']}s   "
                f"total {result['median_total_seconds']}s   "
                f"tokens {result['median_tokens']}   in voice {result['answered_in_voice']}",
            )
            for sample in result["samples"]:
                log_line(handle, f"    {sample['reply'][:150]}")
            thinking = result["thinking_on"]
            if thinking.get("error"):
                log_line(handle, f"thinking on: not supported — {thinking['error']}")
            else:
                log_line(
                    handle,
                    f"thinking on: first token {thinking['first_token_seconds']}s   "
                    f"total {thinking['total_seconds']}s   tokens {thinking['tokens']}   "
                    f"reasoning {thinking['thinking_chars']} chars",
                )
            bot = result["bot_endpoint"]
            log_line(
                handle,
                f"bot endpoint (/v1, median of {bot['samples']}): first token "
                f"{bot['first_token_seconds']}s   total {bot['total_seconds']}s   "
                f"tokens {bot['tokens']}   reasoning {bot['reasoning_chars']} chars",
            )
            log_line(handle, f"hindi: {result['hindi']['reply'][:150]}")
            quiet = result["bot_endpoint_no_think"]
            log_line(
                handle,
                f"bot endpoint (/v1, reasoning_effort=none, median of {quiet['samples']}): "
                f"first token {quiet['first_token_seconds']}s   "
                f"total {quiet['total_seconds']}s   "
                f"reasoning {quiet['reasoning_chars']} chars",
            )
            for entry in result["hindi_replies"]:
                log_line(handle, f"hindi reply ({entry['prompt']}):")
                for reply in entry["replies"]:
                    log_line(handle, f"    {reply[:160]}")
            log_line(handle, "")

            if not args.keep and tag not in before:
                # Only remove what this run pulled. A model that was
                # already on disk belongs to the user.
                remove(tag)
                log_line(handle, f"removed {tag} from disk")
                log_line(handle, "")

    payload = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at)),
        "runs_per_build": args.runs,
        "prompt_system": SYSTEM,
        "prompt_question": QUESTION,
        "prompt_hindi": HINDI,
        "builds": results,
    }
    out_json.write_text(json.dumps(payload, indent=2) + "\n")

    print("\n=== summary ===")
    print(f"{'build':34} {'disk':>6} {'first':>8} {'total':>8} {'tokens':>8} {'in voice':>10}")
    for result in results:
        if "error" in result:
            print(f"{result['tag']:34} FAILED")
            continue
        print(
            f"{result['tag']:34} {str(result['disk_gb']):>6} "
            f"{str(result['median_first_token_seconds']):>8} "
            f"{str(result['median_total_seconds']):>8} "
            f"{str(result['median_tokens']):>8} {str(result['answered_in_voice']):>10}"
        )
    print(f"\nwrote {out_json}")
    print(f"wrote {out_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
