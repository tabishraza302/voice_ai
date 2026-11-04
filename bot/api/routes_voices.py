from fastapi import APIRouter

from bot.provider_registry import provider_registry

router = APIRouter(prefix="/voices", tags=["voices"])

TTS_VOICES = list(
    {
        voice.id: {
            "id": voice.id,
            "name": voice.name,
            "language": voice.language,
            "gender": voice.gender,
            "description": voice.description,
        }
        for provider in provider_registry("tts")
        for model in provider.models
        for voice in model.voices
    }.values()
)


@router.get("")
async def list_voices():
    return {
        "voices": TTS_VOICES,
        "default_voice_en": "af_heart",
        "default_voice_hi": "hf_alpha",
    }
