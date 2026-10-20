"""Tests for script detection, sentence chunking, and the routers.

These cover the behaviours that are easy to break silently: the
Devanagari threshold, the sentence splitter's handling of the danda, and
the fact that each router returns *its own* backend's settings type.
"""

from __future__ import annotations

import pytest
from pipecat.transcriptions.language import Language

from bot.config import Settings
from bot.language import (
    KokoroVoiceRouter,
    LocalMlxVoiceRouter,
    NemotronSttLanguageRouter,
    SentenceChunker,
    WhisperSttLanguageRouter,
    detect_script,
    select_stt_language_router,
    select_voice_router,
)


class TestDetectScript:
    def test_devanagari(self):
        assert detect_script("नमस्ते, आप कैसे हैं?") == "devanagari"

    def test_latin(self):
        assert detect_script("Hello, how are you?") == "latin"

    def test_single_devanagari_proper_noun_does_not_flip(self):
        # A Hindi name inside an English sentence must not switch the voice.
        text = "I met Rajesh at the office today and we talked about the project."
        assert detect_script(text) == "latin"

    def test_mixed_sentence_is_devanagari(self):
        # Mostly Devanagari: the voice should follow the bulk of the text.
        text = "यह बहुत अच्छा है, और मुझे इस बात की जानकारी है।"
        assert detect_script(text) == "devanagari"

    def test_empty_and_symbols(self):
        assert detect_script("") == "latin"
        assert detect_script("123 !? @#") == "latin"


class TestSentenceChunker:
    def test_splits_on_period(self):
        chunks = SentenceChunker().feed("Hello there. How are you?")
        assert chunks == ["Hello there.", "How are you?"]

    def test_splits_on_devanagari_danda(self):
        chunks = SentenceChunker().feed("यह पहला वाक्य है। यह दूसरा है।")
        assert chunks == ["यह पहला वाक्य है।", "यह दूसरा है।"]

    def test_buffers_until_a_boundary(self):
        chunker = SentenceChunker()
        assert chunker.feed("Hello") == []
        assert chunker.feed(" there.") == ["Hello there."]

    def test_flush_returns_the_remainder(self):
        chunker = SentenceChunker()
        assert chunker.feed("no terminator") == []
        assert chunker.flush() == ["no terminator"]

    def test_flush_is_empty_when_drained(self):
        chunker = SentenceChunker()
        chunker.feed("Done.")
        chunker.flush()
        assert chunker.flush() == []

    def test_long_text_without_terminator_is_cut(self):
        chunker = SentenceChunker(max_chars=40)
        chunks = chunker.feed("word " * 40)
        assert chunks, "a long buffer with no terminator must still be released"
        assert all(len(c) <= 41 for c in chunks)


class TestVoiceRouters:
    def test_kokoro_router_switches_voice_and_language(self):
        router = KokoroVoiceRouter(Settings())
        hindi = router.delta_for("devanagari")
        english = router.delta_for("latin")
        assert hindi.voice == "hf_alpha"
        assert hindi.language == Language.HI
        assert english.voice == "af_heart"
        assert english.language == Language.EN

    def test_kokoro_router_uses_the_configured_voices(self):
        settings = Settings(tts_voice_en="af_bella", tts_voice_hi="hf_beta")
        router = KokoroVoiceRouter(settings)
        assert router.delta_for("devanagari").voice == "hf_beta"
        assert router.delta_for("latin").voice == "af_bella"

    def test_mlx_router_sets_the_language(self):
        router = LocalMlxVoiceRouter(Settings())
        assert router.delta_for("devanagari").language == Language.HI
        assert router.delta_for("latin").language == Language.EN


class TestSttLanguageRouters:
    def test_nemotron_router_passes_none_through_for_auto(self):
        # None means auto-detect, which is the mode Nemotron is best at.
        router = NemotronSttLanguageRouter(Settings())
        assert router.delta_for(None).language is None

    def test_nemotron_router_sets_the_language(self):
        router = NemotronSttLanguageRouter(Settings())
        assert router.delta_for(Language.HI).language == Language.HI

    def test_whisper_router_sets_the_model(self):
        router = WhisperSttLanguageRouter(Settings())
        assert router.delta_for(Language.EN).model == Settings().stt_model


class TestRouterSelection:
    def test_voice_router_for_the_default_provider(self):
        assert isinstance(select_voice_router(Settings()), KokoroVoiceRouter)

    def test_voice_router_for_the_mlx_provider(self):
        settings = Settings(tts_provider="mlx-audio")
        assert isinstance(select_voice_router(settings), LocalMlxVoiceRouter)

    def test_stt_router_for_the_default_provider(self):
        assert isinstance(
            select_stt_language_router(Settings()), NemotronSttLanguageRouter
        )

    def test_stt_router_for_the_whisper_provider(self):
        settings = Settings(stt_provider="mlx-whisper")
        assert isinstance(
            select_stt_language_router(settings), WhisperSttLanguageRouter
        )

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="no voice router"):
            select_voice_router(Settings(tts_provider="nope"))
        with pytest.raises(ValueError, match="no STT language router"):
            select_stt_language_router(Settings(stt_provider="nope"))
