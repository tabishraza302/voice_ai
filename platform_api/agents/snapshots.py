"""Immutable prompt identity shared by draft reads and Chat Test sessions."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.flow.canonical import canonical_json_v1
from platform_api.agents.defaults import (
    DEFAULT_BASE_PROMPT,
    DEFAULT_POLICY_SLUG,
    DEFAULT_POLICY_VERSION,
    compute_policy_hash,
)
from platform_api.agents.models import PlatformPromptVersion


async def active_policy_snapshot(db: AsyncSession) -> dict[str, Any]:
    """Return the exact active policy text and its verified immutable identity."""
    result = await db.execute(
        select(PlatformPromptVersion)
        .where(
            PlatformPromptVersion.slug == DEFAULT_POLICY_SLUG,
            PlatformPromptVersion.is_active == True,  # noqa: E712
        )
        .order_by(PlatformPromptVersion.version.desc())
        .limit(1)
        .with_for_update(read=True)
    )
    version = result.scalar_one_or_none()
    if version is None:
        prompt_text = DEFAULT_BASE_PROMPT
        return {
            "version_id": None,
            "version": DEFAULT_POLICY_VERSION,
            "slug": DEFAULT_POLICY_SLUG,
            "prompt_text": prompt_text,
            "policy_hash": compute_policy_hash(prompt_text),
        }

    if compute_policy_hash(version.prompt_text) != version.policy_hash:
        raise ValueError("Active platform policy hash does not match its prompt text")
    return {
        "version_id": str(version.id),
        "version": version.version,
        "slug": version.slug,
        "prompt_text": version.prompt_text,
        "policy_hash": version.policy_hash,
    }


def compute_draft_chat_snapshot_hash(
    *,
    authoring_hash: str,
    persona_prompt: str,
    policy_hash: str,
) -> str:
    """Bind mutable draft config, persona, and active policy into one opaque ID."""
    payload = {
        "authoring_hash": authoring_hash,
        "persona_prompt": persona_prompt,
        "policy_hash": policy_hash,
    }
    return hashlib.sha256(canonical_json_v1(payload)).hexdigest()
