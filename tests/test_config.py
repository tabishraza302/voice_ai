"""Tests for the settings object.

The defaults are a contract: a missing ``.env`` must not crash the bot,
and every default is documented in ``bot/config.py``.
"""

from __future__ import annotations

from bot.config import SYSTEM_PROMPT, Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert s.stt_provider == "mlx-audio"
    assert s.stt_model == "mlx-community/nemotron-3.5-asr-streaming-0.6b"
    assert s.stt_language == "auto"
    assert s.llm_provider == "ollama"
    assert s.llm_model == "gemma3:4b"
    assert s.tts_provider == "kokoro"
    assert s.tts_voice_en == "af_heart"
    assert s.tts_voice_hi == "hf_alpha"
    assert s.port == 7860
    assert s.enable_tracing is True
    assert s.enable_metrics is True
    assert s.vad_stop_secs == 0.8


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


def test_system_prompt_is_shared_and_says_no_markdown():
    # The prompt is read aloud, so it must forbid formatting and emoji.
    assert "read aloud" in SYSTEM_PROMPT
    assert "Never use emoji" in SYSTEM_PROMPT
    assert "markdown" in SYSTEM_PROMPT.lower()
