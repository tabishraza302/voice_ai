from __future__ import annotations

import asyncio
import json
import struct
import uuid
from dataclasses import dataclass

import pytest

from app.tts_cache import (
    DEFAULT_ALLOWLIST,
    MAX_AUDIO_BYTES,
    MAX_AUDIO_SECONDS,
    MAX_METADATA_BYTES,
    MAX_PAYLOAD_BYTES,
    CachedAudioEntry,
    TTSAudioCache,
    max_audio_bytes,
)
from tts_cache_test_support import (
    RecordingTTSAudioCache,
    tts_cache_redis_url,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def cache_url() -> str:
    """The Redis the cache tests write to.

    The audio cache has a Redis of its own: the tests never write into the
    instance the application coordinates its calls with.
    """
    return tts_cache_redis_url()


@pytest.fixture
async def cache(cache_url: str):
    instance = RecordingTTSAudioCache(redis_url=cache_url)
    yield instance
    try:
        await instance.remove_stored_keys()
    finally:
        await instance.close()


@dataclass
class Plain:
    voice: str = "alice"
    speed: float = 1.0


def build_key(cache: TTSAudioCache, **overrides):
    payload = {
        "agent_id": uuid.uuid4(),
        "api_key": "sk-secret-key-1",
        "provider": "cartesia",
        "model": "sonic-3.6",
        "voice_id": "voice-abc",
        "language": "en",
        "sample_rate": 24000,
        "channels": 1,
        "encoding": "pcm_s16le",
        "settings": {"speed": "normal"},
        "text": "Hello!",
    }
    payload.update(overrides)
    return cache.build_cache_key(**payload)


def test_default_allowlist_and_eligibility():
    cache = TTSAudioCache(redis_url="redis://localhost:6379/0")
    assert cache.is_eligible("Hello!")
    assert cache.is_eligible("  Hello!  ")  # whitespace stripped for eligibility check
    assert cache.is_eligible("How can I help you?")
    assert cache.is_eligible("Thank you for calling.")
    assert cache.is_eligible("Thank you for calling. Goodbye!")
    assert cache.is_eligible("नमस्ते।")
    assert not cache.is_eligible("My personal balance is $50")
    assert not cache.is_eligible("")
    assert not cache.is_eligible(None)


def test_configured_phrases_replace_the_defaults():
    custom = TTSAudioCache(
        redis_url="redis://localhost:6379/0",
        phrases_json='["Welcome to our service.", "Goodbye!"]',
    )
    assert custom.is_eligible("Welcome to our service.")
    assert custom.is_eligible("Goodbye!")
    assert not custom.is_eligible("Hello!")  # the defaults are replaced
    assert not custom.is_eligible("Thank you for calling. Goodbye!")

    # An empty list disables admission entirely.
    disabled = TTSAudioCache(
        redis_url="redis://localhost:6379/0",
        phrases_json="[]",
    )
    assert not disabled.enabled
    assert not disabled.is_eligible("Hello!")
    assert not disabled.is_eligible("Goodbye!")


def test_a_phrase_list_that_cannot_be_read_disables_admission():
    """A broken override never falls back to the built-in phrases."""
    broken = TTSAudioCache(redis_url="redis://localhost:6379/0", phrases_json="{not json")
    assert not broken.enabled
    assert not broken.is_eligible("Hello!")

    wrong_type = TTSAudioCache(redis_url="redis://localhost:6379/0", phrases_json='{"a": 1}')
    assert not wrong_type.enabled
    assert not wrong_type.is_eligible("Hello!")

    # Blank configuration keeps the documented defaults.
    blank = TTSAudioCache(redis_url="redis://localhost:6379/0", phrases_json="   ")
    assert blank.enabled
    assert blank.is_eligible("Hello!")


def test_only_strings_are_phrases():
    cache = TTSAudioCache(redis_url="redis://localhost:6379/0", phrases_json='[5, "Hello!"]')
    assert cache.is_eligible("Hello!")
    assert not cache.is_eligible("5")

    numbers_only = TTSAudioCache(redis_url="redis://localhost:6379/0", phrases_json="[5, 6]")
    assert not numbers_only.enabled


def test_a_url_that_cannot_be_used_disables_the_cache():
    """A malformed optional URL must not keep the application from starting."""
    cache = TTSAudioCache(redis_url="not a redis url at all")
    assert not cache.enabled


@pytest.mark.anyio
async def test_a_cache_with_a_broken_url_still_answers():
    cache = TTSAudioCache(redis_url="not a redis url at all")
    assert await cache.lookup("tts:audio:whatever") is None
    assert (
        await cache.store(
            "tts:audio:whatever",
            CachedAudioEntry(audio=b"\x00" * 100, sample_rate=24000, duration_seconds=0.01),
        )
        is False
    )
    await cache.close()


def test_cache_key_is_stable_and_order_independent(cache: TTSAudioCache):
    agent_id = uuid.uuid4()
    key1 = build_key(cache, agent_id=agent_id, settings={"emotion": ["happy"], "speed": "normal"})
    key2 = build_key(cache, agent_id=agent_id, settings={"speed": "normal", "emotion": ["happy"]})
    assert key1 is not None
    assert key1 == key2
    assert key1.startswith("tts:audio:")


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent_id": uuid.uuid4()},
        {"api_key": "sk-secret-key-2"},
        {"provider": "elevenlabs"},
        {"model": "sonic-2"},
        {"voice_id": "voice-other"},
        {"language": "hi"},
        {"sample_rate": 16000},
        {"encoding": "pcm_f32le"},
        {"container": "wav"},
        {"endpoint": "wss://other.example/tts"},
        {"settings": {"speed": "fast"}},
        {"text": " Hello! "},
        {"extra_identity": {"auto_mode": False}},
    ],
)
def test_cache_key_isolates_every_input(cache: TTSAudioCache, overrides):
    base = build_key(cache)
    other = build_key(cache, **overrides)
    assert base is not None
    assert other is not None
    assert base != other


