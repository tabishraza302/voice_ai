import pytest

from bot.config import Settings
from bot.session_settings import resolve_session_settings


@pytest.mark.asyncio
async def test_session_without_profile_preserves_configured_settings():
    configured = Settings(_env_file=None, llm_model="custom-local-model")

    async def forbidden_resolver(settings, agent_id):
        raise AssertionError("no profile should be loaded for a default session")

    actual = await resolve_session_settings(configured, {}, forbidden_resolver)

    assert actual.llm_model == "custom-local-model"
    assert actual.stt_provider == configured.stt_provider


@pytest.mark.asyncio
async def test_session_with_requested_profile_applies_only_that_profile():
    configured = Settings(_env_file=None, llm_model="configured-model")
    calls = []

    async def resolve_profile(settings, agent_id):
        calls.append(agent_id)
        updated = settings.model_copy(deep=True)
        updated.llm_model = "profile-model"
        return updated

    actual = await resolve_session_settings(
        configured, {"agent_id": "saved-profile-id"}, resolve_profile
    )

    assert calls == ["saved-profile-id"]
    assert actual.llm_model == "profile-model"


@pytest.mark.asyncio
async def test_session_with_unknown_profile_does_not_silently_fall_back():
    async def unknown_profile(settings, agent_id):
        raise LookupError("requested profile does not exist")

    with pytest.raises(LookupError, match="requested profile does not exist"):
        await resolve_session_settings(
            Settings(_env_file=None), {"agent_id": "missing"}, unknown_profile
        )


@pytest.mark.parametrize("agent_id", [None, 1, "", "   ", "x" * 129])
@pytest.mark.asyncio
async def test_session_with_malformed_explicit_profile_id_fails(agent_id):
    async def forbidden_resolver(settings, agent_id):
        raise AssertionError("malformed ids must be rejected before lookup")

    with pytest.raises(ValueError, match="agent_id"):
        await resolve_session_settings(
            Settings(_env_file=None), {"agent_id": agent_id}, forbidden_resolver
        )
