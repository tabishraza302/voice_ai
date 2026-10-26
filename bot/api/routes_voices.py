from fastapi import APIRouter

router = APIRouter(prefix="/voices", tags=["voices"])

KOKORO_VOICES = [
    {"id": "af_heart", "name": "Heart (Female, EN)", "language": "en", "gender": "female", "description": "Warm, natural conversational English female voice"},
    {"id": "af_bella", "name": "Bella (Female, EN)", "language": "en", "gender": "female", "description": "Expressive, clear English female voice"},
    {"id": "am_adam", "name": "Adam (Male, EN)", "language": "en", "gender": "male", "description": "Grounded, articulate English male voice"},
    {"id": "am_michael", "name": "Michael (Male, EN)", "language": "en", "gender": "male", "description": "Professional, authoritative English male voice"},
    {"id": "hf_alpha", "name": "Alpha (Female, HI)", "language": "hi", "gender": "female", "description": "Authentic Hindi female voice with natural prosody"},
    {"id": "hf_beta", "name": "Beta (Female, HI)", "language": "hi", "gender": "female", "description": "Warm Hindi female voice for narrative guidance"},
    {"id": "hm_omega", "name": "Omega (Male, HI)", "language": "hi", "gender": "male", "description": "Deep, confident Hindi male voice"},
    {"id": "hm_psi", "name": "Psi (Male, HI)", "language": "hi", "gender": "male", "description": "Clear, friendly Hindi male voice"},
]


@router.get("")
async def list_voices():
    return {
        "voices": KOKORO_VOICES,
        "default_voice_en": "af_heart",
        "default_voice_hi": "hf_alpha",
    }
