"""Tests for the local mlx-audio STT service.

The three upstream mismatches this service exists to fix are all silent
at import time and loud only in the browser, so they are pinned here:

1. auto-detect must not trip Pipecat's `language is not None` assertion;
2. the language code must be Nemotron's prompt key, not a Whisper code;
3. the request must ask for JSON, because the server defaults to ndjson.
"""

from __future__ import annotations

import pytest
from pipecat.transcriptions.language import Language

from bot.config import Settings
from bot.services import build_stt, build_stt_language_router
from bot.stt_service import LocalMlxSTTService


@pytest.fixture
def service() -> LocalMlxSTTService:
    settings = Settings(_env_file=None)
    return build_stt(settings)


class TestLanguageMapping:
    def test_hindi_uses_the_nemotron_prompt_key(self, service):
        # "hi-IN", not the Whisper "hi". Both are valid Nemotron keys, but
        # the point is that the mapping is Nemotron's, not Whisper's.
        assert service.language_to_service_language(Language.HI) == "hi-IN"

    def test_english_uses_the_nemotron_prompt_key(self, service):
        assert service.language_to_service_language(Language.EN) == "en-US"

    def test_auto_is_the_auto_prompt_key(self, service):
        assert service.language_to_service_language(None) == "auto"


class TestAutoDetect:
    def test_auto_detect_survives_construction(self):
        # Pipecat's stock OpenAISTTService asserts language is not None,
        # which would make auto-detect unreachable. The stored value stays
        # None: None is what "auto-detect" means to this service.
        settings = Settings(_env_file=None, stt_language="auto")
        stt = build_stt(settings)
        assert stt._settings.language is None

    def test_forced_language_is_stored_as_the_service_native_string(self):
        # Pipecat converts the Language enum through
        # language_to_service_language() at construction time, so the
        # stored value is Nemotron's prompt key, not the enum.
        settings = Settings(_env_file=None, stt_language="hi")
        stt = build_stt(settings)
        assert stt._settings.language == "hi-IN"


class TestRequestShape:
    async def test_request_asks_for_json_and_sends_the_model(self, service, monkeypatch):
        """The server requires `model`, and defaults to ndjson."""
        captured: dict = {}

        class FakeResponse:
            text = "नमस्ते"

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse()

        monkeypatch.setattr(service._client.audio.transcriptions, "create", fake_create)

        await service._transcribe(b"RIFFfakewav")

        assert captured["model"] == Settings(_env_file=None).stt_model
        assert captured["response_format"] == "json"
        assert captured["language"] == "auto"  # auto-detect
        assert captured["file"][0] == "audio.wav"

    async def test_forced_hindi_sends_the_prompt_key(self, monkeypatch):
        settings = Settings(_env_file=None, stt_language="hi")
        stt = build_stt(settings)
        captured: dict = {}

        class FakeResponse:
            text = "नमस्ते"

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return FakeResponse()

        monkeypatch.setattr(stt._client.audio.transcriptions, "create", fake_create)

        await stt._transcribe(b"RIFFfakewav")

        assert captured["language"] == "hi-IN"


class TestRouter:
    def test_router_delta_is_the_services_own_settings_type(self):
        settings = Settings(_env_file=None)
        router = build_stt_language_router(settings)
        delta = router.delta_for(Language.HI)
        assert isinstance(delta, LocalMlxSTTService.Settings)
        assert delta.language == Language.HI
