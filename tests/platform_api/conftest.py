"""Shared fixtures for platform_api test suites.

Provides configured PlatformSettings, cryptography keys, app factories,
and test client builders.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from cryptography.fernet import Fernet
import httpx
import pytest
import pytest_asyncio

from bot.flow.qualification import (
    REQUIRED_SCENARIO_CLASSES,
    CapabilityReport,
    ScenarioResult,
)
from bot.provider_registry import provider_registry
from platform_api.agents.config import DEFAULT_AGENT_SETTINGS
from platform_api.catalog import catalog_with_managed_llms, load_catalog
from platform_api.qualification import (
    has_qualified_voice_playback,
    load_deployment_capability_report,
)
from platform_api.app import create_app
from platform_api.config import PlatformSettings
from platform_api.db.session import create_engine, get_session_factory
from platform_api.models.provider_model_entry import ProviderModelEntry
from tests.support.postgres import FreshPostgres

TEST_CSRF_KEY = base64.urlsafe_b64encode(b"C" * 32).decode("ascii")
TEST_RATE_KEY = base64.urlsafe_b64encode(b"R" * 32).decode("ascii")
TEST_MFA_KEY = Fernet.generate_key().decode("ascii")
TEST_PROVIDER_KEY = Fernet.generate_key().decode("ascii")


def _qualified_test_report() -> CapabilityReport:
    report = CapabilityReport(
        protocol_version=4,
        adapter_id="test-local-adapter",
        adapter_version="test-only",
        model_revision="test-model-revision",
        metadata="separate_stream",
        agent_settings=DEFAULT_AGENT_SETTINGS,
        speech_gating=True,
        cancel_generation=True,
        playback="qualified_fragment_completion",
        locales=["en", "hi"],
        profile_hash="test-profile-hash",
        source_revision="test-source-revision",
        fixture_hashes={"test_fixture": "f" * 64},
        cases=[
            ScenarioResult(
                scenario_id=f"test_{scenario_class}",
                scenario_class=scenario_class,
                passed=True,
                measurements={"latency_ms": 1},
                raw_evidence={"test_only": True},
                trace_id=f"test_{scenario_class}",
            )
            for scenario_class in REQUIRED_SCENARIO_CLASSES
        ],
        measurements={"latency_ms": 1},
        budgets={"fragment_latency_ms": 1},
        raw_evidence_references=["sha256:" + "e" * 64],
        playback_boundary_description="Synthetic test-only qualification evidence.",
    )
    return report.model_copy(update={"report_hash": report.compute_hash()})


def _registered_catalog_data() -> dict[str, object]:
    stt_providers = provider_registry("stt")
    tts_providers = provider_registry("tts")
    voices = {
        voice.id: {
            "id": voice.id,
            "name": voice.name,
            "language": voice.language,
            "gender": voice.gender,
        }
        for provider in tts_providers
        for model in provider.models
        for voice in model.voices
    }
    return {
        "stt_providers": [
            {
                "id": provider.id,
                "name": provider.name,
                "languages": list(
                    dict.fromkeys(
                        language
                        for model in provider.models
                        for language in model.languages
                    )
                ),
                "models": [
                    {
                        "id": model.id,
                        "name": model.name,
                        "languages": list(model.languages),
                    }
                    for model in provider.models
                ],
            }
            for provider in stt_providers
        ],
        "tts_providers": [
            {
                "id": provider.id,
                "name": provider.name,
                "models": [
                    {
                        "id": model.id,
                        "name": model.name,
                        "voices": [
                            {
                                "id": voice.id,
                                "name": voice.name,
                                "language": voice.language,
                                "gender": voice.gender,
                            }
                            for voice in model.voices
                        ],
                    }
                    for model in provider.models
                ],
            }
            for provider in tts_providers
        ],
        "tts_voices": list(voices.values()),
    }


@pytest.fixture
def test_platform_settings(fresh_pg: FreshPostgres, tmp_path) -> PlatformSettings:
    """Return verified PlatformSettings connected to fresh test PostgreSQL database."""
    mail_dir = str(tmp_path / "test-mail")
    catalog_path = tmp_path / "test-qualified-catalog.json"
    catalog_path.write_text(
        json.dumps(_registered_catalog_data()),
        encoding="utf-8",
    )
    report = _qualified_test_report()
    report_path = tmp_path / "test-capability-report.json"
    report_path.write_text(
        json.dumps(report.model_dump(mode="json")),
        encoding="utf-8",
    )
    return PlatformSettings(
        database_url=fresh_pg.url,
        auth_csrf_key=TEST_CSRF_KEY,
        auth_rate_key=TEST_RATE_KEY,
        auth_mfa_key=TEST_MFA_KEY,
        provider_secret_key=TEST_PROVIDER_KEY,
        env="development",
        web_origin="http://127.0.0.1:3001",
        cors_origins=["http://127.0.0.1:3001"],
        cookie_secure=False,
        session_cookie_name="platform_dev_session",
        csrf_cookie_name="platform_csrf",
        auth_mail_mode="file",
        auth_mail_dir=mail_dir,
        catalog_path=str(catalog_path),
        adapter_capability_report_path=str(report_path),
        adapter_capability_report_hash=report.report_hash,
        deployment_adapter_id=report.adapter_id,
        deployment_adapter_version=report.adapter_version,
        deployment_model_revision=report.model_revision,
        deployment_profile_hash=report.profile_hash,
        deployment_source_revision=report.source_revision,
    )


@pytest_asyncio.fixture
async def qualified_test_llm(
    initialized_pg: FreshPostgres,
) -> ProviderModelEntry:
    """Seed one explicitly managed runtime model for tests that need qualification."""
    row = ProviderModelEntry(
        provider_id=DEFAULT_AGENT_SETTINGS["llm_provider"],
        model_id=DEFAULT_AGENT_SETTINGS["llm_model"],
        name="Test runtime model",
        enabled=True,
        qualified=True,
    )
    async with initialized_pg.sessions() as db:
        db.add(row)
        await db.commit()
    return row


@pytest_asyncio.fixture
async def qualified_test_catalog(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    qualified_test_llm: ProviderModelEntry,
):
    report = load_deployment_capability_report(
        test_platform_settings.adapter_capability_report_path,
        test_platform_settings.adapter_capability_report_hash,
    )
    assert report is not None
    catalog = load_catalog(
        test_platform_settings.catalog_path,
        qualified_settings=report.agent_settings.model_dump(
            mode="python",
            exclude_none=True,
        ),
        tts_playback_qualified=has_qualified_voice_playback(report),
    )
    async with initialized_pg.sessions() as db:
        return await catalog_with_managed_llms(catalog, db)


@pytest_asyncio.fixture
async def initialized_pg(
    fresh_pg: FreshPostgres,
    test_platform_settings: PlatformSettings,
) -> FreshPostgres:
    """Initialize the configured platform schema on fresh_pg and verify it."""
    await fresh_pg.engine.dispose()
    fresh_pg.engine = create_engine(test_platform_settings)
    fresh_pg.sessions = get_session_factory(fresh_pg.engine)
    await fresh_pg.initialize(
        schema=test_platform_settings.postgres_schema,
        expected_version=test_platform_settings.schema_version,
    )
    return fresh_pg


@pytest_asyncio.fixture
async def app_client(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> AsyncIterator[httpx.AsyncClient]:
    """Yield httpx.AsyncClient interacting with a fully lifespan-initialized platform app."""
    app = create_app(test_platform_settings)
    app.state.disable_billing_recovery_worker = True
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            yield client
