"""The three registries that make STT, TTS and LLM swappable.

**The contract.** The pipeline is written against Pipecat's
``STTService`` / ``LLMService`` / ``TTSService`` base classes, never
against a concrete class. The observers read Pipecat's ``MetricsFrame``
payloads, which are backend-agnostic. The eval harness talks to the agent
over RTVI, which is also backend-agnostic. So swapping a layer touches
exactly two things: this file and ``bot/config.py``.

**The three registries.** Each maps a provider name to a factory. Each
layer gets one ``build_<layer>`` that looks up the configured provider and
dispatches. An unknown provider raises, listing what is registered — a
typo in ``.env`` must fail loudly at startup, not silently fall back to
the default and leave you wondering why your swap did nothing.

**The routers.** The routers live in ``bot/language.py``, one per
backend, because the settings delta they build is backend-specific. A
provider that has a service but no router is a provider that will
misbehave the first time the language changes. ``build_voice_router`` and
``build_stt_language_router`` therefore resolve through the same provider
name, and the test suite asserts that every registered provider has both.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger
from pipecat.services.llm_service import LLMService
from pipecat.services.ollama.llm import OLLamaLLMService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService, TextAggregationMode
from pipecat.transcriptions.language import Language

from bot.config import SYSTEM_PROMPT, Settings
from bot.language import (
    SttLanguageRouter,
    VoiceRouter,
    select_stt_language_router,
    select_voice_router,
)
from bot.stt_realtime import LocalMlxRealtimeSTTService
from bot.stt_service import LocalMlxSTTService
from bot.tts_service import LocalMlxTTSService

# The two STT checkpoints this repo knows about. They are named here so a
# provider swap that leaves the model behind fails with a clear message
# instead of a checkpoint loader error. Nemotron serves both STT paths:
# the batch endpoint loads it for whole-turn transcription, and
# `/v1/realtime` streams it through `create_streaming_session`.
NEMOTRON_MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
KOKORO_MODEL = "kokoro"
# The two mlx-audio TTS checkpoints this repo knows about. Kokoro-82M is
# the default: same model and same voice packs as the in-process ONNX
# build, on the Metal GPU. Chatterbox is the multilingual escape hatch —
# it clones a voice from reference audio and ignores a voice id.
MLX_KOKORO_MODEL = "mlx-community/Kokoro-82M-4bit"
CHATTERBOX_MODEL = "mlx-community/chatterbox-multilingual-v3"


# ---------------------------------------------------------------------------
# STT backends
# ---------------------------------------------------------------------------


def build_stt_mlx_audio(settings: Settings) -> STTService:
    """NVIDIA Nemotron 3.5 ASR Streaming 0.6B via the local mlx-audio server.

    The batch transport, and the swap when the language must be pinned:
    its OpenAI-compatible endpoint takes a language field, which
    ``/v1/realtime`` does not. Auto-detect is available
    (``stt_language="auto"``) because the subclass removes Pipecat's
    `language is not None` assertion — see ``bot/stt_service.py`` for the
    other two upstream mismatches it fixes.
    """
    settings = resolve_models(settings)
    language = _parse_language(settings.stt_language)
    return LocalMlxSTTService(
        model=settings.stt_model,
        base_url=settings.stt_base_url,
        language=language,
        ttfs_p99_latency=settings.ttfs_p99_latency,
    )


class SttLanguageNotSupportedError(ValueError):
    """The selected STT transport cannot honour the configured language."""


class LlmModelNotAvailableError(ValueError):
    """The configured LLM model is not installed on the local model server."""


def local_llm_tags_url(settings: Settings) -> str | None:
    """Return the loopback Ollama tags URL, or None when discovery does not apply.

    Model discovery stays local: a non-loopback base URL is never queried,
    because this project's runtime is air-gapped.
    """
    if settings.llm_provider != "ollama":
        return None
    parsed = urlsplit(settings.llm_base_url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/api/tags"


def _cut_last(s: str, sep: str) -> tuple[str, str, bool]:
    idx = s.rfind(sep)
    if idx >= 0:
        return s[:idx], s[idx + len(sep):], True
    return s, "", False


def _normalize_ollama_model_name(name: str) -> str:
    """Normalize an Ollama model name into canonical host/namespace/model:tag format.

    Follows Ollama's `types/model/name.go` rules:
    - Tag is extracted after the last ':' if after the last '/'.
    - Model is extracted after the last '/'.
    - Namespace is extracted after the next last '/'.
    - Preceding part is registry host (stripping optional 'scheme://' prefix).
    - Defaults: host 'registry.ollama.ai', namespace 'library', tag 'latest'.
    """
    s = name.strip()
    if not s:
        raise ValueError("empty model name")

    tag = ""
    if s.rfind(":") > s.rfind("/"):
        s, tag, _ = _cut_last(s, ":")
        if not tag:
            raise ValueError(f"invalid ollama model name: explicit empty tag in {name!r}")
    s, model, promised = _cut_last(s, "/")
    if not promised:
        host = "registry.ollama.ai"
        namespace = "library"
        model = s
    else:
        s, namespace, promised = _cut_last(s, "/")
        if not promised:
            host = "registry.ollama.ai"
            namespace = s
        else:
            scheme, sep, rest = s.partition("://")
            host = (rest if sep else scheme) or "registry.ollama.ai"
            namespace = namespace or "library"
    if not model or not namespace or not host:
        raise ValueError(f"invalid ollama model name: {name!r}")

    final_tag = tag or "latest"
    return f"{host.lower()}/{namespace.lower()}/{model.lower()}:{final_tag.lower()}"

async def fetch_local_llm_models(
    settings: Settings, *, timeout: float = 2.0
) -> set[str] | None:
    """Return locally installed model names, or None when the catalog is unavailable.

    ``trust_env=False`` matters: proxy environment variables must not
    redirect a request that is supposed to stay on loopback.
    """
    url = local_llm_tags_url(settings)
    if url is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    return {
        entry["name"]
        for entry in models
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"]
    }


async def validate_llm_model_available(settings: Settings) -> None:
    """Reject a session whose model the local server does not list.

    This is best-effort by design: when the catalog cannot be fetched the
    session is not rejected here, so the provider's own error stays the
    truthful report. When the catalog *is* readable, an unknown model fails
    before the call starts instead of on the first turn.
    """
    try:
        target = _normalize_ollama_model_name(settings.llm_model)
    except ValueError as exc:
        raise LlmModelNotAvailableError(
            f"invalid llm_model {settings.llm_model!r}: {exc}"
        ) from exc

    available = await fetch_local_llm_models(settings)
    if available is not None:
        normalized_available = set()
        for name in available:
            try:
                normalized_available.add(_normalize_ollama_model_name(name))
            except ValueError:
                pass
        if target not in normalized_available:
            installed_desc = ", ".join(sorted(available)) if available else "none"
            raise LlmModelNotAvailableError(
                f"llm_model {settings.llm_model!r} is not installed on the local "
                f"model server; installed: {installed_desc}"
            )


def validate_stt_language(settings: Settings) -> None:
    """Validate that the configured STT provider supports the configured language.

    The realtime backend accepts model, format and turn-detection only — no
    language — so a pinned ``stt_language`` is refused rather than sent and
    ignored. Use ``mlx-audio`` when the language must be pinned.
    """
    if settings.stt_provider == "mlx-audio-realtime" and _parse_language(settings.stt_language) is not None:
        raise SttLanguageNotSupportedError(
            "stt_provider=mlx-audio-realtime has no language switch: "
            "/v1/realtime takes model, format and turn_detection only. "
            "Set stt_language=auto, or use stt_provider=mlx-audio, whose "
            "Nemotron prompt conditioning does take one."
        )


def build_stt_mlx_audio_realtime(settings: Settings) -> STTService:
    """Nemotron over the local server's ``/v1/realtime`` WebSocket.

    Same checkpoint as the batch provider, different transport: one
    WebSocket per session, a commit per turn, text after each commit.
    The session accepts a model, a rate and a turn-detection mode — no
    language — so a pinned ``stt_language`` is refused here rather than
    sent and ignored. Use ``mlx-audio`` when the language must be pinned.
    """
    validate_stt_language(settings)
    settings = resolve_models(settings)
    return LocalMlxRealtimeSTTService(
        model=settings.stt_model,
        base_url=settings.stt_base_url,
        ttfs_p99_latency=settings.ttfs_p99_latency,
    )


def resolve_models(settings: Settings) -> Settings:
    """Pair every provider with the model it serves.

    A swap is one line in `.env` — `stt_provider=mlx-whisper` — and the
    model that line leaves behind belongs to the other provider. The
    factories and the routers read the same settings, so the pairing is
    resolved once, here, and everything downstream sees the model that
    is actually loaded.
    """
    if settings.stt_provider == "mlx-whisper" and settings.stt_model == NEMOTRON_MODEL:
        settings.stt_model = WHISPER_MODEL
    if settings.stt_provider == "mlx-audio-realtime" and settings.stt_model == WHISPER_MODEL:
        # The realtime endpoint serves any streaming checkpoint, but
        # whisper-large-v3-turbo is not one: it would fail at load with
        # "does not support streaming". Nemotron is the streaming model
        # this repo measured.
        settings.stt_model = NEMOTRON_MODEL
    if settings.tts_provider == "mlx-audio" and settings.tts_model == KOKORO_MODEL:
        # The placeholder means "this provider's own model". For mlx-audio
        # that is Kokoro-82M in MLX form. A user who wants the
        # multilingual model instead sets its id explicitly
        # (CHATTERBOX_MODEL), and an explicit id passes through untouched.
        settings.tts_model = MLX_KOKORO_MODEL
    if settings.tts_provider == "mlx-audio" and settings.tts_model == CHATTERBOX_MODEL:
        # Chatterbox has no voice packs: it clones a voice from reference
        # audio and ignores a voice id. Clearing the ids keeps the
        # request honest.
        settings.tts_voice_en = None
        settings.tts_voice_hi = None
    return settings


def build_stt_mlx_whisper(settings: Settings) -> STTService:
    """Pipecat's MLX Whisper service — the English tier and the A/B baseline.

    Faster on English (4.08 % WER on FLEURS `en_us` against Nemotron's
    7.91 %) and far worse on Hindi (35.56 % against 6.81 %), which is why
    it is a swap rather than the default.
    """
    from pipecat.services.whisper.stt import WhisperSTTServiceMLX

    settings = resolve_models(settings)
    language = _parse_language(settings.stt_language)
    return WhisperSTTServiceMLX(
        settings=WhisperSTTServiceMLX.Settings(
            model=settings.stt_model,
            language=language,
            temperature=0.0,
        ),
        ttfs_p99_latency=settings.ttfs_p99_latency,
    )


STT_BACKENDS: dict[str, Callable[[Settings], STTService]] = {
    "mlx-audio": build_stt_mlx_audio,
    "mlx-audio-realtime": build_stt_mlx_audio_realtime,
    "mlx-whisper": build_stt_mlx_whisper,
}


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------


def _llm_settings(settings: Settings) -> dict[str, Any]:
    """The LLM settings every backend shares.

    The system prompt lives here, not in either factory, so that swapping
    the LLM cannot silently change what the bot says. Note that
    `system_instruction` and a context `system` message must not both be
    set: the constructor's wins and Pipecat logs a warning.
    """
    return {
        "model": settings.llm_model,
        "system_instruction": getattr(settings, "system_instruction", SYSTEM_PROMPT),
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }


def _openai_llm_settings(settings: Settings) -> dict[str, Any]:
    """The LLM settings for an OpenAI-compatible server.

    One thing differs from the other backends: the name of the length
    cap. "max_tokens" is what llama.cpp, vLLM and LM Studio accept; the
    gpt-5 family rejects it outright and answers nothing. The name is a
    setting because it is a property of the server, not of the code.
    """
    llm_settings = _llm_settings(settings)
    llm_settings.pop("max_tokens", None)
    llm_settings[settings.llm_max_tokens_parameter] = settings.llm_max_tokens
    return llm_settings


def build_llm_ollama(settings: Settings) -> LLMService:
    """Ollama — the default local LLM runtime.

    `keep_alive` and the reasoning policy travel in `extra_body`, which
    Pipecat merges into the request JSON as-is. Ollama's OpenAI-compatible
    endpoint honours both; it ignores `think`, so `reasoning_effort` is
    the only way to stop a thinking model from spending the whole reply
    budget on hidden reasoning. They cannot be top-level parameters —
    the OpenAI client rejects keyword arguments it does not know. The
    other OpenAI-compatible servers do not know the fields, so they are
    not sent to them.
    """
    llm_settings = _llm_settings(settings)
    extra_body = {"keep_alive": settings.llm_keep_alive}
    if settings.llm_reasoning_effort:
        extra_body["reasoning_effort"] = settings.llm_reasoning_effort
    llm_settings["extra"] = {"extra_body": extra_body}
    return OLLamaLLMService(
        base_url=settings.llm_base_url,
        api_key="ollama",
        settings=OLLamaLLMService.Settings(**llm_settings),
    )


def build_llm_openai_compatible(settings: Settings) -> LLMService:
    """Any OpenAI-compatible server: llama.cpp, vLLM, LM Studio, or a cloud API.

    The credential is the layer's, not the provider's: a local server
    ignores it, and a cloud API needs it. "local" is what the local
    servers were sent before there was a setting for it.
    """
    return OpenAILLMService(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "local",
        settings=OpenAILLMService.Settings(**_openai_llm_settings(settings)),
    )


LLM_BACKENDS: dict[str, Callable[[Settings], LLMService]] = {
    "ollama": build_llm_ollama,
    "openai-compatible": build_llm_openai_compatible,
}


# ---------------------------------------------------------------------------
# TTS backends
# ---------------------------------------------------------------------------


def build_tts_kokoro(settings: Settings) -> TTSService:
    """Kokoro — the default. In-process ONNX, no server, no API key."""
    from pipecat.services.kokoro.tts import KokoroTTSService

    return KokoroTTSService(
        settings=KokoroTTSService.Settings(
            voice=settings.tts_voice_en,
            language=Language.EN,
            speed=settings.tts_speed,
        ),
        text_aggregation_mode=TextAggregationMode.TOKEN,
    )


def build_tts_mlx_audio(settings: Settings) -> TTSService:
    """Any mlx-audio TTS model, including the multilingual Chatterbox.

    This is the escape hatch for the TTS layer's weakest point: Kokoro's
    Hindi voices are grade C, and if they turn out to be unintelligible
    the fix is a different model, not a different framework.
    `mlx-community/chatterbox-multilingual-v3` is the intended one.
    """
    settings = resolve_models(settings)
    return LocalMlxTTSService(
        model=settings.tts_model,
        base_url=settings.tts_base_url,
        sample_rate=24000,
        settings=LocalMlxTTSService.Settings(
            model=settings.tts_model,
            language=Language.EN,
            speed=settings.tts_speed,
        ),
        text_aggregation_mode=TextAggregationMode.TOKEN,
    )


TTS_BACKENDS: dict[str, Callable[[Settings], TTSService]] = {
    "kokoro": build_tts_kokoro,
    "mlx-audio": build_tts_mlx_audio,
}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _dispatch(
    registry: dict[str, Callable[[Settings], Any]],
    provider: str,
    layer: str,
    settings: Settings,
) -> Any:
    if provider not in registry:
        raise ValueError(
            f"unknown {layer}_provider {provider!r}; "
            f"registered: {sorted(registry)}"
        )
    logger.info(f"{layer} provider: {provider}")
    return registry[provider](settings)


def build_stt(settings: Settings) -> STTService:
    """Build the configured STT service."""
    return _dispatch(STT_BACKENDS, settings.stt_provider, "stt", settings)


def build_llm(settings: Settings) -> LLMService:
    """Build the configured LLM service."""
    return _dispatch(LLM_BACKENDS, settings.llm_provider, "llm", settings)


def build_tts(settings: Settings) -> TTSService:
    """Build the configured TTS service."""
    return _dispatch(TTS_BACKENDS, settings.tts_provider, "tts", settings)


def build_voice_router(settings: Settings) -> VoiceRouter:
    """Build the voice router that matches the configured TTS provider.

    The models are resolved first, because the router's delta carries the
    model id: it must be the one that is actually loaded, not the
    placeholder.
    """
    settings = resolve_models(settings)
    return select_voice_router(settings)


def build_stt_language_router(settings: Settings) -> SttLanguageRouter:
    """Build the STT language router that matches the configured provider."""
    settings = resolve_models(settings)
    return select_stt_language_router(settings)


def _parse_language(value: str) -> Language | None:
    """Turn the ``stt_language`` setting into a Language (or None for auto)."""
    normalized = (value or "auto").strip().lower()
    if normalized in ("auto", "none", ""):
        return None
    try:
        return Language(normalized)
    except ValueError:
        # Accept regional forms like "hi-in" as well as "hi".
        for language in Language:
            if language.value.lower() == normalized:
                return language
        raise ValueError(
            f"unknown stt_language {value!r}; use 'auto', or a Pipecat language code"
        ) from None
