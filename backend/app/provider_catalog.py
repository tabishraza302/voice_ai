import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Mapping
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from .auth import current_user
from .models import User
from .redis import RedisResources
from .voice import SUPPORTED_LANGUAGES, SUPPORTED_TTS_MODELS

logger = logging.getLogger(__name__)

router = APIRouter(tags=["providers"])

_PROVIDER_KEY_NAMES = {
    "cartesia": "CARTESIA_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
}
_OPENAI_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CACHE_TTL_SECONDS = 300
_IN_MEMORY_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}

_COMPARE_DELETE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


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
    name: str | None = None
    languages: list[str]
    available: bool
    sample_url: str | None = None


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
    sample_url: str | None = None
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


async def _get_cached_voices(provider: str, resources: RedisResources | None) -> list[TTSVoiceRead] | None:
    if resources is not None:
        try:
            cached_json = await resources.commands.get(resources.key(f"tts:voices:{provider}"))
            if cached_json is not None:
                raw_list = json.loads(cached_json)
                return [TTSVoiceRead(**item) for item in raw_list]
        except Exception as exc:
            logger.debug("Redis cache get error for %s: %s", provider, exc)
            mem = _IN_MEMORY_CACHE.get(provider)
            if mem:
                expiry, mem_list = mem
                if time.monotonic() < expiry:
                    return [TTSVoiceRead(**item) for item in mem_list]
            raise

    mem = _IN_MEMORY_CACHE.get(provider)
    if mem:
        expiry, raw_list = mem
        if time.monotonic() < expiry:
            return [TTSVoiceRead(**item) for item in raw_list]

    return None


async def _fallback_voices(
    provider: str,
    api_key: str,
    resources: RedisResources | None,
) -> list[TTSVoiceRead]:
    mem = _IN_MEMORY_CACHE.get(provider)
    if mem and time.monotonic() < mem[0]:
        return [TTSVoiceRead(**item) for item in mem[1]]
    voices = await _fetch_provider_voices(provider, api_key)
    await _set_cached_voices(provider, voices, resources)
    return voices


async def _set_cached_voices(
    provider: str, voices: list[TTSVoiceRead], resources: RedisResources | None
) -> None:
    raw_list = [v.model_dump() for v in voices]
    _IN_MEMORY_CACHE[provider] = (time.monotonic() + CACHE_TTL_SECONDS, raw_list)

    if resources is not None:
        try:
            await resources.commands.set(
                resources.key(f"tts:voices:{provider}"),
                json.dumps(raw_list),
                ex=CACHE_TTL_SECONDS,
            )
        except Exception as exc:
            logger.debug("Redis cache set error for %s: %s", provider, exc)


async def _fetch_cartesia_voices(api_key: str) -> list[TTSVoiceRead]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            "https://api.cartesia.ai/voices",
            headers={
                "X-API-Key": api_key,
                "Cartesia-Version": "2024-06-10",
            },
        )
        response.raise_for_status()
        data = response.json()
        voices: list[TTSVoiceRead] = []
        for item in data:
            voice_id = item.get("id")
            if not voice_id:
                continue
            raw_lang = item.get("language")
            langs = [raw_lang] if raw_lang and raw_lang in SUPPORTED_LANGUAGES else ["en"]
            voices.append(
                TTSVoiceRead(
                    id=str(voice_id),
                    name=item.get("name"),
                    languages=langs,
                    available=True,
                    sample_url=None,
                )
            )
        return voices


async def _fetch_elevenlabs_voices(api_key: str) -> list[TTSVoiceRead]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": api_key},
        )
        response.raise_for_status()
        data = response.json()
        voices: list[TTSVoiceRead] = []
        for item in data.get("voices", []):
            voice_id = item.get("voice_id")
            if not voice_id:
                continue
            langs: list[str] = []
            labels = item.get("labels") or {}
            lang_label = labels.get("language")
            if lang_label and lang_label in SUPPORTED_LANGUAGES:
                langs.append(lang_label)
            for vl in item.get("verified_languages") or []:
                l = vl.get("language")
                if l and l in SUPPORTED_LANGUAGES and l not in langs:
                    langs.append(l)
            if not langs:
                langs = ["en"]
            preview_url = item.get("preview_url")
            voices.append(
                TTSVoiceRead(
                    id=str(voice_id),
                    name=item.get("name"),
                    languages=langs,
                    available=True,
                    sample_url=preview_url if preview_url else None,
                )
            )
        return voices


async def _fetch_provider_voices(provider: str, api_key: str) -> list[TTSVoiceRead]:
    if provider == "cartesia":
        return await _fetch_cartesia_voices(api_key)
    if provider == "elevenlabs":
        return await _fetch_elevenlabs_voices(api_key)
    return []


