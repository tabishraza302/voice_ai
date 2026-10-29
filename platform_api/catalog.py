"""Fake-safe model and voice catalog loader.

Loads qualified platform models and voices from a generated qualification/catalog JSON.
Never imports GPU weights, MLX runtimes, or backend services during API startup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field

DEFAULT_QUALIFIED_CATALOG = {
    "llm_models": [
        {
            "id": "qwen3.5:4b",
            "name": "Qwen 3.5 4B",
            "description": "Default fast-inference model with suppressed reasoning",
            "supports_reasoning": True,
        },
        {
            "id": "gemma3:4b",
            "name": "Gemma 3 4B",
            "description": "Google Gemma 3 4B parameter model",
            "supports_reasoning": False,
        },
    ],
    "stt_providers": [
        {
            "id": "mlx-audio-realtime",
            "name": "NVIDIA Nemotron 3.5 ASR Streaming 0.6B",
            "languages": ["auto", "en", "hi"],
        },
        {
            "id": "mlx-audio",
            "name": "Nemotron 3.5 ASR Batch",
            "languages": ["auto", "en", "hi"],
        },
    ],
    "tts_voices": [
        {
            "id": "af_heart",
            "name": "Heart (English)",
            "language": "en",
            "gender": "female",
        },
        {
            "id": "hf_alpha",
            "name": "Alpha (Hindi)",
            "language": "hi",
            "gender": "female",
        },
    ],
}


class CatalogModelItem(BaseModel):
    id: str
    name: str
    description: str = ""
    supports_reasoning: bool = False


class CatalogSTTItem(BaseModel):
    id: str
    name: str
    languages: list[str] = Field(default_factory=lambda: ["auto", "en", "hi"])


class CatalogVoiceItem(BaseModel):
    id: str
    name: str
    language: str = "en"
    gender: str = "neutral"


class ModelCatalog(BaseModel):
    llm_models: list[CatalogModelItem]
    stt_providers: list[CatalogSTTItem]
    tts_voices: list[CatalogVoiceItem]
    generation_models: list[CatalogModelItem] = Field(default_factory=list)

def load_catalog(catalog_path: str | Path | None = None) -> ModelCatalog:
    """Load the configured catalog, using the baseline only when none is set.

    Pure file reader; does not contact external services or load GPU weights.
    """
    if catalog_path is None:
        return ModelCatalog.model_validate(DEFAULT_QUALIFIED_CATALOG)

    path = Path(catalog_path)
    if not path.is_file():
        raise ValueError("Configured model catalog file does not exist")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ModelCatalog.model_validate(data)
    except Exception as e:
        raise ValueError(f"Failed to load model catalog from {path}: {e}") from e
