from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import logging
import math
import os
import struct
from collections.abc import Mapping
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# A clip is never longer than this, whatever the sample rate.
MAX_AUDIO_SECONDS = 10.0
# Absolute byte ceiling for the audio of one clip: 10s of 24kHz mono 16-bit PCM.
MAX_AUDIO_BYTES = 480_000
# Absolute ceiling for one serialized entry.
MAX_PAYLOAD_BYTES = 1_048_576  # 1 MiB
# Ceiling for the metadata that travels with the audio.
MAX_METADATA_BYTES = 65_536  # 64 KiB
# Ceiling for the word events of one clip.
MAX_WORD_EVENTS = 2_048
# Ceiling for the text of the word events of one clip.
MAX_WORD_METADATA_BYTES = 65_536  # 64 KiB
# Ceiling for the text of a single word event.
MAX_WORD_CHARS = 1_024
DEFAULT_TTL_SECONDS = 86_400  # 24 hours
CACHE_SOCKET_TIMEOUT_SECONDS = 0.05  # 50ms timeout for fail-open
# One small pool for the whole app: the cache is optional, so it never grows
# with the number of calls.
CACHE_MAX_CONNECTIONS = 8
# Bumped whenever the stored layout or the identity of a key changes, so old
# entries are simply never looked up again.
CACHE_SCHEMA_VERSION = "v3"

DEFAULT_ALLOWLIST = [
    "Hello!",
    "How can I help you?",
    "Thank you for calling.",
    # The exact text of the standard forced farewell, which is spoken as one
    # segment and would otherwise never match a per-sentence phrase.
    "Thank you for calling. Goodbye!",
    "Have a great day!",
    "Goodbye!",
    "I'm sorry, could you please repeat that?",
    "नमस्ते।",
    "धन्यवाद।",
]


@dataclass
class CachedAudioEntry:
    """One stored clip: its audio and the word events that go with it.

    ``word_times`` holds ``(word, timestamp, includes_inter_frame_spaces)``
    triples. The spacing flag is the one the provider's own timings carried,
    so a replayed clip spaces its words exactly like the live one.
    """

    audio: bytes
    sample_rate: int = 24000
    num_channels: int = 1
    encoding: str = "pcm_s16le"
    duration_seconds: float = 0.0
    word_times: list[tuple[str, float, bool]] = field(default_factory=list)


def max_audio_bytes(sample_rate: int, num_channels: int = 1) -> int:
    """Byte ceiling for a clip of at most ``MAX_AUDIO_SECONDS`` in a given format.

    The duration is the limit the product sets; the payload ceiling is the
    absolute one that keeps a serialized entry inside ``MAX_PAYLOAD_BYTES``.
    """
    channels = max(int(num_channels), 1)
    rate = max(int(sample_rate), 1)
    duration_bound = int(rate * 2 * channels * MAX_AUDIO_SECONDS)
    return min(duration_bound, MAX_PAYLOAD_BYTES - MAX_METADATA_BYTES - 8)


def normalize_settings_value(val: Any) -> Any:
    """Turn a settings value into something that can be hashed.

    Only values with a defined serialization are accepted. An object of an
    unknown type is refused rather than reduced to its attributes, because two
    different objects with the same attribute names would then look the same.
    """
    if isinstance(val, (str, int, float, bool)) or val is None:
        return val
    if isinstance(val, Enum):
        return val.value
    if isinstance(val, Mapping):
        return {str(k): normalize_settings_value(v) for k, v in sorted(val.items())}
    if isinstance(val, (set, frozenset)):
        # Sets have no order of their own, so their values are sorted to keep
        # a cache key stable across processes.
        normalized = [normalize_settings_value(v) for v in val]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(val, (list, tuple)):
        return [normalize_settings_value(v) for v in val]
    if hasattr(val, "model_dump") and callable(val.model_dump):
        return normalize_settings_value(val.model_dump())
    if hasattr(val, "as_dict") and callable(val.as_dict):
        return normalize_settings_value(val.as_dict())
    if dataclasses.is_dataclass(val) and not isinstance(val, type):
        return normalize_settings_value(dataclasses.asdict(val))
    raise ValueError(f"Unsupported settings type: {type(val)}")


