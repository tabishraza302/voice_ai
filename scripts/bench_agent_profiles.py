#!/usr/bin/env python3
"""Measure compiled preset prompts against an already installed local Ollama model."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.agents.compiler import compile_system_prompt
from bot.config import Settings
from bot.db.seeds.presets import DEFAULT_BASE_PROMPT, DEFAULT_PRESET_AGENTS

USER_TURNS = {
    "en": "What is one simple way to reduce household electricity use?",
    "hi": "घर में बिजली बचाने का एक आसान तरीका क्या है?",
}
REQUEST_TIMEOUT_SECONDS = 120


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _set_read_deadline(response, deadline: float) -> None:
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise TimeoutError("profile request exceeded the 120-second deadline")
    response.fp.raw._sock.settimeout(remaining)


def profile_cases(settings: Settings | None = None) -> list[dict]:
    settings = settings or Settings()
    cases = []
    for profile in DEFAULT_PRESET_AGENTS:
        system = compile_system_prompt(DEFAULT_BASE_PROMPT, profile["persona_prompt"])
        for language, user_text in USER_TURNS.items():
            cases.append(
                {
                    "slug": profile["slug"],
                    "language": language,
                    "model": profile["llm_model"],
                    "temperature": profile["llm_temperature"],
                    "max_tokens": profile["llm_max_tokens"],
                    "reasoning_effort": profile["llm_reasoning_effort"],
                    "keep_alive": settings.llm_keep_alive,
                    "system": system,
                    "user": user_text,
                }
            )
    return cases


def local_model_is_installed(base_url: str, model: str) -> bool:
    parsed = urlparse(base_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("profile-bench only supports a local Ollama endpoint")
    root = base_url.rstrip("/").removesuffix("/v1")
    request = urllib.request.Request(f"{root}/api/tags", method="GET")
    try:
        with _local_opener().open(request, timeout=3) as response:
            data = json.load(response)
    except (OSError, urllib.error.URLError):
        raise RuntimeError("local Ollama is unavailable or redirected the request") from None
    return any(item.get("name") == model or item.get("name", "").startswith(model + ":") for item in data.get("models", []))


def measure_case(base_url: str, case: dict) -> dict:
    payload = {
        "model": case["model"],
        "messages": [
            {"role": "system", "content": case["system"]},
            {"role": "user", "content": case["user"]},
        ],
        "temperature": case["temperature"],
        "max_tokens": case["max_tokens"],
        "reasoning_effort": case["reasoning_effort"],
        "keep_alive": case["keep_alive"],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.perf_counter()
    first_content = None
    chunks = []
    deadline = started + REQUEST_TIMEOUT_SECONDS
    try:
        with _local_opener().open(request, timeout=10) as response:
            _set_read_deadline(response, deadline)
            for raw_line in response:
                if time.perf_counter() > deadline:
                    raise TimeoutError("profile request exceeded the 120-second deadline")
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                event = json.loads(line[6:])
                delta = (event.get("choices") or [{}])[0].get("delta", {})
                content = delta.get("content")
                if content:
                    if first_content is None:
                        first_content = time.perf_counter() - started
                    chunks.append(content)
                _set_read_deadline(response, deadline)
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        raise RuntimeError(f"request failed or timed out for {case['slug']}/{case['language']}") from None
    elapsed = time.perf_counter() - started
    reply = "".join(chunks).strip()
    if not reply:
        raise RuntimeError(f"empty spoken reply for {case['slug']}/{case['language']}")
    return {
        "profile": case["slug"],
        "language": case["language"],
        "model": case["model"],
        "temperature": case["temperature"],
        "max_tokens": case["max_tokens"],
        "reasoning_effort": case["reasoning_effort"],
        "first_content_seconds": round(first_content, 3) if first_content is not None else None,
        "total_seconds": round(elapsed, 3),
        "reply_characters": len(reply),
        "reply": reply,
    }


def main() -> int:
    settings = Settings()
    if settings.llm_provider != "ollama":
        raise RuntimeError("profile-bench requires llm_provider=ollama")
    cases = profile_cases(settings)
    for model in sorted({case["model"] for case in cases}):
        if not local_model_is_installed(settings.llm_base_url, model):
            raise RuntimeError(f"model {model!r} is not installed locally; profile-bench never downloads models")
    results = [measure_case(settings.llm_base_url, case) for case in cases]
    destination = Path("data/measurements/agent_profiles.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "machine": os.uname().machine,
        "endpoint": settings.llm_base_url,
        "conditions": "One streamed English and Hindi request per built-in profile; local Ollama; no warm-up or retries; 120-second request timeout.",
        "results": results,
    }
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote six profile prompt measurements to {destination}")
    for result in results:
        print(
            f"{result['profile']} {result['language']}: first content "
            f"{result['first_content_seconds']}s, total {result['total_seconds']}s, "
            f"{result['reply_characters']} reply chars"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        raise SystemExit(f"profile-bench unavailable: {exc}")
