"""Resolve optional, explicitly requested per-session agent settings."""

from collections.abc import Awaitable, Callable

from bot.config import Settings


async def resolve_session_settings(
    settings: Settings,
    body: object,
    profile_resolver: Callable[..., Awaitable[Settings]],
) -> Settings:
    """Apply a saved profile only when its id was sent in this session's body."""
    if not isinstance(body, dict):
        return settings
    if "agent_id" not in body:
        return settings
    agent_id = body.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip() or len(agent_id.strip()) > 128:
        raise ValueError("request body agent_id must be a nonblank string of at most 128 characters")
    return await profile_resolver(settings, agent_id=agent_id.strip())
