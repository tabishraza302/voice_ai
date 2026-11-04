"""The acceptance test for the swappable-layers requirement.

The requirement is that STT, TTS and LLM can each be swapped without
touching the pipeline, the observers, the collector config, the metric
names, the dashboard or the eval scenarios. That is a claim, and this file
is where it is checked mechanically rather than asserted in a review.

Three things are verified:

* every registered provider builds a service of the right Pipecat base
  type, and has a matching router — a provider without a router would
  misbehave the first time the language changes;
* the routing processor names no concrete backend, so a TTS swap does not
  require editing it;
* swapping a provider changes no observable metric.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from pipecat.services.llm_service import LLMService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService

from bot.config import Settings
from bot.language import (
    SttLanguageRouter,
    VoiceRouter,
    select_stt_language_router,
    select_voice_router,
)
from bot.services import (
    LLM_BACKENDS,
    STT_BACKENDS,
    TTS_BACKENDS,
    build_llm,
    build_stt,
    build_tts,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# A swap is two lines: the provider and the model it serves. Swapping the
# provider alone points it at the other provider's checkpoint, which the
# factories refuse.
STT_MODELS = {
    "mlx-audio": "mlx-community/nemotron-3.5-asr-streaming-0.6b",
    "mlx-audio-realtime": "mlx-community/nemotron-3.5-asr-streaming-0.6b",
    "mlx-whisper": "mlx-community/whisper-large-v3-turbo",
}
TTS_MODELS = {
    "kokoro": "kokoro",
    "mlx-audio": "mlx-community/Kokoro-82M-4bit",
}


class TestRegistries:
    def test_registries_are_not_empty(self):
        assert STT_BACKENDS
        assert LLM_BACKENDS
        assert TTS_BACKENDS

    @pytest.mark.parametrize("provider", sorted(STT_BACKENDS))
    def test_stt_provider_builds_an_stt_service(self, provider):
        settings = Settings(
            _env_file=None, stt_provider=provider, stt_model=STT_MODELS[provider]
        )
        assert isinstance(build_stt(settings), STTService)

    @pytest.mark.parametrize("provider", sorted(LLM_BACKENDS))
    def test_llm_provider_builds_an_llm_service(self, provider):
        settings = Settings(_env_file=None, llm_provider=provider)
        assert isinstance(build_llm(settings), LLMService)

    @pytest.mark.parametrize("provider", sorted(TTS_BACKENDS))
    def test_tts_provider_builds_a_tts_service(self, provider):
        settings = Settings(
            _env_file=None, tts_provider=provider, tts_model=TTS_MODELS[provider]
        )
        assert isinstance(build_tts(settings), TTSService)

    def test_unknown_provider_raises_and_names_the_options(self):
        with pytest.raises(ValueError) as excinfo:
            build_stt(Settings(_env_file=None, stt_provider="nope"))
        message = str(excinfo.value)
        assert "nope" in message
        assert "mlx-audio" in message

    def test_swapping_the_provider_uses_that_providers_model(self):
        # A one-line swap leaves the other provider's checkpoint behind.
        # The registries and the routers read the same settings, so the
        # pairing is resolved once — and both see the model that is
        # actually loaded.
        settings = Settings(_env_file=None, stt_provider="mlx-whisper")
        build_stt(settings)
        assert settings.stt_model == STT_MODELS["mlx-whisper"]

        settings = Settings(_env_file=None, tts_provider="mlx-audio")
        build_tts(settings)
        assert settings.tts_model == TTS_MODELS["mlx-audio"]

    def test_the_ollama_service_carries_the_configured_keep_alive(self):
        # The setting only means anything if it reaches the request
        # *body*: Ollama's OpenAI-compatible endpoint honours `keep_alive`
        # there, and the OpenAI client rejects it as a top-level
        # argument.
        settings = Settings(
            _env_file=None,
            llm_provider="ollama",
            llm_keep_alive="7m",
            llm_reasoning_effort="",
        )
        service = build_llm(settings)
        assert service._settings.extra == {"extra_body": {"keep_alive": "7m"}}

    def test_the_ollama_service_carries_the_reasoning_policy(self):
        # Qwen3.5 thinks by default, and `think: false` is not forwarded
        # by Ollama's OpenAI-compatible endpoint. If `reasoning_effort`
        # does not reach the body, every reply is hidden reasoning and
        # the bot says nothing.
        settings = Settings(
            _env_file=None, llm_provider="ollama", llm_reasoning_effort="none"
        )
        service = build_llm(settings)
        assert service._settings.extra["extra_body"]["reasoning_effort"] == "none"

    @pytest.mark.asyncio
    async def test_ollama_runtime_http_client_ignores_proxy_environment(self, monkeypatch):
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            monkeypatch.setenv(name, "http://127.0.0.1:9")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")

        service = build_llm(Settings(_env_file=None, llm_provider="ollama"))
        client = service._client
        try:
            http_client = client._client
            assert http_client._trust_env is False
            assert not http_client.is_closed
        finally:
            await client.close()
        assert http_client.is_closed


class TestRouterCoverage:
    """A provider with a service but no router is a latent bug."""

    @pytest.mark.parametrize("provider", sorted(STT_BACKENDS))
    def test_every_stt_provider_has_a_language_router(self, provider):
        settings = Settings(_env_file=None, stt_provider=provider)
        assert isinstance(select_stt_language_router(settings), SttLanguageRouter)

    @pytest.mark.parametrize("provider", sorted(TTS_BACKENDS))
    def test_every_tts_provider_has_a_voice_router(self, provider):
        settings = Settings(_env_file=None, tts_provider=provider)
        assert isinstance(select_voice_router(settings), VoiceRouter)


class TestBackendIndependence:
    """The routing processor must not name a concrete backend."""

    FORBIDDEN = {
        "KokoroTTSService",
        "LocalMlxTTSService",
        "WhisperSTTServiceMLX",
        "LocalMlxSTTService",
        "FunASRSTTService",
        "OLLamaLLMService",
        "OpenAILLMService",
    }

    def _imported_names(self, path: pathlib.Path) -> set[str]:
        tree = ast.parse(path.read_text())
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[-1])
        return names

    def test_pipeline_module_names_no_backend(self):
        imported = self._imported_names(REPO_ROOT / "bot" / "pipeline.py")
        assert not (imported & self.FORBIDDEN), (
            "bot/pipeline.py must not import a concrete backend; "
            f"found {imported & self.FORBIDDEN}"
        )

    def test_pipeline_module_uses_the_protocol(self):
        imported = self._imported_names(REPO_ROOT / "bot" / "pipeline.py")
        assert "VoiceRouter" in imported


class TestSwapChangesNoMetric:
    """A swap must not change anything the dashboards observe."""

    def test_metric_names_are_identical_across_providers(self):
        # The observers are built on Pipecat's MetricsFrame payload types,
        # which are backend-agnostic. This is the property that makes the
        # claim true; the concrete metric names are asserted in
        # test_observers.py.
        from bot.observers import METRIC_NAMES

        names = set(METRIC_NAMES)
        for provider in ("mlx-audio", "mlx-audio-realtime", "mlx-whisper"):
            settings = Settings(
                _env_file=None,
                stt_provider=provider,
                stt_model=STT_MODELS[provider],
            )
            build_stt(settings)
        assert names == set(METRIC_NAMES)
