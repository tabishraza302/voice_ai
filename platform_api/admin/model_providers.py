"""Data-only metadata for model-serving providers supported by the platform."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProviderMetadata:
    id: str
    name: str
    runtime_adapter: str


MODEL_PROVIDER_REGISTRY = (
    ModelProviderMetadata(
        id="ollama",
        name="Ollama",
        runtime_adapter="pipecat.services.ollama.llm.OLLamaLLMService",
    ),
)


def model_provider_metadata(provider_id: str) -> ModelProviderMetadata | None:
    """Return metadata for a supported serving provider, without importing its runtime."""
    return next(
        (provider for provider in MODEL_PROVIDER_REGISTRY if provider.id == provider_id),
        None,
    )
