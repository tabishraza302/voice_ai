"""Tests for the settings object.

The defaults are a contract: a missing ``.env`` must not crash the bot,
and every default is documented in ``bot/config.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bot.config import SYSTEM_PROMPT, Settings
from tests.test_voice_live_integration import validate_loopback_base_url


def test_defaults():
    s = Settings(_env_file=None)
    assert s.stt_provider == "mlx-audio-realtime"
    assert s.stt_model == "mlx-community/nemotron-3.5-asr-streaming-0.6b"
    assert s.stt_language == "auto"
    assert s.llm_provider == "ollama"
    assert s.llm_model == "qwen3.5:4b"
    assert s.llm_reasoning_effort == "none"
    assert s.tts_provider == "mlx-audio"
    assert s.tts_voice_en == "af_heart"
    assert s.tts_voice_hi == "hf_alpha"
    assert s.port == 7860
    assert s.enable_tracing is True
    assert s.enable_metrics is True
    assert s.vad_stop_secs == 0.8
    assert s.vad_min_volume == 0.6
    assert s.vad_confidence == 0.7
    assert s.sqlite_database_url == "sqlite+aiosqlite:///voice_ai.db"
    assert s.web_port == 3001
    assert s.host == "localhost"

@pytest.mark.parametrize(
    "field,value",
    [
        ("vad_min_volume", -0.01),
        ("vad_min_volume", 1.01),
        ("vad_confidence", -0.01),
        ("vad_confidence", 1.01),
        ("web_port", 0),
        ("web_port", 65536),
    ],
)
def test_runtime_thresholds_and_web_port_reject_out_of_range_values(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_every_field_has_a_default():
    """A field with no default would make a missing .env fatal."""
    s = Settings(_env_file=None)
    for name in type(s).model_fields:
        assert getattr(s, name) is not None or name in (
            # These are legitimately nullable: "auto" is None at the
            # STT layer, and a missing voice is the model's own default.
            "stt_language",
        ), f"{name} has no usable default"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("stt_provider", "mlx-whisper")
    monkeypatch.setenv("llm_model", "gemma3:12b")
    monkeypatch.setenv("vad_stop_secs", "0.5")
    s = Settings(_env_file=None)
    assert s.stt_provider == "mlx-whisper"
    assert s.llm_model == "gemma3:12b"
    assert s.vad_stop_secs == 0.5


def test_vad_analyzer_uses_the_configured_stop_window():
    """Reverting to ``SileroVADAnalyzer()`` would silently restore 0.2 s."""
    from bot.bot import build_vad_analyzer

    analyzer = build_vad_analyzer(Settings(_env_file=None))
    assert analyzer.params.stop_secs == 0.8
    assert analyzer.params.min_volume == 0.6
    assert analyzer.params.confidence == 0.7


def test_the_mlx_server_is_needed_by_either_layer():
    from bot.bot import needs_mlx_audio_server

    assert needs_mlx_audio_server(Settings(_env_file=None, stt_provider="mlx-audio"))
    assert needs_mlx_audio_server(
        Settings(_env_file=None, stt_provider="mlx-audio-realtime", tts_provider="kokoro")
    )
    assert needs_mlx_audio_server(
        Settings(_env_file=None, stt_provider="mlx-whisper", tts_provider="mlx-audio")
    )
    assert not needs_mlx_audio_server(
        Settings(_env_file=None, stt_provider="mlx-whisper", tts_provider="kokoro")
    )


def test_a_missing_mlx_server_names_the_layer_that_needs_it():
    """A bot that starts and speaks nothing is the failure mode to avoid."""
    import asyncio

    from bot.bot import start_mlx_audio_server

    settings = Settings(
        _env_file=None,
        stt_provider="mlx-whisper",
        tts_provider="mlx-audio",
        stt_base_url="http://127.0.0.1:9/v1",
        tts_base_url="http://127.0.0.1:9",
    )
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(start_mlx_audio_server(settings))
    message = str(excinfo.value)
    assert "tts" in message
    assert "127.0.0.1:9" in message


def test_system_prompt_is_shared_and_says_no_markdown():
    # The prompt is read aloud, so it must forbid formatting and emoji.
    assert "read aloud" in SYSTEM_PROMPT
    assert "Never use emoji" in SYSTEM_PROMPT
    assert "markdown" in SYSTEM_PROMPT.lower()


@pytest.mark.parametrize(
    "allowed_host",
    [
        "localhost",
        "LOCALHOST",
        "127.0.0.1",
        "127.0.0.2",
        "::1",
    ],
)
def test_runner_host_allows_literal_loopback(allowed_host):
    s = Settings(_env_file=None, host=allowed_host)
    assert s.host == allowed_host


@pytest.mark.parametrize(
    "rejected_host",
    [
        "0.0.0.0",
        "::",
        "[::]",
        "[::1]",
        "192.168.1.100",
        "10.0.0.5",
        "0.0.0.0.nip.io",
        "local.test",
        "example.com",
        "",
        "   ",
    ],
)
def test_runner_host_rejects_non_loopback_and_wildcard(rejected_host):
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, host=rejected_host)
    assert "unauthenticated local management routes" in str(excinfo.value)


def test_runner_cli_host_cannot_bypass_the_loopback_guard():
    """The dev runner's --host is the final bind address, not a suggestion."""
    from bot.bot import _LoopbackBindParser

    parser = _LoopbackBindParser(add_help=False)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7860)

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--host", "0.0.0.0"])
    assert excinfo.value.code == 2

    parsed = parser.parse_args(["--host", "127.0.0.1", "--port", "9001"])
    assert parsed.host == "127.0.0.1"
    assert parsed.port == 9001


@pytest.mark.parametrize(
    "valid_url,expected",
    [
        ("http://127.0.0.1:7860", "http://127.0.0.1:7860"),
        ("http://127.0.0.1:7860/", "http://127.0.0.1:7860"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.2:9000", "http://127.0.0.2:9000"),
        ("http://[::1]:7860", "http://[::1]:7860"),
        ("https://127.0.0.1:8443", "https://127.0.0.1:8443"),
    ],
)
def test_voice_live_endpoint_validation_accepts_loopback(valid_url, expected):
    assert validate_loopback_base_url(valid_url) == expected


@pytest.mark.parametrize(
    "invalid_url",
    [
        "ftp://127.0.0.1:7860",
        "http://example.com:7860",
        "http://192.168.1.50:7860",
        "http://10.0.0.1:7860",
        "http://0.0.0.0:7860",
        "http://8.8.8.8:7860",
        "not-a-url",
    ],
)
def test_voice_live_endpoint_validation_rejects_non_loopback(invalid_url):
    with pytest.raises(ValueError):
        validate_loopback_base_url(invalid_url)
