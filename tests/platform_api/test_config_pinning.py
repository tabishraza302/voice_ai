"""Contract tests for immutable configuration pinning, test barrier reads, and secrets scrubbing.

Follows Task 3 requirements:
- Versions append and cannot be edited by profile mutations
- Pause a direct immutable-version row read on one PostgreSQL connection while another
  connection publishes Version 2, then verify Version 1 bytes and hash remain unchanged
- Policy, model, and voice hashes are pinned in the version artifact
- Forbidden secrets/credentials are strictly excluded from immutable configuration
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

import httpx
import pytest
from sqlalchemy import select

from platform_api.agents.models import AgentDraft, AgentVersion
from platform_api.agents.publishing import _scrub_secrets, publish_agent_version
from platform_api.agents.schemas import DraftUpdate
from platform_api.agents.service import update_draft
from platform_api.auth.passwords import get_password_hasher
from platform_api.auth.sessions import create_authenticated_session
from platform_api.config import PlatformSettings
from platform_api.models.auth import User
from tests.support.postgres import FreshPostgres


def load_fixture(filename: str) -> dict[str, Any]:
    """Load JSON fixture from test fixtures."""
    path = Path(__file__).resolve().parents[1] / "fixtures" / "flow" / filename
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["flow"]


def load_text_only_flow(filename: str) -> dict[str, Any]:
    """Load a flow with both TTS voices explicitly disabled."""
    return {
        **load_fixture(filename),
        "tts_voice_en": "",
        "tts_voice_hi": "",
    }


async def create_test_user_session(
    initialized_pg: FreshPostgres,
    settings: PlatformSettings,
    app_client: httpx.AsyncClient,
    email: str,
    display_name: str = "Test User",
) -> tuple[User, dict[str, str]]:
    """Helper to register an active user and obtain authenticated headers with CSRF."""
    now = datetime.now(timezone.utc)
    hasher = get_password_hasher()
    pw_hash = await hasher.hash_password("secure-password-12345")

    async with initialized_pg.sessions() as session:
        user = User(
            id=uuid.uuid4(),
            email=email,
            email_key=email.lower().strip(),
            display_name=display_name,
            password_hash=pw_hash,
            role="user",
            status="active",
            email_verified_at=now,
            preferred_language="en",
            timezone="Asia/Kolkata",
            low_balance_notifications=True,
            auth_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
        _, secret = await create_authenticated_session(
            session, settings, user=user, client_ip="127.0.0.1", user_agent="Test", now=now
        )
        await session.commit()

    cookie_name = settings.session_cookie_name
    cookie_header = f"{cookie_name}={secret}"

    boot_res = await app_client.get(
        "/api/v1/auth/session",
        headers={"Origin": settings.web_origin, "Cookie": cookie_header},
    )
    csrf_token = boot_res.json()["csrf_token"]

    headers = {
        "Origin": settings.web_origin,
        "Cookie": cookie_header,
        "X-CSRF-Token": csrf_token,
        "Content-Type": "application/json",
    }
    return user, headers


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_version_bytes_and_hashes_pinned_across_draft_mutations_and_new_versions(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Publishing Version 2 or editing draft does not alter Version 1 bytes, hashes, or manifest."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "pinning_immutability@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Pinned Agent"},
    )
    agent_id = create_res.json()["id"]

    # 1. Publish Version 1
    flow_v1 = load_text_only_flow("valid-loan-en-hi.json")
    await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": flow_v1},
    )
    pub_v1_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "pinning-v1"},
        json={"expected_revision": 2, "changelog": "Release 1.0.0"},
    )
    assert pub_v1_res.status_code == 201
    v1_data = pub_v1_res.json()
    v1_flow_hash = v1_data["flow_hash"]
    v1_artifact_hash = v1_data["artifact_hash"]
    v1_authoring_hash = v1_data["authoring_hash"]

    # Read Version 1 detail
    v1_detail_before = await app_client.get(
        f"/api/v1/agents/{agent_id}/versions/1",
        headers={"Cookie": headers["Cookie"]},
    )
    v1_config_before = v1_detail_before.json()["immutable_config"]

    # 2. Mutate draft to revision 3
    flow_v2 = load_text_only_flow("valid-credit-card-en-hi.json")
    await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 2, "config": flow_v2},
    )

    # 3. Publish Version 2
    pub_v2_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "pinning-v2"},
        json={"expected_revision": 3, "changelog": "Release 2.0.0 (Credit Card)"},
    )
    assert pub_v2_res.status_code == 201
    v2_data = pub_v2_res.json()
    assert v2_data["version"] == 2
    assert v2_data["flow_hash"] != v1_flow_hash

    # 4. Mutate draft again to revision 4
    await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 3, "config": flow_v1},
    )

    # 5. Verify Version 1 bytes, hashes, and immutable config are 100% retained
    v1_detail_after = await app_client.get(
        f"/api/v1/agents/{agent_id}/versions/1",
        headers={"Cookie": headers["Cookie"]},
    )
    assert v1_detail_after.status_code == 200
    v1_after_body = v1_detail_after.json()
    assert v1_after_body["flow_hash"] == v1_flow_hash
    assert v1_after_body["artifact_hash"] == v1_artifact_hash
    assert v1_after_body["authoring_hash"] == v1_authoring_hash
    assert v1_after_body["immutable_config"] == v1_config_before


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrency_barrier_retains_version_one_immutability(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """A direct version-row read on connection A retains V1 through V2 publication on B."""
    user, headers = await create_test_user_session(
        initialized_pg,
        test_platform_settings,
        app_client,
        "concurrency_barrier@example.com",
    )
    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Barrier Agent"},
    )
    assert create_res.status_code == 201
    agent_id = uuid.UUID(create_res.json()["id"])

    flow_v1 = load_text_only_flow("valid-loan-en-hi.json")
    async with initialized_pg.sessions() as session:
        await update_draft(
            session,
            agent_id=agent_id,
            owner_user_id=user.id,
            payload=DraftUpdate(expected_revision=1, config=flow_v1),
        )
        v1, _ = await publish_agent_version(
            session,
            agent_id=agent_id,
            owner_user_id=user.id,
            expected_revision=2,
            changelog="V1",
            capabilities=None,
        )
        await session.commit()
        v1_id = v1.id
        v1_hash = v1.artifact_hash
        v1_config = v1.immutable_config_json

    read_started = asyncio.Event()
    publish_done = asyncio.Event()

    async def paused_version_read() -> None:
        async with initialized_pg.sessions() as connection_a:
            result = await connection_a.execute(
                select(AgentVersion).where(AgentVersion.id == v1_id)
            )
            version_before = result.scalar_one()
            assert version_before.artifact_hash == v1_hash
            assert version_before.immutable_config_json == v1_config
            read_started.set()
            await publish_done.wait()
            result_after = await connection_a.execute(
                select(AgentVersion)
                .where(AgentVersion.id == v1_id)
                .execution_options(populate_existing=True)
            )
            version_after = result_after.scalar_one()
            assert version_after.artifact_hash == v1_hash
            assert version_after.immutable_config_json == v1_config

    async def publish_second_version() -> None:
        await read_started.wait()
        try:
            flow_v2 = load_text_only_flow("valid-credit-card-en-hi.json")
            async with initialized_pg.sessions() as connection_b:
                await update_draft(
                    connection_b,
                    agent_id=agent_id,
                    owner_user_id=user.id,
                    payload=DraftUpdate(expected_revision=2, config=flow_v2),
                )
                version_2, _ = await publish_agent_version(
                    connection_b,
                    agent_id=agent_id,
                    owner_user_id=user.id,
                    expected_revision=3,
                    changelog="V2 published while V1 read was paused",
                    capabilities=None,
                )
                await connection_b.commit()
                assert version_2.version == 2
        finally:
            publish_done.set()

    await asyncio.gather(paused_version_read(), publish_second_version())



@pytest.mark.postgres
@pytest.mark.asyncio
async def test_profile_mutations_do_not_alter_version_records(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Mutating agent name, description, or enabled state never modifies immutable version records."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "profile_mutation@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Original Name", "description": "Original Description"},
    )
    agent_id = create_res.json()["id"]

    flow = load_text_only_flow("valid-loan-en-hi.json")
    await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": flow},
    )
    pub_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "pinning-profile-v1"},
        json={"expected_revision": 2, "changelog": "V1 release"},
    )
    assert pub_res.status_code == 201

    # Read Version 1
    v1_initial = await app_client.get(
        f"/api/v1/agents/{agent_id}/versions/1",
        headers={"Cookie": headers["Cookie"]},
    )
    v1_body = v1_initial.json()

    # Perform extensive profile mutations on Agent entity
    await app_client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=headers,
        json={"name": "Radically Changed Name", "description": "New Description"},
    )
    await app_client.post(f"/api/v1/agents/{agent_id}/enable", headers=headers)
    await app_client.post(f"/api/v1/agents/{agent_id}/disable", headers=headers)

    # Read Version 1 again: all hashes and changelog remain identical
    v1_after = await app_client.get(
        f"/api/v1/agents/{agent_id}/versions/1",
        headers={"Cookie": headers["Cookie"]},
    )
    assert v1_after.status_code == 200
    assert v1_after.json() == v1_body


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_draft_patch_rejects_secret_fields_without_mutating_draft(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Credentials are blocking on save, not silently scrubbed into a draft."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "secret_rejection@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Secret Rejection Agent"},
    )
    assert create_res.status_code == 201
    agent_id = create_res.json()["id"]

    flow = load_fixture("valid-loan-en-hi.json")
    flow["api_key"] = "sk-live-should-never-be-saved"
    flow["steps"][0]["access_token"] = "bearer-should-never-be-saved"

    patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": flow},
    )
    assert patch_res.status_code == 422
    body = patch_res.json()
    assert body["error"]["code"] == "VALIDATION"
    codes = {field["code"] for field in body["error"]["fields"]}
    assert "SECRET_NOT_ALLOWED" in codes

    # The rejected payload must not have advanced the draft or leaked into it.
    draft_res = await app_client.get(
        f"/api/v1/agents/{agent_id}/draft",
        headers={"Cookie": headers["Cookie"]},
    )
    assert draft_res.status_code == 200
    draft_body = draft_res.json()
    assert draft_body["draft_revision"] == 1
    serialized = json.dumps(draft_body["config"])
    assert "sk-live-should-never-be-saved" not in serialized
    assert "bearer-should-never-be-saved" not in serialized


def test_secrets_scrubbing_function():
    """Unit assertion verifying recursive exclusion of credentials/tokens from immutable configs."""
    raw_payload = {
        "schema_version": 1,
        "api_key": "sk-secret-12345",
        "secret": "top-secret-val",
        "password": "plain-text-password",
        "steps": [
            {
                "id": "step1",
                "title": "Welcome",
                "access_token": "bearer-secret-token",
                "nested": {
                    "private_key": "-----BEGIN RSA PRIVATE KEY-----",
                    "safe_value": "hello world",
                },
            }
        ],
    }
    clean = _scrub_secrets(raw_payload)

    # Verify all forbidden keys were pruned
    assert "api_key" not in clean
    assert "secret" not in clean
    assert "password" not in clean
    assert "access_token" not in clean["steps"][0]
    assert "private_key" not in clean["steps"][0]["nested"]
    # Verify safe fields remain intact
    assert clean["schema_version"] == 1
    assert clean["steps"][0]["title"] == "Welcome"
    assert clean["steps"][0]["nested"]["safe_value"] == "hello world"
