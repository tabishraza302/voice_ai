from bot.provider_registry import (
    CHATTERBOX_MODEL,
    STT_PROVIDER_REGISTRY,
    TTS_PROVIDER_REGISTRY,
    is_registered_model,
    provider_registry,
)
from bot.services import STT_BACKENDS, TTS_BACKENDS


def test_provider_metadata_matches_live_adapter_registries() -> None:
    assert {provider.id for provider in STT_PROVIDER_REGISTRY} == set(STT_BACKENDS) == {
        "mlx-audio",
        "mlx-audio-realtime",
        "mlx-whisper",
    }
    assert {provider.id for provider in TTS_PROVIDER_REGISTRY} == set(TTS_BACKENDS) == {
        "kokoro",
        "mlx-audio",
    }
    assert all(not provider.secret_fields for provider in STT_PROVIDER_REGISTRY)
    assert all(not provider.secret_fields for provider in TTS_PROVIDER_REGISTRY)


def test_model_compatibility_contains_only_runtime_supported_pairs() -> None:
    assert is_registered_model(
        "stt", "mlx-audio", "mlx-community/nemotron-3.5-asr-streaming-0.6b"
    )
    assert is_registered_model(
        "stt", "mlx-audio-realtime", "mlx-community/nemotron-3.5-asr-streaming-0.6b"
    )
    assert is_registered_model(
        "stt", "mlx-whisper", "mlx-community/whisper-large-v3-turbo"
    )
    assert is_registered_model("tts", "kokoro", "kokoro")
    assert is_registered_model(
        "tts", "mlx-audio", "mlx-community/Kokoro-82M-4bit"
    )
    assert not is_registered_model("stt", "mlx-whisper", "mlx-community/nemotron-3.5-asr-streaming-0.6b")
    assert not is_registered_model("tts", "kokoro", "mlx-community/Kokoro-82M-4bit")
    assert is_registered_model("tts", "mlx-audio", CHATTERBOX_MODEL)
    assert not is_registered_model("stt", "cloud-stt", "cloud-model")


def test_voices_are_grouped_by_registered_tts_provider_and_model() -> None:
    for provider in provider_registry("tts"):
        assert provider.models
        for model in provider.models:
            if model.id == CHATTERBOX_MODEL:
                assert model.voices == ()
                continue
            assert model.id in {"kokoro", "mlx-community/Kokoro-82M-4bit"}
            assert [(voice.id, voice.language) for voice in model.voices] == [
                ("af_heart", "en"),
                ("af_bella", "en"),
                ("am_adam", "en"),
                ("am_michael", "en"),
                ("hf_alpha", "hi"),
                ("hf_beta", "hi"),
                ("hm_omega", "hi"),
                ("hm_psi", "hi"),
            ]
