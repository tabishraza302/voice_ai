import json

from bot.provider_registry import MLX_KOKORO_MODEL, NEMOTRON_MODEL
from platform_api.agents.config import DEFAULT_AGENT_SETTINGS
from platform_api.agents.validation import validate_catalog_eligibility
from platform_api.catalog import (
    CatalogModelItem,
    CatalogSTTItem,
    CatalogSTTModelItem,
    CatalogTTSModelItem,
    CatalogTTSProviderItem,
    CatalogVoiceItem,
    ModelCatalog,
    load_catalog,
)


STT_NEMOTRON = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
STT_WHISPER = "mlx-community/whisper-large-v3-turbo"
TTS_MLX_KOKORO = "mlx-community/Kokoro-82M-4bit"
TTS_CHATTERBOX = "mlx-community/chatterbox-multilingual-v3"
KOKORO_VOICE_IDS = [
    "af_heart",
    "af_bella",
    "am_adam",
    "am_michael",
    "hf_alpha",
    "hf_beta",
    "hm_omega",
    "hm_psi",
]


def test_unconfigured_catalog_lists_registered_speech_metadata_not_choices() -> None:
    catalog = load_catalog()

    assert catalog.llm_models == []
    assert catalog.generation_models == []
    assert [(provider.id, [model.id for model in provider.models]) for provider in catalog.stt_providers] == [
        ("mlx-audio", [STT_NEMOTRON]),
        ("mlx-audio-realtime", [STT_NEMOTRON]),
        ("mlx-whisper", [STT_WHISPER]),
    ]
    assert [
        (provider.id, model.id, [voice.id for voice in model.voices])
        for provider in catalog.tts_providers
        for model in provider.models
    ] == [
        ("kokoro", "kokoro", KOKORO_VOICE_IDS),
        ("mlx-audio", TTS_MLX_KOKORO, KOKORO_VOICE_IDS),
        ("mlx-audio", TTS_CHATTERBOX, []),
    ]
    assert all(
        not model.selectable
        for provider in catalog.stt_providers
        for model in provider.models
    )
    assert all(
        not model.selectable and not voice.selectable
        for provider in catalog.tts_providers
        for model in provider.models
        for voice in model.voices
    )

    with_only_report_pins = load_catalog(
        qualified_settings=DEFAULT_AGENT_SETTINGS,
        tts_playback_qualified=True,
    )
    assert not any(
        model.selectable
        for provider in with_only_report_pins.stt_providers
        for model in provider.models
    )
    assert not any(
        model.selectable
        for provider in with_only_report_pins.tts_providers
        for model in provider.models
    )


def test_catalog_choices_require_exact_local_catalog_and_capability_pins(tmp_path) -> None:
    path = tmp_path / "qualified.json"
    path.write_text(
        json.dumps(
            {
                "llm_models": [],
                "stt_providers": [
                    {
                        "id": "mlx-whisper",
                        "name": "Qualified Whisper",
                        "models": [
                            {"id": STT_WHISPER, "name": "Whisper", "languages": ["en", "hi"]}
                        ],
                    },
                    {"id": "not-installed", "name": "Not installed"},
                ],
                "tts_providers": [
                    {
                        "id": "kokoro",
                        "name": "Qualified Kokoro",
                        "models": [
                            {
                                "id": "kokoro",
                                "name": "Kokoro",
                                "voices": [
                                    {
                                        "id": "af_heart",
                                        "name": "Heart",
                                        "language": "en",
                                        "gender": "female",
                                    }
                                ],
                            }
                        ],
                    }
                ],
                "tts_voices": [
                    {"id": "af_heart", "name": "Heart", "language": "en", "gender": "female"},
                    {"id": "fictional-voice", "name": "Fictional", "language": "en", "gender": "neutral"},
                ],
                "generation_models": [],
            }
        ),
        encoding="utf-8",
    )
    pins = {
        "stt_provider": "mlx-whisper",
        "stt_model": STT_WHISPER,
        "tts_provider": "kokoro",
        "tts_model": "kokoro",
        "tts_voice_en": "af_heart",
        "tts_voice_hi": "hf_alpha",
    }

    catalog = load_catalog(
        path,
        qualified_settings=pins,
        tts_playback_qualified=True,
    )

    assert [provider.id for provider in catalog.stt_providers] == [
        "mlx-audio",
        "mlx-audio-realtime",
        "mlx-whisper",
    ]
    assert [model.selectable for model in catalog.stt_providers[0].models] == [False]
    assert [model.selectable for model in catalog.stt_providers[2].models] == [True]
    assert [
        (provider.id, model.id, model.selectable)
        for provider in catalog.tts_providers
        for model in provider.models
    ] == [
        ("kokoro", "kokoro", True),
        ("mlx-audio", TTS_MLX_KOKORO, False),
        ("mlx-audio", TTS_CHATTERBOX, False),
    ]
    kokoro_voices = catalog.tts_providers[0].models[0].voices
    assert [(voice.id, voice.selectable) for voice in kokoro_voices] == [
        ("af_heart", True),
        ("af_bella", False),
        ("am_adam", False),
        ("am_michael", False),
        ("hf_alpha", False),
        ("hf_beta", False),
        ("hm_omega", False),
        ("hm_psi", False),
    ]
    assert {voice.id for voice in catalog.tts_voices} == set(KOKORO_VOICE_IDS)

    unqualified_playback = load_catalog(
        path,
        qualified_settings=pins,
        tts_playback_qualified=False,
    )
    assert all(
        not model.selectable and not voice.selectable
        for provider in unqualified_playback.tts_providers
        for model in provider.models
        for voice in model.voices
    )


