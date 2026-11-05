import json
import os
import re
from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from .auth import current_user
from .models import User
from .voice import SUPPORTED_LANGUAGES, SUPPORTED_TTS_MODELS

router = APIRouter(tags=["providers"])

_PROVIDER_KEY_NAMES = {
    "cartesia": "CARTESIA_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
}
_OPENAI_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class OpenAIModelRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    available: bool


class OpenAIProviderRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    error: str | None
    models: list[OpenAIModelRead]


class TTSVoiceRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    languages: list[str]
    available: bool


class TTSModelRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    available: bool
    voices: list[TTSVoiceRead]


class TTSProviderRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    available: bool
    error: str | None
    models: list[TTSModelRead]


class ProviderCatalogRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    openai: OpenAIProviderRead
    tts_providers: list[TTSProviderRead]


def _has_key(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name)
    return isinstance(value, str) and bool(value.strip())


def _openai_models(env: Mapping[str, str]) -> tuple[list[str], str | None]:
    raw = env.get("VERIFIED_OPENAI_MODELS")
    if not isinstance(raw, str) or not raw.strip():
        return [], "VERIFIED_OPENAI_MODELS is not configured on the server."

    values = [value.strip() for value in raw.split(",")]
    if any(not _OPENAI_MODEL_ID.fullmatch(value) for value in values) or len(values) != len(set(values)):
        return [], "VERIFIED_OPENAI_MODELS is invalid on the server."
    return values, None


def _tts_voice_catalog(env: Mapping[str, str]) -> tuple[dict[str, dict[str, dict[str, list[str]]]] | None, str | None]:
    raw = env.get("VERIFIED_TTS_VOICE_CATALOG")
    if not isinstance(raw, str) or not raw.strip():
        return None, "The server has no verified TTS voice catalog configured."
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None, "The server's verified TTS voice catalog is invalid."
    if not isinstance(parsed, dict):
        return None, "The server's verified TTS voice catalog is invalid."

    verified: dict[str, dict[str, dict[str, list[str]]]] = {}
    for provider, model_id in SUPPORTED_TTS_MODELS.items():
        if provider not in parsed:
            continue
        provider_catalog = parsed[provider]
        if not isinstance(provider_catalog, dict):
            return None, "The server's verified TTS voice catalog is invalid."
        if model_id not in provider_catalog:
            continue
        model_catalog = provider_catalog[model_id]
        if not isinstance(model_catalog, dict):
            return None, "The server's verified TTS voice catalog is invalid."

        voices: dict[str, list[str]] = {}
        for voice_id, raw_languages in model_catalog.items():
            if (
                not isinstance(voice_id, str)
                or not voice_id
                or len(voice_id) > 256
                or voice_id != voice_id.strip()
                or not voice_id.isprintable()
                or not isinstance(raw_languages, list)
                or any(not isinstance(language, str) for language in raw_languages)
            ):
                return None, "The server's verified TTS voice catalog is invalid."
            voices[voice_id] = list(
                dict.fromkeys(language for language in raw_languages if language in SUPPORTED_LANGUAGES)
            )
        verified.setdefault(provider, {})[model_id] = voices

    return verified, None


def _openai_catalog(env: Mapping[str, str]) -> OpenAIProviderRead:
    models, list_error = _openai_models(env)
    key_available = _has_key(env, "OPENAI_API_KEY")
    available = key_available and list_error is None and bool(models)
    if list_error is not None:
        error = list_error
    elif not key_available:
        error = "OPENAI_API_KEY is not configured on the server."
    else:
        error = None

    return OpenAIProviderRead(
        available=available,
        error=error,
        models=[OpenAIModelRead(id=model, available=key_available) for model in models],
    )


def _tts_provider_catalog(
    env: Mapping[str, str],
    catalog: dict[str, dict[str, dict[str, list[str]]]] | None,
    catalog_error: str | None,
    provider: str,
    model_id: str,
) -> TTSProviderRead:
    if catalog_error is not None:
        return TTSProviderRead(provider=provider, available=False, error=catalog_error, models=[])

    provider_catalog = catalog.get(provider) if catalog is not None else None
    model_catalog = provider_catalog.get(model_id) if provider_catalog is not None else None
    if model_catalog is None:
        return TTSProviderRead(
            provider=provider,
            available=False,
            error=f"No verified {provider} voices are configured on the server.",
            models=[],
        )

    key_name = _PROVIDER_KEY_NAMES[provider]
    key_available = _has_key(env, key_name)
    voices = [
        TTSVoiceRead(id=voice_id, languages=languages, available=key_available and bool(languages))
        for voice_id, languages in model_catalog.items()
    ]
    model_available = key_available and any(voice.available for voice in voices)
    model = TTSModelRead(id=model_id, available=model_available, voices=voices)

    if not key_available:
        error = f"{key_name} is not configured on the server."
    elif not model_available:
        error = f"No verified {provider} voices support the configured call languages."
    else:
        error = None
    return TTSProviderRead(
        provider=provider,
        available=model_available,
        error=error,
        models=[model],
    )


def _build_provider_catalog(env: Mapping[str, str]) -> ProviderCatalogRead:
    tts_catalog, catalog_error = _tts_voice_catalog(env)
    return ProviderCatalogRead(
        openai=_openai_catalog(env),
        tts_providers=[
            _tts_provider_catalog(env, tts_catalog, catalog_error, provider, model_id)
            for provider, model_id in SUPPORTED_TTS_MODELS.items()
        ],
    )


@router.get("/provider-catalog", response_model=ProviderCatalogRead)
async def get_provider_catalog(user: Annotated[User, Depends(current_user)]):
    return _build_provider_catalog(os.environ)
