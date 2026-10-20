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

**The routers.** The factories and the routers live together on purpose:
a provider that has a service but no router is a provider that will
misbehave the first time the language changes. ``build_voice_router`` and
``build_stt_language_router`` therefore resolve through the same provider
name, and the test suite asserts that every registered provider has both.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

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
from bot.stt_service import LocalMlxSTTService
from bot.tts_service import LocalMlxTTSService


# ---------------------------------------------------------------------------
# STT backends
# ---------------------------------------------------------------------------


def build_stt_mlx_audio(settings: Settings) -> STTService:
    """NVIDIA Nemotron 3.5 ASR Streaming 0.6B via the local mlx-audio server.

    The default. Auto-detect is available (``stt_language="auto"``) because
    the subclass removes Pipecat's `language is not None` assertion — see
    ``bot/stt_service.py`` for the other two upstream mismatches it fixes.
    """
    language = _parse_language(settings.stt_language)
    return LocalMlxSTTService(
        model=settings.stt_model,
        base_url=settings.stt_base_url,
        language=language,
        ttfs_p99_latency=settings.ttfs_p99_latency,
    )


def build_stt_mlx_whisper(settings: Settings) -> STTService:
    """Pipecat's MLX Whisper service — the English tier and the A/B baseline.

    Faster on English (4.08 % WER on FLEURS `en_us` against Nemotron's
    7.91 %) and far worse on Hindi (35.56 % against 6.81 %), which is why
    it is a swap rather than the default.
    """
    from pipecat.services.whisper.stt import WhisperSTTServiceMLX

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
        "system_instruction": SYSTEM_PROMPT,
        "temperature": settings.llm_temperature,
        "max_tokens": settings.llm_max_tokens,
    }


def build_llm_ollama(settings: Settings) -> LLMService:
    """Ollama — the default local LLM runtime."""
    return OLLamaLLMService(
        base_url=settings.llm_base_url,
        api_key="ollama",
        settings=OLLamaLLMService.Settings(**_llm_settings(settings)),
    )


def build_llm_openai_compatible(settings: Settings) -> LLMService:
    """Any local OpenAI-compatible server: llama.cpp, vLLM, LM Studio."""
    return OpenAILLMService(
        base_url=settings.llm_base_url,
        api_key="local",
        settings=OpenAILLMService.Settings(**_llm_settings(settings)),
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
    """Build the voice router that matches the configured TTS provider."""
    return select_voice_router(settings)


def build_stt_language_router(settings: Settings) -> SttLanguageRouter:
    """Build the STT language router that matches the configured provider."""
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
