from __future__ import annotations

import logging
from dataclasses import dataclass

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RedisResources:
    commands: Redis
    control: Redis
    blocking: Redis
    namespace: str

    def key(self, suffix: str) -> str:
        return f"{self.namespace}:{suffix}"

    async def close(self) -> None:
        await self.blocking.aclose()
        await self.control.aclose()
        await self.commands.aclose()


async def open_redis_resources(url: str, namespace: str = "voice-ai") -> RedisResources:
    """Create bounded reusable connection pools; do not require Redis to be reachable here."""
    commands = Redis.from_url(
        url,
        decode_responses=True,
        max_connections=32,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
    )
    control = Redis.from_url(
        url,
        decode_responses=True,
        max_connections=8,
        socket_connect_timeout=1.0,
        socket_timeout=1.0,
    )
    blocking = Redis.from_url(
        url,
        decode_responses=True,
        max_connections=32,
        socket_connect_timeout=1.0,
        socket_timeout=45.0,
    )
    return RedisResources(
        commands=commands,
        control=control,
        blocking=blocking,
        namespace=namespace,
    )
