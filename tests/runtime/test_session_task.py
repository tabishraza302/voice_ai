from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from bot.config import Settings
from bot.runtime.session_task import SessionAdmissionError, SessionTaskManager


class NoPlatformCalls:
    credentials = SimpleNamespace(
        runner_id="runner-local",
        web_origin="https://voice.example.test",
        service_token="service-token-value-that-is-long-enough-for-tests",
    )
    timeout_seconds = 0.1

    def __init__(self) -> None:
        self.runtime_config_calls = 0
        self.consume_join_calls = 0

    async def runtime_config(self, session_id: str):
        self.runtime_config_calls += 1
        raise AssertionError("runtime configuration must not be requested without PostgreSQL storage")

    async def consume_join(self, session_id: str, request):
        self.consume_join_calls += 1
        raise AssertionError("a browser join must not be consumed without PostgreSQL storage")


@pytest.mark.asyncio
async def test_session_offer_fails_closed_before_platform_join_without_postgres_store():
    platform = NoPlatformCalls()
    settings = Settings(
        _env_file=None,
        platform_billing_slice_ms="1000",
        platform_billing_permit_ttl_ms="5000",
    )
    manager = SessionTaskManager(platform=platform, base_settings=settings)

    with pytest.raises(SessionAdmissionError, match="durable PostgreSQL") as error:
        await manager.prepare_offer(
            {"session_id": str(uuid4()), "join_token": "opaque-one-use-join-token"},
            origin=platform.credentials.web_origin,
        )

    assert error.value.status_code == 503
    assert platform.runtime_config_calls == 0
    assert platform.consume_join_calls == 0
