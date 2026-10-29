"""The local model catalog check: loopback only, best-effort, truthful errors."""

from __future__ import annotations

import pytest

from bot.config import Settings
from bot.services import (
    LlmModelNotAvailableError,
    local_llm_tags_url,
    validate_llm_model_available,
)


def test_tags_url_is_loopback_only():
    local = Settings(_env_file=None, llm_provider="ollama", llm_base_url="http://127.0.0.1:11434")
    assert local_llm_tags_url(local) == "http://127.0.0.1:11434/api/tags"

    remote = Settings(_env_file=None, llm_provider="ollama", llm_base_url="http://models.example:11434")
    assert local_llm_tags_url(remote) is None

    openai = Settings(_env_file=None, llm_provider="openai", llm_base_url="http://127.0.0.1:11434")
    assert local_llm_tags_url(openai) is None


@pytest.mark.asyncio
async def test_unlisted_model_is_rejected(monkeypatch):
    async def catalog(settings, *, timeout=2.0):
        return {"qwen3.5:4b", "qwen3:4b"}

    monkeypatch.setattr("bot.services.fetch_local_llm_models", catalog)
    settings = Settings(_env_file=None, llm_model="missing:1b")

    with pytest.raises(LlmModelNotAvailableError, match="missing:1b"):
        await validate_llm_model_available(settings)


@pytest.mark.asyncio
async def test_listed_model_is_accepted(monkeypatch):
    async def catalog(settings, *, timeout=2.0):
        return {"qwen3.5:4b", "qwen3:4b"}

    monkeypatch.setattr("bot.services.fetch_local_llm_models", catalog)
    settings = Settings(_env_file=None, llm_model="qwen3.5:4b")

    await validate_llm_model_available(settings)


@pytest.mark.asyncio
async def test_unreadable_catalog_does_not_reject_the_session(monkeypatch):
    """When the catalog cannot be read, the provider's own error stays truthful."""
    async def catalog(settings, *, timeout=2.0):
        return None

    monkeypatch.setattr("bot.services.fetch_local_llm_models", catalog)
    settings = Settings(_env_file=None, llm_model="anything:1b")

    await validate_llm_model_available(settings)


@pytest.mark.asyncio
async def test_empty_catalog_is_empty_not_unavailable(monkeypatch):
    """An empty models list from Ollama means 0 models are installed, rejecting any model."""
    async def empty_catalog(settings, *, timeout=2.0):
        return set()

    monkeypatch.setattr("bot.services.fetch_local_llm_models", empty_catalog)
    settings = Settings(_env_file=None, llm_model="qwen3.5:4b")

    with pytest.raises(LlmModelNotAvailableError, match="installed: none"):
        await validate_llm_model_available(settings)


@pytest.mark.asyncio
async def test_implicit_latest_compares_equal_to_explicit_latest(monkeypatch):
    """Configured 'qwen3.5' matches catalog 'qwen3.5:latest'."""
    async def catalog(settings, *, timeout=2.0):
        return {"qwen3.5:latest"}

    monkeypatch.setattr("bot.services.fetch_local_llm_models", catalog)
    settings = Settings(_env_file=None, llm_model="qwen3.5")

    await validate_llm_model_available(settings)


@pytest.mark.asyncio
async def test_explicit_latest_compares_equal_to_implicit_latest(monkeypatch):
    """Configured 'qwen3.5:latest' matches catalog 'qwen3.5'."""
    async def catalog(settings, *, timeout=2.0):
        return {"qwen3.5"}

    monkeypatch.setattr("bot.services.fetch_local_llm_models", catalog)
    settings = Settings(_env_file=None, llm_model="qwen3.5:latest")

    await validate_llm_model_available(settings)


@pytest.mark.asyncio
async def test_implicit_latest_with_registry_port_normalizes_final_segment_only(monkeypatch):
    """Colons in registry host:port do not prevent appending implicit :latest to the model tag."""
    async def catalog(settings, *, timeout=2.0):
        return {"registry.example:5000/org/model:latest"}

    monkeypatch.setattr("bot.services.fetch_local_llm_models", catalog)
    settings = Settings(_env_file=None, llm_model="registry.example:5000/org/model")

    await validate_llm_model_available(settings)


@pytest.mark.asyncio
async def test_library_namespace_compares_equal_to_bare_name(monkeypatch):
    """Configured 'library/qwen3.5:4b' matches catalog 'qwen3.5:4b' and vice versa."""
    async def catalog(settings, *, timeout=2.0):
        return {"qwen3.5:4b"}

    monkeypatch.setattr("bot.services.fetch_local_llm_models", catalog)
    settings = Settings(_env_file=None, llm_model="library/qwen3.5:4b")
    await validate_llm_model_available(settings)


@pytest.mark.asyncio
async def test_default_registry_prefix_compares_equal_to_bare_name(monkeypatch):
    """Configured 'registry.ollama.ai/library/qwen3.5:4b' matches catalog 'qwen3.5:4b'."""
    async def catalog(settings, *, timeout=2.0):
        return {"qwen3.5:4b"}

    monkeypatch.setattr("bot.services.fetch_local_llm_models", catalog)
    settings = Settings(_env_file=None, llm_model="registry.ollama.ai/library/qwen3.5:4b")
    await validate_llm_model_available(settings)


@pytest.mark.asyncio
async def test_custom_namespace_does_not_collapse_to_default_library(monkeypatch):
    """'custom/qwen3.5:4b' does not match 'qwen3.5:4b'."""
    async def catalog(settings, *, timeout=2.0):
        return {"qwen3.5:4b"}

    monkeypatch.setattr("bot.services.fetch_local_llm_models", catalog)
    settings = Settings(_env_file=None, llm_model="custom/qwen3.5:4b")
    with pytest.raises(LlmModelNotAvailableError, match="is not installed"):
        await validate_llm_model_available(settings)


@pytest.mark.asyncio
async def test_non_default_registry_host_and_port_do_not_collapse(monkeypatch):
    """Registry host/port must be preserved and distinct."""
    async def catalog(settings, *, timeout=2.0):
        return {"registry.example:5000/org/model:v1"}

    monkeypatch.setattr("bot.services.fetch_local_llm_models", catalog)

    # Different port must not match
    settings_diff_port = Settings(_env_file=None, llm_model="registry.example:5001/org/model:v1")
    with pytest.raises(LlmModelNotAvailableError, match="is not installed"):
        await validate_llm_model_available(settings_diff_port)

    # Different tag must not match
    settings_diff_tag = Settings(_env_file=None, llm_model="registry.example:5000/org/model:v2")
    with pytest.raises(LlmModelNotAvailableError, match="is not installed"):
        await validate_llm_model_available(settings_diff_tag)

    # Omitting registry must not match non-default registry
    settings_no_registry = Settings(_env_file=None, llm_model="org/model:v1")
    with pytest.raises(LlmModelNotAvailableError, match="is not installed"):
        await validate_llm_model_available(settings_no_registry)
