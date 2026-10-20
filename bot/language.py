"""Script detection, and the protocol seam that makes the layers swappable.

**Why this file exists.** Settings deltas are backend-specific types.
``KokoroTTSService.Settings(voice=..., language=...)`` is not
``WhisperSTTServiceMLX.Settings(model=..., language=...)``, and neither
accepts what the other does. A routing processor that hardcodes those
types looks perfectly modular until the day the TTS is swapped — and then
it pushes a delta the new backend silently ignores, so the bot keeps
speaking the old voice in the old language with no error anywhere.

So the delta construction lives here, behind two protocols, with one
implementation per backend. ``LanguageRoutingProcessor`` calls
``router.delta_for(...)`` and never names a backend. That is the difference
between a config constant and a genuinely swappable layer.

Three pieces:

* ``detect_script`` — Devanagari vs Latin, for picking a voice.
* ``VoiceRouter`` — script -> TTS settings delta.
* ``SttLanguageRouter`` — requested language -> STT settings delta.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from loguru import logger
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.whisper.stt import WhisperSTTServiceMLX
from pipecat.transcriptions.language import Language
from pipecat.utils.string import match_endofsentence

from bot.config import Settings
from bot.stt_service import LocalMlxSTTService
from bot.tts_service import LocalMlxTTSService

Script = Literal["devanagari", "latin"]

# The Devanagari Unicode block.
_DEVANAGARI_START = 0x0900
_DEVANAGARI_END = 0x097F

# Latin letters, basic + extended.
_LATIN_START = 0x0041
_LATIN_END = 0x024F

# A Devanagari proper noun inside an English sentence must not flip the
# voice. 20 % of letter codepoints is comfortably above "one word" and
# comfortably below "the sentence is Hindi".
_DEVANAGARI_RATIO_THRESHOLD = 0.20


def detect_script(text: str) -> Script:
    """Return "devanagari" or "latin" for the given text.

    Counts codepoints rather than words, because Devanagari text does not
    use spaces between words.
    """
    devanagari = 0
    latin = 0
    for ch in text:
        code = ord(ch)
        if _DEVANAGARI_START <= code <= _DEVANAGARI_END:
            devanagari += 1
        elif _LATIN_START <= code <= _LATIN_END:
            latin += 1

    total = devanagari + latin
    if total == 0:
        return "latin"
    if devanagari / total >= _DEVANAGARI_RATIO_THRESHOLD:
        return "devanagari"
    return "latin"


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class VoiceRouter(Protocol):
    """Maps a detected script to a backend-specific TTS settings delta."""

    def delta_for(self, script: Script) -> Any:
        """Return the settings delta that speaks `script`."""
        ...


@runtime_checkable
class SttLanguageRouter(Protocol):
    """Maps a requested language to a backend-specific STT settings delta."""

    def delta_for(self, language: Language | None) -> Any:
        """Return the settings delta that transcribes `language`.

        `None` means auto-detect.
        """
        ...


# ---------------------------------------------------------------------------
# Voice routers
# ---------------------------------------------------------------------------


class KokoroVoiceRouter:
    """Voice selection for Pipecat's Kokoro service.

    Kokoro's voices are language-specific: `af_heart` speaks US English
    whatever the `language` field says, and `hf_alpha` speaks Hindi. So
    both the voice and the language move together.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def delta_for(self, script: Script) -> KokoroTTSService.Settings:
        if script == "devanagari":
            return KokoroTTSService.Settings(
                voice=self._settings.tts_voice_hi, language=Language.HI
            )
        return KokoroTTSService.Settings(
            voice=self._settings.tts_voice_en, language=Language.EN
        )


class LocalMlxVoiceRouter:
    """Voice selection for the local mlx-audio TTS service.

    mlx-audio's Chatterbox models take a language code rather than a
    per-voice pack, so the delta carries the language and leaves the voice
    to the model's own default.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def delta_for(self, script: Script) -> LocalMlxTTSService.Settings:
        language = Language.HI if script == "devanagari" else Language.EN
        return LocalMlxTTSService.Settings(
            model=self._settings.tts_model,
            language=language,
            speed=self._settings.tts_speed,
        )


# ---------------------------------------------------------------------------
# STT language routers
# ---------------------------------------------------------------------------


class NemotronSttLanguageRouter:
    """Language selection for the local Nemotron STT service."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def delta_for(self, language: Language | None) -> LocalMlxSTTService.Settings:
        return LocalMlxSTTService.Settings(
            model=self._settings.stt_model,
            language=language,
        )


