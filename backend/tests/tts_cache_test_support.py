"""Shared support for the tests of the TTS audio cache.

The cache tests write to a Redis of their own. The audio cache is not allowed
to share the instance the application coordinates its calls with, so a test
never touches the state of a running application, and the keys it writes are
removed again when it is done.
"""

from __future__ import annotations

import os
from typing import Any

from app.tts_cache import DEFAULT_TTL_SECONDS, CachedAudioEntry, TTSAudioCache

#: The Redis the cache tests write to. It is the audio cache's own instance,
#: not the one the application coordinates its calls with.
DEFAULT_TEST_TTS_CACHE_REDIS_URL = "redis://127.0.0.1:6381/0"


def tts_cache_redis_url() -> str:
    """The URL of the Redis the cache tests write to.

    It is not called ``test_...``: pytest would collect it as a test.
    """
    return (
        os.environ.get("TEST_TTS_CACHE_REDIS_URL")
        or os.environ.get("TTS_CACHE_REDIS_URL")
        or DEFAULT_TEST_TTS_CACHE_REDIS_URL
    )


class RecordingTTSAudioCache(TTSAudioCache):
    """A cache that remembers the keys it was asked to store.

    Whether a clip reached the cache is what the scenarios assert, and the
    cache is the only place that knows. It records the keys it was given, so
    a test never has to look inside the client, and it can remove exactly
    those keys again when the test is over.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stored_keys: set[str] = set()

    async def store(
        self,
        cache_key: str | None,
        entry: CachedAudioEntry | None,
        ttl: int = DEFAULT_TTL_SECONDS,
    ) -> bool:
        stored = await super().store(cache_key, entry, ttl)
        if stored and cache_key:
            # Only a clip that really reached the cache is a key to clean up.
            self.stored_keys.add(cache_key)
        return stored

    async def remove_stored_keys(self) -> None:
        """Delete the keys this cache wrote, and nothing else.

        A cleanup that fails fails the test: a clip that is left behind
        would live on for the whole TTL of an entry.
        """
        client = self._client
        keys = sorted(self.stored_keys)
        self.stored_keys.clear()
        if client is None or not keys:
            return
        await client.delete(*keys)