def test_an_object_without_a_known_serialization_bypasses_the_cache(cache: TTSAudioCache):
    class Unknown:
        def __init__(self) -> None:
            self.voice = "alice"
            self.speed = 1.0

    assert build_key(cache, settings={"voice": Unknown()}) is None


def test_a_dataclass_is_a_known_serialization(cache: TTSAudioCache):
    key = build_key(cache, settings={"voice": Plain()})
    assert key is not None


def test_cache_key_is_stable_for_unordered_settings(cache: TTSAudioCache):
    agent_id = uuid.uuid4()
    key1 = build_key(cache, agent_id=agent_id, settings={"tones": {"b", "a"}})
    key2 = build_key(cache, agent_id=agent_id, settings={"tones": {"a", "b"}})
    assert key1 is not None
    assert key1 == key2


def test_serialization_roundtrip(cache: TTSAudioCache):
    audio = b"\x01\x02" * 12000  # 24,000 bytes
    words = [("Hello", 0.05, False), ("world", 0.35, True)]
    entry = CachedAudioEntry(
        audio=audio,
        sample_rate=24000,
        num_channels=1,
        encoding="pcm_s16le",
        duration_seconds=0.5,
        word_times=words,
    )

    serialized = cache.serialize(entry)
    assert serialized is not None
    assert len(serialized) > len(audio)
    assert len(serialized) <= MAX_PAYLOAD_BYTES

    deserialized = cache.deserialize(serialized)
    assert deserialized is not None
    assert deserialized.audio == audio
    assert deserialized.sample_rate == 24000
    assert deserialized.num_channels == 1
    assert deserialized.encoding == "pcm_s16le"
    assert deserialized.word_times == words


@pytest.mark.parametrize("sample_rate", [8000, 16000, 24000, 44100, 48000])
def test_duration_bound_follows_the_sample_rate(cache: TTSAudioCache, sample_rate: int):
    ceiling = max_audio_bytes(sample_rate, 1)
    duration_bound = int(sample_rate * 2 * MAX_AUDIO_SECONDS)
    payload_headroom = MAX_PAYLOAD_BYTES - MAX_METADATA_BYTES - 8
    assert ceiling == min(duration_bound, payload_headroom)

    if sample_rate == 24000:
        # The documented bound for the format the app uses.
        assert ceiling == MAX_AUDIO_BYTES == 480_000

    # A clip of exactly ten seconds fits, one second more does not.
    ten_seconds = ceiling
    assert (
        cache.serialize(
            CachedAudioEntry(
                audio=b"\x00" * ten_seconds,
                sample_rate=sample_rate,
                num_channels=1,
                encoding="pcm_s16le",
                duration_seconds=MAX_AUDIO_SECONDS,
                word_times=[],
            )
        )
        is not None
    )
    assert (
        cache.serialize(
            CachedAudioEntry(
                audio=b"\x00" * (ten_seconds + sample_rate * 2),
                sample_rate=sample_rate,
                num_channels=1,
                encoding="pcm_s16le",
                duration_seconds=MAX_AUDIO_SECONDS + 1.0,
                word_times=[],
            )
        )
        is None
    )


def test_a_clip_longer_than_ten_seconds_is_rejected(cache: TTSAudioCache):
    # The byte count is inside the ceiling, but the clip is too long.
    entry = CachedAudioEntry(
        audio=b"\x00" * (24000 * 2 * 11),
        sample_rate=24000,
        num_channels=1,
        encoding="pcm_s16le",
        duration_seconds=11.0,
        word_times=[],
    )
    assert cache.serialize(entry) is None


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x00\x00",
        struct.pack(">I", 0) + b"{}",
        struct.pack(">I", 100) + b"short",
        struct.pack(">I", 2) + b"[]",
        struct.pack(">I", 12) + b"not json at all",
    ],
)
def test_corrupt_entries_are_rejected(cache: TTSAudioCache, payload: bytes):
    assert cache.deserialize(payload) is None


