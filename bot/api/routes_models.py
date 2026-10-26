import httpx
from fastapi import APIRouter
from urllib.parse import urlsplit
from bot.config import Settings
from bot.agents.schemas import SUPPORTED_STT_PROVIDERS

DEFAULT_LLM_MODELS = ["qwen3.5:4b", "qwen2.5:3b", "llama3.2:3b"]
STT_PROVIDER_CATALOG = [
    {"id": "mlx-audio-realtime", "name": "Streaming Nemotron 3.5 (Metal GPU)", "description": "Streaming transcription through the local mlx-audio server."},
    {"id": "mlx-audio", "name": "Batch Nemotron 3.5 (Metal GPU)", "description": "Batch transcription through the local mlx-audio server."},
    {"id": "mlx-whisper", "name": "Whisper Large v3 Turbo (Metal GPU)", "description": "In-process MLX Whisper transcription."},
]


async def list_models(settings: Settings | None = None):
    settings = settings or Settings()
    llm_models = list(DEFAULT_LLM_MODELS)
    parsed = urlsplit(settings.llm_base_url)
    # Model discovery is strictly local: this project's runtime is air-gapped.
    if settings.llm_provider == "ollama" and parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        tags_url = f"{parsed.scheme}://{parsed.netloc}/api/tags"
    else:
        tags_url = None
    try:
        if tags_url:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(tags_url)
            if resp.status_code == 200:
                payload = resp.json()
                tags = payload.get("models", []) if isinstance(payload, dict) else []
                discovered = [
                    model["name"] for model in tags
                    if isinstance(model, dict) and isinstance(model.get("name"), str) and model["name"]
                ] if isinstance(tags, list) else []
                if discovered:
                    llm_models = discovered
    except Exception:
        pass

    if settings.llm_model not in llm_models:
        llm_models.insert(0, settings.llm_model)

    return {
        "llm_models": llm_models,
        "default_llm_model": settings.llm_model,
        "stt_providers": [provider for provider in STT_PROVIDER_CATALOG if provider["id"] in SUPPORTED_STT_PROVIDERS],
        "default_stt_provider": settings.stt_provider,
    }


def create_models_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/models", tags=["models"])

    @router.get("")
    async def get_models():
        return await list_models(settings)

    return router


router = create_models_router(Settings())