async def _tts_provider_catalog(
    env: Mapping[str, str],
    provider: str,
    model_id: str,
    resources: RedisResources | None = None,
) -> TTSProviderRead:
    key_name = _PROVIDER_KEY_NAMES[provider]
    api_key = env.get(key_name, "").strip()
    if not api_key:
        return TTSProviderRead(
            provider=provider,
            available=False,
            error=f"{key_name} is not configured on the server.",
            sample_url=None,
            models=[],
        )

    redis_available = True
    cached_voices: list[TTSVoiceRead] | None = None
    try:
        cached_voices = await _get_cached_voices(provider, resources)
    except Exception as exc:
        logger.debug("Redis unavailable on catalog get for %s: %s", provider, exc)
        redis_available = False

    if cached_voices is not None:
        voices = cached_voices
    else:
        try:
            if resources is not None and redis_available:
                lock_key = resources.key(f"lock:tts:voices:{provider}")
                nonce = secrets.token_hex(16)
                acquired = False
                try:
                    acquired = bool(await resources.commands.set(lock_key, nonce, nx=True, ex=15))
                except Exception as exc:
                    logger.debug("Failed to acquire catalog lock due to Redis error: %s", exc)
                    redis_available = False

                if not redis_available:
                    # Immediate direct fallback on Redis outage — do not enter 3s waiter loop
                    voices = await _fallback_voices(provider, api_key, resources)
                elif acquired:
                    try:
                        rechecked: list[TTSVoiceRead] | None = None
                        try:
                            rechecked = await _get_cached_voices(provider, resources)
                        except Exception:
                            pass
                        if rechecked is not None:
                            voices = rechecked
                        else:
                            voices = await _fetch_provider_voices(provider, api_key)
                            await _set_cached_voices(provider, voices, resources)
                    finally:
                        try:
                            await resources.commands.eval(_COMPARE_DELETE_LUA, 1, lock_key, nonce)
                        except Exception:
                            pass
                else:
                    # Lock is held by another worker; poll every 150ms bounded by remaining 3.0s total budget
                    deadline = time.monotonic() + 3.0
                    voices = None
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        sleep_time = min(0.15, remaining)
                        await asyncio.sleep(sleep_time)

                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break

                        try:
                            cached_json = await asyncio.wait_for(
                                resources.commands.get(resources.key(f"tts:voices:{provider}")),
                                timeout=remaining,
                            )
                            if cached_json is not None:
                                raw_list = json.loads(cached_json)
                                voices = [TTSVoiceRead(**item) for item in raw_list]
                                _IN_MEMORY_CACHE[provider] = (time.monotonic() + CACHE_TTL_SECONDS, raw_list)
                                break
                        except TimeoutError:
                            break
                        except Exception as exc:
                            logger.debug("Redis error during catalog waiter polling: %s", exc)
                            break

                    if voices is None:
                        # Bounded fallback on deadline exhaustion or waiter outage: direct fallback, no another wait
                        voices = await _fallback_voices(provider, api_key, resources)
            else:
                voices = await _fallback_voices(provider, api_key, resources if redis_available else None)
        except httpx.HTTPStatusError as exc:
            error_detail = exc.response.text[:200]
            try:
                parsed = exc.response.json()
                if isinstance(parsed, dict) and "detail" in parsed:
                    detail = parsed["detail"]
                    if isinstance(detail, dict) and "message" in detail:
                        error_detail = detail["message"]
                    elif isinstance(detail, str):
                        error_detail = detail
            except Exception:
                pass
            return TTSProviderRead(
                provider=provider,
                available=False,
                error=f"{provider.capitalize()} API error: {error_detail}",
                sample_url=None,
                models=[],
            )
        except Exception as exc:
            return TTSProviderRead(
                provider=provider,
                available=False,
                error=f"Failed to fetch {provider} voices: {exc}",
                sample_url=None,
                models=[],
            )

    model_available = bool(voices)
    model = TTSModelRead(id=model_id, available=model_available, voices=voices)
    raw_sample = env.get(f"{provider.upper()}_SAMPLE_AUDIO_URL") or env.get(f"{provider.upper()}_SAMPLE_URL")
    sample_url = raw_sample.strip() if isinstance(raw_sample, str) and raw_sample.strip() else None
    return TTSProviderRead(
        provider=provider,
        available=model_available,
        error=None if model_available else f"No voices returned by {provider}.",
        sample_url=sample_url,
        models=[model],
    )


async def _build_provider_catalog(
    env: Mapping[str, str], resources: RedisResources | None = None
) -> ProviderCatalogRead:
    tts_providers: list[TTSProviderRead] = []
    for provider, model_id in SUPPORTED_TTS_MODELS.items():
        tts_providers.append(await _tts_provider_catalog(env, provider, model_id, resources=resources))

    return ProviderCatalogRead(
        openai=_openai_catalog(env),
        tts_providers=tts_providers,
    )


@router.get("/provider-catalog", response_model=ProviderCatalogRead)
async def get_provider_catalog(request: Request, user: Annotated[User, Depends(current_user)]):
    resources = getattr(request.app.state, "redis", None)
    return await _build_provider_catalog(os.environ, resources=resources)
