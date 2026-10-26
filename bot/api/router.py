from fastapi import APIRouter
from bot.api.routes_agents import router as agents_router
from bot.api.routes_prompts import router as prompts_router
from bot.api.routes_models import create_models_router, list_models
from bot.api.routes_voices import router as voices_router, KOKORO_VOICES
from bot.config import Settings


def create_api_router(settings: Settings) -> APIRouter:
    api = APIRouter()
    api.include_router(agents_router)
    api.include_router(prompts_router)
    api.include_router(create_models_router(settings))
    api.include_router(voices_router)

    @api.get("/catalog")
    async def get_catalog():
        models_data = await list_models(settings)
        llm_list = []
        for m in models_data["llm_models"]:
            is_qwen35 = "qwen3.5" in m
            llm_list.append({
                "id": m,
                "name": m.replace(":", " ").title(),
                "description": (
                    "Default 4B fast-inference model with suppressed reasoning"
                    if is_qwen35
                    else "Apple Silicon local model via Ollama"
                ),
                "supports_reasoning": is_qwen35 or "r1" in m,
            })
        return {
            "llm_models": llm_list,
            "stt_providers": models_data["stt_providers"],
            "tts_voices": KOKORO_VOICES,
        }

    return api
