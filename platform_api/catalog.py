"""Local speech metadata plus explicit qualification flags for user choices.

This module stays data-only: importing the API must not load ML runtimes,
model weights, or backend services.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from bot.provider_registry import SpeechModelMetadata, SpeechVoiceMetadata, provider_registry


class CatalogModelItem(BaseModel):
    id: str
    name: str
    provider_id: str | None = None
    provider_name: str | None = None
    selectable: bool = False
    description: str = ""
    supports_reasoning: bool = False


class CatalogSTTModelItem(BaseModel):
    id: str
    name: str
    languages: list[str] = Field(default_factory=list)
    selectable: bool = False


class CatalogSTTItem(BaseModel):
    id: str
    name: str
    languages: list[str] = Field(default_factory=list)
    models: list[CatalogSTTModelItem] = Field(default_factory=list)


class CatalogVoiceItem(BaseModel):
    id: str
    name: str
    language: str = "en"
    gender: str = "neutral"
    selectable: bool = False


class CatalogTTSModelItem(BaseModel):
    id: str
    name: str
    voices: list[CatalogVoiceItem] = Field(default_factory=list)
    selectable: bool = False


class CatalogTTSProviderItem(BaseModel):
    id: str
    name: str
    models: list[CatalogTTSModelItem] = Field(default_factory=list)


class ModelCatalog(BaseModel):
    llm_models: list[CatalogModelItem] = Field(default_factory=list)
    stt_providers: list[CatalogSTTItem] = Field(default_factory=list)
    tts_voices: list[CatalogVoiceItem] = Field(default_factory=list)
    generation_models: list[CatalogModelItem] = Field(default_factory=list)
    tts_providers: list[CatalogTTSProviderItem] = Field(default_factory=list)


def _source_stt_provider(source: ModelCatalog | None, provider_id: str) -> CatalogSTTItem | None:
    if source is None:
        return None
    return next((item for item in source.stt_providers if item.id == provider_id), None)


def _source_tts_provider(
    source: ModelCatalog | None, provider_id: str
) -> CatalogTTSProviderItem | None:
    if source is None:
        return None
    return next((item for item in source.tts_providers if item.id == provider_id), None)


def _source_stt_model_allowed(
    source: ModelCatalog | None, provider_id: str, model_id: str
) -> bool:
    if source is None:
        return False
    selected = _source_stt_provider(source, provider_id)
    return bool(
        selected is not None
        and "models" in selected.model_fields_set
        and any(model.id == model_id for model in selected.models)
    )


def _source_tts_model_allowed(
    source: ModelCatalog | None, provider_id: str, model_id: str
) -> bool:
    if source is None:
        return False
    selected_provider = _source_tts_provider(source, provider_id)
    return bool(
        selected_provider is not None
        and "models" in selected_provider.model_fields_set
        and any(model.id == model_id for model in selected_provider.models)
    )


def _source_tts_voice_allowed(
    source: ModelCatalog | None,
    provider_id: str,
    model_id: str,
    voice_id: str,
) -> bool:
    if source is None:
        return False
    selected_provider = _source_tts_provider(source, provider_id)
    if selected_provider is None or "models" not in selected_provider.model_fields_set:
        return False
    selected_model = next(
        (model for model in selected_provider.models if model.id == model_id),
        None,
    )
    return bool(
        selected_model is not None
        and "voices" in selected_model.model_fields_set
        and any(voice.id == voice_id for voice in selected_model.voices)
    )

def _has_exact_pin(
    pins: Mapping[str, str] | None, *, provider_key: str, model_key: str,
    provider_id: str, model_id: str,
) -> bool:
    return bool(
        pins
        and pins.get(provider_key) == provider_id
        and pins.get(model_key) == model_id
    )


def _catalog_voice(
    voice: SpeechVoiceMetadata,
    *,
    selectable: bool,
) -> CatalogVoiceItem:
    return CatalogVoiceItem(
        id=voice.id,
        name=voice.name,
        language=voice.language,
        gender=voice.gender,
        selectable=selectable,
    )


def _catalog_stt_model(
    model: SpeechModelMetadata,
    *,
    languages: list[str],
    selectable: bool,
) -> CatalogSTTModelItem:
    return CatalogSTTModelItem(
        id=model.id,
        name=model.name,
        languages=languages,
        selectable=selectable,
    )


def _catalog_tts_model(
    model: SpeechModelMetadata,
    *,
    source: ModelCatalog | None,
    pins: Mapping[str, str] | None,
    provider_id: str,
    playback_qualified: bool,
) -> CatalogTTSModelItem:
    pinned_voices = {
        voice_id
        for voice_id in (
            pins.get("tts_voice_en") if pins else None,
            pins.get("tts_voice_hi") if pins else None,
        )
        if voice_id
    }
    pair_selectable = (
        playback_qualified
        and bool(model.voices)
        and _source_tts_model_allowed(source, provider_id, model.id)
        and _has_exact_pin(
            pins,
            provider_key="tts_provider",
            model_key="tts_model",
            provider_id=provider_id,
            model_id=model.id,
        )
        and any(
            voice.id in pinned_voices
            and _source_tts_voice_allowed(source, provider_id, model.id, voice.id)
            for voice in model.voices
        )
    )
    voices = [
        _catalog_voice(
            voice,
            selectable=(
                pair_selectable
                and voice.id in pinned_voices
                and _source_tts_voice_allowed(
                    source, provider_id, model.id, voice.id
                )
            ),
        )
        for voice in model.voices
    ]
    return CatalogTTSModelItem(
        id=model.id,
        name=model.name,
        voices=voices,
        selectable=pair_selectable,
    )


def _qualified_stt_providers(
    source: ModelCatalog | None,
    pins: Mapping[str, str] | None,
) -> list[CatalogSTTItem]:
    result: list[CatalogSTTItem] = []
    for provider in provider_registry("stt"):
        selected = _source_stt_provider(source, provider.id)
        qualified_languages = (
            set(selected.languages)
            if selected is not None and "languages" in selected.model_fields_set
            else None
        )
        models: list[CatalogSTTModelItem] = []
        for model in provider.models:
            selected_model = (
                next(
                    (item for item in selected.models if item.id == model.id),
                    None,
                )
                if selected is not None and "models" in selected.model_fields_set
                else None
            )
            language_limit = (
                set(selected_model.languages)
                if selected_model is not None
                and "languages" in selected_model.model_fields_set
                else qualified_languages
            )
            languages = [
                language
                for language in model.languages
                if language_limit is None or language in language_limit
            ]
            models.append(
                _catalog_stt_model(
                    model,
                    languages=languages,
                    selectable=(
                        _source_stt_model_allowed(source, provider.id, model.id)
                        and _has_exact_pin(
                            pins,
                            provider_key="stt_provider",
                            model_key="stt_model",
                            provider_id=provider.id,
                            model_id=model.id,
                        )
                    ),
                )
            )
        provider_languages = list(
            dict.fromkeys(language for model in models for language in model.languages)
        )
        result.append(
            CatalogSTTItem(
                id=provider.id,
                name=provider.name,
                languages=provider_languages,
                models=models,
            )
        )
    return result


def _qualified_tts_providers(
    source: ModelCatalog | None,
    pins: Mapping[str, str] | None,
    *,
    playback_qualified: bool,
) -> list[CatalogTTSProviderItem]:
    result: list[CatalogTTSProviderItem] = []
    for provider in provider_registry("tts"):
        models = [
            _catalog_tts_model(
                model,
                source=source,
                pins=pins,
                provider_id=provider.id,
                playback_qualified=playback_qualified,
            )
            for model in provider.models
        ]
        result.append(
            CatalogTTSProviderItem(
                id=provider.id,
                name=provider.name,
                models=models,
            )
        )
    return result


def _flatten_tts_voices(
    providers: list[CatalogTTSProviderItem],
) -> list[CatalogVoiceItem]:
    voices: dict[str, CatalogVoiceItem] = {}
    for provider in providers:
        for model in provider.models:
            for voice in model.voices:
                existing = voices.get(voice.id)
                if existing is None or (voice.selectable and not existing.selectable):
                    voices[voice.id] = voice
    return list(voices.values())


def _enrich_catalog(
    source: ModelCatalog | None,
    qualified_settings: Mapping[str, str] | None,
    *,
    tts_playback_qualified: bool,
) -> ModelCatalog:
    stt_providers = _qualified_stt_providers(source, qualified_settings)
    tts_providers = _qualified_tts_providers(
        source,
        qualified_settings,
        playback_qualified=tts_playback_qualified,
    )
    return ModelCatalog(
        llm_models=[],
        stt_providers=stt_providers,
        tts_voices=_flatten_tts_voices(tts_providers),
        generation_models=source.generation_models if source else [],
        tts_providers=tts_providers,
    )


def load_catalog(
    catalog_path: str | Path | None = None,
    *,
    qualified_settings: Mapping[str, str] | None = None,
    tts_playback_qualified: bool = False,
) -> ModelCatalog:
    """Load registered local speech metadata and explicitly qualified choices.

    Nested speech entries in the catalog file qualify each provider, model, and
    voice; the deployment report qualifies its exact pins. Missing qualification
    never implies an adapter is installed, ready, or eligible.
    """
    source: ModelCatalog | None = None
    if catalog_path is not None and str(catalog_path).strip():
        path = Path(catalog_path)
        if not path.is_file():
            raise ValueError("Configured model catalog file does not exist")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            source = ModelCatalog.model_validate(data)
        except Exception as exc:
            raise ValueError(f"Failed to load model catalog from {path}") from exc
    return _enrich_catalog(
        source,
        qualified_settings,
        tts_playback_qualified=tts_playback_qualified,
    )


async def catalog_with_managed_llms(catalog: ModelCatalog, db: AsyncSession) -> ModelCatalog:
    """Read current administrator-managed model choices without mutating shared state."""
    from platform_api.admin.ollama_model_routes import list_model_entries
    from platform_api.admin.model_providers import model_provider_metadata

    entries = await list_model_entries(db)
    return catalog.model_copy(update={
        "llm_models": [
            CatalogModelItem(
                id=entry.model_id,
                name=entry.name,
                provider_id=entry.provider_id,
                provider_name=model_provider_metadata(entry.provider_id).name,
                selectable=entry.enabled and entry.qualified,
            )
            for entry in entries
        ],
    })