class WhisperSttLanguageRouter:
    """Language selection for Pipecat's MLX Whisper service."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def delta_for(self, language: Language | None) -> WhisperSTTServiceMLX.Settings:
        return WhisperSTTServiceMLX.Settings(
            model=self._settings.stt_model,
            language=language,
            temperature=0.0,
        )


# ---------------------------------------------------------------------------
# Sentence chunking
# ---------------------------------------------------------------------------

# Sentence terminators, including the Devanagari danda.
_TERMINATORS = "।.?!…"

# Kokoro's ONNX engine has a hard 510-phoneme limit and the upstream
# package truncates long text unless it is split. 200 characters is
# comfortably under that for any script, and keeps the gap between the
# first spoken clause and the rest short.
MAX_CHUNK_CHARS = 200


class SentenceChunker:
    """Splits a text stream into speakable chunks.

    This is what makes per-sentence TTS possible. The LLM's reply is
    streamed, so the chunker buffers characters until it sees a sentence
    end and then releases the sentence. That is also what lets the bot
    start speaking the first clause before the LLM has finished writing
    the last one.

    Sentence boundaries come from Pipecat's own ``match_endofsentence``,
    which is backed by the language-aware ``sentencex`` tokenizer. It finds
    the Devanagari danda (।) as readily as a Latin full stop — verified:
    the same call returns the same boundary for Hindi text whether the
    language is passed as "hi" or "en". Reusing it means the bot splits
    sentences the same way the rest of Pipecat does.

    It is deliberately not TTS-specific: every backend benefits from
    shorter, punctuated chunks, and a model with no phoneme limit still
    sounds better with sentence-sized segments.
    """

    def __init__(self, max_chars: int = MAX_CHUNK_CHARS) -> None:
        self._max_chars = max_chars
        self._buffer = ""

    def feed(self, text: str) -> list[str]:
        """Add text to the buffer and return any complete chunks."""
        self._buffer += text
        chunks: list[str] = []

        while True:
            boundary = match_endofsentence(self._buffer)
            if boundary:
                chunk = self._buffer[:boundary].strip()
                self._buffer = self._buffer[boundary:]
                if chunk:
                    chunks.append(chunk)
                continue

            # No sentence end yet. If the buffer has run past the limit,
            # cut it at the last space so a word is not split.
            if len(self._buffer) > self._max_chars:
                space = self._buffer.rfind(" ", 0, self._max_chars)
                cut = space + 1 if space > 0 else self._max_chars
                chunk = self._buffer[:cut].strip()
                self._buffer = self._buffer[cut:]
                if chunk:
                    chunks.append(chunk)
                continue

            break

        return chunks

    def flush(self) -> list[str]:
        """Return any remaining buffered text as a final chunk."""
        remainder = self._buffer.strip()
        self._buffer = ""
        return [remainder] if remainder else []


# ---------------------------------------------------------------------------
# Router selection
# ---------------------------------------------------------------------------


def parse_language_request(value: str | None) -> Language | None:
    """Turn a client-supplied language request into a Language.

    ``None``, ``"auto"`` and ``""`` all mean auto-detect, which is the
    mode that survives code-switching. A regional code is accepted
    ("hi-in" as well as "hi").
    """
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in ("auto", "none", ""):
        return None
    try:
        return Language(normalized)
    except ValueError:
        for language in Language:
            if language.value.lower() == normalized:
                return language
    raise ValueError(f"unknown language {value!r}")


def select_voice_router(settings: Settings) -> VoiceRouter:
    """Return the voice router matching the configured TTS provider."""
    provider = settings.tts_provider
    if provider == "kokoro":
        return KokoroVoiceRouter(settings)
    if provider == "mlx-audio":
        return LocalMlxVoiceRouter(settings)
    raise ValueError(
        f"no voice router for tts_provider={provider!r}; "
        "register one in bot/language.py before using it"
    )


def select_stt_language_router(settings: Settings) -> SttLanguageRouter:
    """Return the STT language router matching the configured provider."""
    provider = settings.stt_provider
    if provider == "mlx-audio":
        return NemotronSttLanguageRouter(settings)
    if provider == "mlx-whisper":
        return WhisperSttLanguageRouter(settings)
    raise ValueError(
        f"no STT language router for stt_provider={provider!r}; "
        "register one in bot/language.py before using it"
    )