class TTSAudioCache:
    """Storage for synthesized clips.

    The cache is optional: with no URL, or with a URL that cannot be used, it
    simply admits nothing and every lookup misses. It never keeps the
    application from starting.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        phrases_json: str | None = None,
    ) -> None:
        url = redis_url if redis_url is not None else os.environ.get("TTS_CACHE_REDIS_URL", "").strip()
        self._enabled = bool(url)
        self._redis_url = url
        self._client: aioredis.Redis | None = None

        if self._enabled:
            try:
                self._client = aioredis.from_url(
                    url,
                    decode_responses=False,
                    socket_timeout=CACHE_SOCKET_TIMEOUT_SECONDS,
                    socket_connect_timeout=CACHE_SOCKET_TIMEOUT_SECONDS,
                    max_connections=CACHE_MAX_CONNECTIONS,
                )
            except Exception:
                # The cache is optional: a URL that cannot be used disables it
                # instead of keeping the application from starting. The URL
                # itself is never logged.
                logger.warning("TTS audio cache is disabled: the configured Redis URL is not usable")
                self._enabled = False
                self._client = None

        # Allowlist parsing: a configured list replaces the default one. An
        # empty list disables admission, and so does a configured list that
        # cannot be read: the phrases the owner meant to replace are never
        # silently swapped for the built-in ones.
        raw_phrases = phrases_json if phrases_json is not None else os.environ.get("TTS_CACHE_PHRASES_JSON")
        if raw_phrases is None or not raw_phrases.strip():
            self._allowlist: set[str] = {p.strip() for p in DEFAULT_ALLOWLIST}
            self._admission_disabled = False
        else:
            try:
                parsed = json.loads(raw_phrases)
            except Exception:
                logger.warning("TTS_CACHE_PHRASES_JSON is not valid JSON; no phrase is admitted")
                self._allowlist = set()
                self._admission_disabled = True
            else:
                if not isinstance(parsed, list):
                    logger.warning(
                        "TTS_CACHE_PHRASES_JSON is not a list of phrases; no phrase is admitted"
                    )
                    self._allowlist = set()
                    self._admission_disabled = True
                else:
                    # Only strings are phrases: a number or an object in the
                    # list is not turned into one.
                    phrases = {
                        phrase.strip() for phrase in parsed if isinstance(phrase, str) and phrase.strip()
                    }
                    self._allowlist = phrases
                    self._admission_disabled = not phrases

    @property
    def enabled(self) -> bool:
        return self._enabled and not self._admission_disabled

    def is_eligible(self, text: str | None) -> bool:
        if not self.enabled or not text:
            return False
        # Eligibility policy compares surrounding whitespace stripped
        cleaned = text.strip()
        return cleaned in self._allowlist

    def build_cache_key(
        self,
        *,
        agent_id: str | Any,
        api_key: str,
        provider: str,
        model: str,
        voice_id: str,
        language: str | None,
        sample_rate: int,
        channels: int,
        encoding: str,
        settings: Mapping[str, Any] | None,
        text: str,
        endpoint: str = "",
        container: str = "",
        extra_identity: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Build the key of a clip, or None when the inputs cannot be identified.

        The key covers every input that changes the audio: the agent the call
        belongs to, the provider account, the provider and its endpoint, the
        voice settings, the audio format and the exact prepared text. Anything
        that cannot be serialized deterministically bypasses the cache rather
        than collapsing two different inputs onto one entry.
        """
        try:
            norm_settings = normalize_settings_value(settings or {})
            norm_extra = normalize_settings_value(extra_identity or {})

            # Provider-account fingerprint: SHA-256 of server API key, never the key itself
            api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()

            payload = {
                "version": CACHE_SCHEMA_VERSION,
                "agent_id": str(agent_id),
                "api_key_hash": api_key_hash,
                "provider": provider.lower(),
                "model": model,
                "voice_id": voice_id,
                "language": language or "",
                "sample_rate": sample_rate,
                "channels": channels,
                "encoding": encoding,
                "container": container,
                "endpoint": endpoint,
                "settings": norm_settings,
                "extra": norm_extra,
                "text": text,  # exact prepared text including whitespace
            }
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
            return f"tts:audio:{digest}"
        except Exception as exc:
            logger.debug("Bypassing cache key creation: %s", exc)
            return None

    def serialize(self, entry: CachedAudioEntry) -> bytes | None:
        if not entry.audio:
            return None
        sample_rate = max(int(entry.sample_rate), 1)
        channels = max(int(entry.num_channels), 1)
        duration = float(entry.duration_seconds or 0.0)
        if duration <= 0:
            duration = len(entry.audio) / (sample_rate * 2 * channels)
        if not math.isfinite(duration) or duration > MAX_AUDIO_SECONDS:
            return None
        if len(entry.audio) > max_audio_bytes(sample_rate, channels):
            return None
        if len(entry.audio) % (2 * channels) != 0:
            # A partial sample cannot be played back as it is.
            return None
        header = {
            "sample_rate": sample_rate,
            "channels": channels,
            "encoding": entry.encoding,
            "duration_seconds": duration,
            "words": [[word, timestamp, spacing] for word, timestamp, spacing in entry.word_times],
        }
        header_bytes = json.dumps(header).encode("utf-8")
        if len(header_bytes) > MAX_METADATA_BYTES:
            return None
        total = struct.pack(">I", len(header_bytes)) + header_bytes + entry.audio
        if len(total) > MAX_PAYLOAD_BYTES:
            return None
        return total

    def deserialize(self, raw: bytes) -> CachedAudioEntry | None:
        """Read a stored clip, or None when it cannot be trusted.

        A damaged entry is an ordinary miss: it is never replayed as if it
        were a clip of the right shape.
        """
        if not isinstance(raw, (bytes, bytearray)) or len(raw) < 4:
            return None
        if len(raw) > MAX_PAYLOAD_BYTES:
            # An entry that is larger than a payload may ever be is refused
            # before any of it is copied out of the stored value.
            return None
        try:
            header_len = struct.unpack(">I", bytes(raw[:4]))[0]
        except Exception:
            return None
        if header_len == 0 or header_len > MAX_METADATA_BYTES:
            return None
        if len(raw) < 4 + header_len:
            return None
        try:
            header = json.loads(bytes(raw[4 : 4 + header_len]).decode("utf-8"))
            if not isinstance(header, dict):
                return None
            audio = bytes(raw[4 + header_len :])
            sample_rate = int(header.get("sample_rate", 24000))
            channels = int(header.get("channels", 1))
            encoding = str(header.get("encoding", "pcm_s16le"))
            if sample_rate <= 0 or channels <= 0:
                return None
            if not audio or len(audio) > max_audio_bytes(sample_rate, channels):
                return None
            if len(audio) % (2 * channels) != 0:
                return None
            duration = float(header.get("duration_seconds", 0.0) or 0.0)
            if not math.isfinite(duration) or duration <= 0:
                return None
            # The declared duration has to describe the audio that is stored.
            actual = len(audio) / (sample_rate * 2 * channels)
            if abs(duration - actual) > max(0.05, actual * 0.05):
                return None
            if duration > MAX_AUDIO_SECONDS:
                return None
            words: list[tuple[str, float, bool]] = []
            for item in header.get("words", []) or []:
                if not isinstance(item, (list, tuple)) or len(item) not in (2, 3):
                    return None
                word = str(item[0])
                timestamp = float(item[1])
                spacing = bool(item[2]) if len(item) == 3 else False
                if not math.isfinite(timestamp) or timestamp < 0:
                    return None
                if timestamp > duration + max(0.1, duration * 0.05):
                    return None
                words.append((word, timestamp, spacing))
            if len(words) > MAX_WORD_EVENTS:
                return None
            return CachedAudioEntry(
                audio=audio,
                sample_rate=sample_rate,
                num_channels=channels,
                encoding=encoding,
                duration_seconds=duration,
                word_times=words,
            )
        except Exception:
            return None

    async def lookup(self, cache_key: str | None) -> CachedAudioEntry | None:
        if not self._enabled or not self._client or not cache_key:
            return None
        try:
            raw = await asyncio.wait_for(
                self._client.get(cache_key),
                timeout=CACHE_SOCKET_TIMEOUT_SECONDS,
            )
            if not raw or not isinstance(raw, (bytes, bytearray)):
                return None
            return self.deserialize(raw)
        except Exception:
            logger.debug("TTS audio cache lookup failed/timed out, failing open", exc_info=True)
            return None

    async def store(
        self,
        cache_key: str | None,
        entry: CachedAudioEntry | None,
        ttl: int = DEFAULT_TTL_SECONDS,
    ) -> bool:
        if not self._enabled or not self._client or not cache_key or not entry:
            return False
        try:
            payload = self.serialize(entry)
            if payload is None:
                return False
            await asyncio.wait_for(
                self._client.set(cache_key, payload, ex=ttl),
                timeout=CACHE_SOCKET_TIMEOUT_SECONDS,
            )
            return True
        except Exception:
            logger.debug("TTS audio cache store failed/timed out, failing open", exc_info=True)
            return False

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
