"""Platform API agents, drafts, templates, versions, and publishing package.

Importing this package must not import the audit/auth stack: ``platform_api.models``
imports ``platform_api.agents.models`` while the auth model package is still
initializing.  Heavier exports are resolved lazily.
"""

from __future__ import annotations

from typing import Any

from platform_api.agents.models import (
    Agent,
    AgentDraft,
    AgentTemplateVersion,
    AgentVersion,
    IdempotencyRecord,
    PlatformPromptVersion,
)

__all__ = [
    "router",
    "Agent",
    "AgentDraft",
    "AgentVersion",
    "AgentTemplateVersion",
    "PlatformPromptVersion",
    "IdempotencyRecord",
    "publish_agent_version",
]


def __getattr__(name: str) -> Any:
    if name == "router":
        from platform_api.agents.routes import router

        return router
    if name == "publish_agent_version":
        from platform_api.agents.publishing import publish_agent_version

        return publish_agent_version
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