def test_flat_voice_list_does_not_qualify_provider_or_model(tmp_path) -> None:
    path = tmp_path / "legacy-flat-catalog.json"
    path.write_text(
        json.dumps(
            {
                "stt_providers": [{"id": "mlx-whisper", "name": "Whisper"}],
                "tts_voices": [
                    {"id": "af_heart", "name": "Heart", "language": "en"},
                    {"id": "hf_alpha", "name": "Alpha", "language": "hi"},
                ],
            }
        ),
        encoding="utf-8",
    )

    catalog = load_catalog(
        path,
        qualified_settings={
            "stt_provider": "mlx-whisper",
            "stt_model": STT_WHISPER,
            "tts_provider": "kokoro",
            "tts_model": "kokoro",
            "tts_voice_en": "af_heart",
            "tts_voice_hi": "hf_alpha",
        },
        tts_playback_qualified=True,
    )

    assert not any(
        model.selectable
        for provider in catalog.stt_providers
        for model in provider.models
    )
    assert not any(
        model.selectable
        for provider in catalog.tts_providers
        for model in provider.models
    )
    assert not any(voice.selectable for voice in catalog.tts_voices)


def test_catalog_eligibility_returns_unqualified_provider_diagnostics() -> None:
    diagnostics = validate_catalog_eligibility(DEFAULT_AGENT_SETTINGS, ModelCatalog())

    assert {item.path for item in diagnostics} >= {
        "/llm_model",
        "/stt_provider",
        "/tts_provider",
    }


def test_legacy_model_only_config_requires_explicit_provider() -> None:
    catalog = ModelCatalog(llm_models=[
        CatalogModelItem(id="qwen3.5:4b", name="Qwen", provider_id="ollama", selectable=True)
    ])
    diagnostics = validate_catalog_eligibility({"llm_model": "qwen3.5:4b"}, catalog)
    assert any(item.code == "MODEL_PROVIDER_REQUIRED" for item in diagnostics)


def test_catalog_eligibility_returns_no_diagnostics_for_exact_selectable_pins() -> None:
    catalog = ModelCatalog(
        llm_models=[CatalogModelItem(id="qwen3.5:4b", name="Qwen", provider_id="ollama", provider_name="Ollama", selectable=True)],
        stt_providers=[
            CatalogSTTItem(
                id="mlx-audio",
                name="MLX Audio",
                models=[
                    CatalogSTTModelItem(
                        id=NEMOTRON_MODEL,
                        name="Nemotron",
                        selectable=True,
                    )
                ],
            )
        ],
        tts_providers=[
            CatalogTTSProviderItem(
                id="mlx-audio",
                name="MLX Audio",
                models=[
                    CatalogTTSModelItem(
                        id=MLX_KOKORO_MODEL,
                        name="Kokoro",
                        selectable=True,
                        voices=[
                            CatalogVoiceItem(
                                id="af_heart",
                                name="Heart",
                                selectable=True,
                            ),
                            CatalogVoiceItem(
                                id="hf_alpha",
                                name="Alpha",
                                language="hi",
                                selectable=True,
                            ),
                        ],
                    )
                ],
            )
        ],
    )

    assert validate_catalog_eligibility(DEFAULT_AGENT_SETTINGS, catalog) == []
