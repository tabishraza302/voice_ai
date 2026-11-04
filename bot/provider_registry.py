"""Metadata for the speech adapters actually registered by the local runtime.

This module is intentionally data-only so the platform API can expose real
choices without importing MLX, Pipecat services, or model weights.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SpeechKind = Literal["stt", "tts"]

NEMOTRON_MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
KOKORO_MODEL = "kokoro"
MLX_KOKORO_MODEL = "mlx-community/Kokoro-82M-4bit"
CHATTERBOX_MODEL = "mlx-community/chatterbox-multilingual-v3"


@dataclass(frozen=True)
class SecretFieldMetadata:
    name: str
    required: bool = True


@dataclass(frozen=True)
class SpeechVoiceMetadata:
    id: str
    name: str
    language: str
    gender: str
    description: str = ""


@dataclass(frozen=True)
class SpeechModelMetadata:
    id: str
    name: str
    languages: tuple[str, ...] = ()
    voices: tuple[SpeechVoiceMetadata, ...] = ()


@dataclass(frozen=True)
class SpeechProviderMetadata:
    id: str
    kind: SpeechKind
    name: str
    description: str
    models: tuple[SpeechModelMetadata, ...]
    secret_fields: tuple[SecretFieldMetadata, ...] = ()


_KOKORO_VOICES = (
    SpeechVoiceMetadata(
        "af_heart", "Heart (Female, EN)", "en", "female",
        "Warm, natural conversational English female voice",
    ),
    SpeechVoiceMetadata(
        "af_bella", "Bella (Female, EN)", "en", "female",
        "Expressive, clear English female voice",
    ),
    SpeechVoiceMetadata(
        "am_adam", "Adam (Male, EN)", "en", "male",
        "Grounded, articulate English male voice",
    ),
    SpeechVoiceMetadata(
        "am_michael", "Michael (Male, EN)", "en", "male",
        "Professional, authoritative English male voice",
    ),
    SpeechVoiceMetadata(
        "hf_alpha", "Alpha (Female, HI)", "hi", "female",
        "Authentic Hindi female voice with natural prosody",
    ),
    SpeechVoiceMetadata(
        "hf_beta", "Beta (Female, HI)", "hi", "female",
        "Warm Hindi female voice for narrative guidance",
    ),
    SpeechVoiceMetadata(
        "hm_omega", "Omega (Male, HI)", "hi", "male",
        "Deep, confident Hindi male voice",
    ),
    SpeechVoiceMetadata(
        "hm_psi", "Psi (Male, HI)", "hi", "male",
        "Clear, friendly Hindi male voice",
    ),
)

STT_PROVIDER_REGISTRY = (
    SpeechProviderMetadata(
        id="mlx-audio",
        kind="stt",
        name="Batch Nemotron 3.5 (Metal GPU)",
        description="Batch transcription through the local mlx-audio server.",
        models=(
            SpeechModelMetadata(
                id=NEMOTRON_MODEL,
                name="Nemotron 3.5 ASR Streaming 0.6B",
                languages=("auto", "en", "hi"),
            ),
        ),
    ),
    SpeechProviderMetadata(
        id="mlx-audio-realtime",
        kind="stt",
        name="Streaming Nemotron 3.5 (Metal GPU)",
        description="Streaming transcription through the local mlx-audio server.",
        models=(
            SpeechModelMetadata(
                id=NEMOTRON_MODEL,
                name="Nemotron 3.5 ASR Streaming 0.6B",
                languages=("auto",),
            ),
        ),
    ),
    SpeechProviderMetadata(
        id="mlx-whisper",
        kind="stt",
        name="Whisper Large v3 Turbo (Metal GPU)",
        description="In-process MLX Whisper transcription.",
        models=(
            SpeechModelMetadata(
                id=WHISPER_MODEL,
                name="Whisper Large v3 Turbo",
                languages=("auto", "en", "hi"),
            ),
        ),
    ),
)

TTS_PROVIDER_REGISTRY = (
    SpeechProviderMetadata(
        id="kokoro",
        kind="tts",
        name="Kokoro (local ONNX)",
        description="In-process local Kokoro speech synthesis.",
        models=(
            SpeechModelMetadata(
                id=KOKORO_MODEL,
                name="Kokoro",
                voices=_KOKORO_VOICES,
            ),
        ),
    ),
    SpeechProviderMetadata(
        id="mlx-audio",
        kind="tts",
        name="MLX Audio",
        description="Local mlx-audio server speech synthesis.",
        models=(
            SpeechModelMetadata(
                id=MLX_KOKORO_MODEL,
                name="Kokoro 82M (4-bit)",
                voices=_KOKORO_VOICES,
            ),
            SpeechModelMetadata(
                id=CHATTERBOX_MODEL,
                name="Chatterbox Multilingual v3",
            ),
        ),
    ),
)


def provider_registry(kind: SpeechKind) -> tuple[SpeechProviderMetadata, ...]:
    if kind == "stt":
        return STT_PROVIDER_REGISTRY
    if kind == "tts":
        return TTS_PROVIDER_REGISTRY
    raise ValueError(f"Unsupported speech provider kind: {kind!r}")


def provider_for(
    kind: SpeechKind, provider_id: str
) -> SpeechProviderMetadata | None:
    return next(
        (provider for provider in provider_registry(kind) if provider.id == provider_id),
        None,
    )


def model_for(
    kind: SpeechKind, provider_id: str, model_id: str
) -> SpeechModelMetadata | None:
    provider = provider_for(kind, provider_id)
    if provider is None:
        return None
    return next((model for model in provider.models if model.id == model_id), None)


def is_registered_model(kind: SpeechKind, provider_id: str, model_id: str) -> bool:
    return model_for(kind, provider_id, model_id) is not None
