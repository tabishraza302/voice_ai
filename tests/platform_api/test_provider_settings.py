from platform_api.agents.config import (
    compute_model_hash,
    compute_settings_hash,
    compute_voice_hash,
    extract_settings,
)


def test_platform_defaults_pin_runtime_provider_and_model_ids() -> None:
    settings = extract_settings({})

    assert settings == {
        "llm_provider": "ollama",
        "llm_model": "qwen3.5:4b",
        "stt_provider": "mlx-audio",
        "stt_model": "mlx-community/nemotron-3.5-asr-streaming-0.6b",
        "tts_provider": "mlx-audio",
        "tts_model": "mlx-community/Kokoro-82M-4bit",
        "tts_voice_en": "af_heart",
        "tts_voice_hi": "hf_alpha",
    }


def test_model_and_voice_hashes_cover_provider_model_and_voice_pins() -> None:
    base = extract_settings({})
    changed_stt = {**base, "stt_model": "mlx-community/whisper-large-v3-turbo"}
    changed_tts_provider = {**base, "tts_provider": "kokoro"}
    changed_tts_model = {**base, "tts_model": "kokoro"}
    changed_voice = {**base, "tts_voice_en": "af_bella"}

    assert compute_model_hash(base) != compute_model_hash(changed_stt)
    assert compute_voice_hash(base) != compute_voice_hash(changed_tts_provider)
    assert compute_voice_hash(base) != compute_voice_hash(changed_tts_model)
    assert compute_voice_hash(base) != compute_voice_hash(changed_voice)
    assert compute_settings_hash(base) != compute_settings_hash(changed_stt)
    assert compute_settings_hash(base) != compute_settings_hash(changed_tts_provider)
    assert compute_settings_hash(base) != compute_settings_hash(changed_tts_model)


def test_text_only_configuration_preserves_exact_provider_model_pins() -> None:
    settings = extract_settings(
        {
            "tts_voice_en": "",
            "tts_voice_hi": "",
            "tts_provider": "kokoro",
            "tts_model": "kokoro",
        }
    )

    assert settings["tts_provider"] == "kokoro"
    assert settings["tts_model"] == "kokoro"
    assert settings["tts_voice_en"] == ""
    assert settings["tts_voice_hi"] == ""