def test_an_entry_larger_than_a_payload_is_rejected(cache: TTSAudioCache):
    """A stored value that is larger than a payload is an ordinary miss.

    The ceiling is checked before any of the value is copied out, so a
    damaged or hostile entry cannot make the process hold a copy of itself.
    """
    oversized = b"\x00" * (MAX_PAYLOAD_BYTES + 1)
    assert cache.deserialize(oversized) is None


@pytest.mark.anyio
async def test_an_entry_larger_than_a_payload_is_never_replayed(cache: TTSAudioCache):
    """The oversized entry is refused on the way out of the cache as well."""
    key = "tts:audio:oversized-entry"
    client = cache._client
    assert client is not None
    await client.set(key, b"\x00" * (MAX_PAYLOAD_BYTES + 1))
    try:
        assert await cache.lookup(key) is None
    finally:
        await client.delete(key)


def _entry_with_header(cache: TTSAudioCache, **header_changes) -> bytes:
    entry = CachedAudioEntry(
        audio=b"\x00" * 4800,
        sample_rate=24000,
        num_channels=1,
        encoding="pcm_s16le",
        duration_seconds=0.1,
        word_times=[("Hello", 0.05, False)],
    )
    serialized = cache.serialize(entry)
    assert serialized is not None
    header_len = struct.unpack(">I", serialized[:4])[0]
    header = json.loads(serialized[4 : 4 + header_len].decode("utf-8"))
    header.update(header_changes)
    broken_header = json.dumps(header).encode("utf-8")
    return struct.pack(">I", len(broken_header)) + broken_header + entry.audio


def test_an_entry_with_impossible_word_timings_is_rejected(cache: TTSAudioCache):
    broken = _entry_with_header(cache, words=[["Hello", 9999.0, False]])
    assert cache.deserialize(broken) is None


def test_an_entry_with_a_word_time_that_is_not_a_number_is_rejected(cache: TTSAudioCache):
    broken = _entry_with_header(cache, words=[["Hello", "soon", False]])
    assert cache.deserialize(broken) is None


def test_an_entry_with_a_duration_that_is_not_a_number_is_rejected(cache: TTSAudioCache):
    broken = _entry_with_header(cache, duration_seconds="soon")
    assert cache.deserialize(broken) is None


def test_an_entry_whose_duration_does_not_describe_its_audio_is_rejected(cache: TTSAudioCache):
    broken = _entry_with_header(cache, duration_seconds=9.0)
    assert cache.deserialize(broken) is None


def test_an_entry_with_a_partial_sample_is_rejected(cache: TTSAudioCache):
    entry = CachedAudioEntry(
        audio=b"\x00" * 4801,
        sample_rate=24000,
        num_channels=1,
        encoding="pcm_s16le",
        duration_seconds=0.1,
        word_times=[],
    )
    assert cache.serialize(entry) is None


@pytest.mark.anyio
async def test_store_and_lookup_round_trip(cache: TTSAudioCache):
    agent_id = uuid.uuid4()
    key = build_key(cache, agent_id=agent_id)
    assert key is not None

    entry = CachedAudioEntry(
        audio=b"\x07\x08" * 2400,
        sample_rate=24000,
        num_channels=1,
        encoding="pcm_s16le",
        duration_seconds=0.1,  # 4,800 bytes of 24kHz mono PCM
        word_times=[("Hello", 0.05, False)],
    )
    assert await cache.store(key, entry) is True

    stored = await cache.lookup(key)
    assert stored is not None
    assert stored.audio == entry.audio
    assert stored.word_times == entry.word_times

    # The entry expires on its own.
    ttl = await cache._client.ttl(key)
    assert 86_000 < ttl <= 86_400


@pytest.mark.anyio
async def test_lookup_misses_on_an_unknown_key(cache: TTSAudioCache):
    assert await cache.lookup(f"tts:audio:{uuid.uuid4()}") is None


@pytest.mark.anyio
async def test_a_broken_redis_fails_open(cache: TTSAudioCache):
    class BrokenClient:
        async def get(self, key):
            raise RuntimeError("redis is down")

        async def set(self, key, value, ex=None):
            raise RuntimeError("redis is down")

    cache._client = BrokenClient()
    assert await cache.lookup("tts:audio:whatever") is None
    assert (
        await cache.store(
            "tts:audio:whatever",
            CachedAudioEntry(audio=b"\x00" * 100, sample_rate=24000, duration_seconds=0.01),
        )
        is False
    )


@pytest.mark.anyio
async def test_a_cache_without_a_url_is_disabled():
    disabled = TTSAudioCache(redis_url="")
    assert not disabled.enabled
    assert await disabled.lookup("tts:audio:whatever") is None
    assert (
        await disabled.store(
            "tts:audio:whatever",
            CachedAudioEntry(audio=b"\x00" * 100, sample_rate=24000, duration_seconds=0.01),
        )
        is False
    )
