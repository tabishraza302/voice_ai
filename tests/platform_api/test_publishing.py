"""Contract tests for immutable agent publishing, warning acknowledgment, and lost-response idempotency.

Follows Task 3 requirements:
- Atomic publishing transaction compiling executable flow into immutable AgentVersion
- Rejection of invalid drafts (structural, semantic, unqualified catalog models)
- Warning acknowledgment enforcement (422 UNACKNOWLEDGED_WARNINGS)
- CAS draft revision enforcement (409 REVISION_CONFLICT)
- Stale authoring hash detection (409 AUTHORING_HASH_STALE)
- Lost-response publish replay and idempotency conflict handling
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import platform_api.app as platform_app
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from bot.flow.schemas import AdapterCapabilities
from platform_api.agents.defaults import compute_policy_hash
from platform_api.agents.publishing import publish_agent_version
from platform_api.auth.errors import ValidationError

from platform_api.agents.models import AgentDraft, PlatformPromptVersion
from platform_api.agents.schemas import PublishRequest
from platform_api.auth.passwords import get_password_hasher
from platform_api.auth.sessions import create_authenticated_session
from platform_api.config import PlatformSettings
from platform_api.db.session import create_engine as create_platform_engine
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


def test_publish_warning_acknowledgments_are_bounded() -> None:
    with pytest.raises(PydanticValidationError):
        PublishRequest(
            expected_revision=1,
            acknowledged_warning_codes=["W" * 129],
        )
    with pytest.raises(PydanticValidationError):
        PublishRequest(
            expected_revision=1,
            acknowledged_warning_codes=["WARNING"] * 65,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cannot_publish_invalid_drafts(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Publishing incomplete drafts or unqualified models is rejected with 422 VALIDATION."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "publish_invalid@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Invalid Publish Agent"},
    )
    agent_id = create_res.json()["id"]

    # 1. Attempt to publish empty draft -> 422
    pub_empty = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "publish-empty"},
        json={"expected_revision": 1},
    )
    assert pub_empty.status_code == 422
    assert pub_empty.json()["error"]["code"] == "VALIDATION"

    # 2. Patch draft with valid flow but unqualified model
    valid_flow = load_text_only_flow("valid-loan-en-hi.json")
    unqualified_flow = {**valid_flow, "llm_model": "unqualified-rogue-model-xyz"}

    patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": unqualified_flow},
    )
    assert patch_res.status_code == 200

    # 3. Attempt to publish draft with unqualified model -> 422 with MODEL_UNQUALIFIED
    pub_unqualified = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "publish-unqualified"},
        json={"expected_revision": 2},
    )
    assert pub_unqualified.status_code == 422
    fields = pub_unqualified.json()["error"]["fields"]
    assert any(f["code"] == "MODEL_UNQUALIFIED" for f in fields)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_warning_acknowledgment_enforcement(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Publishing flows with warnings requires explicit acknowledgment codes."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "publish_warnings@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Overlap Flow Agent"},
    )
    agent_id = create_res.json()["id"]

    # Load flow with OVERLAPPING_TRANSITIONS warning
    overlap_flow = load_text_only_flow("valid-first-match-overlap.json")
    patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": overlap_flow},
    )
    assert patch_res.status_code == 200

    # 1. Publish without acknowledging warning -> 422 UNACKNOWLEDGED_WARNINGS
    pub_unack = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "publish-warning-unack"},
        json={"expected_revision": 2, "acknowledged_warning_codes": []},
    )
    assert pub_unack.status_code == 422
    assert pub_unack.json()["error"]["fields"][0]["code"] == "UNACKNOWLEDGED_WARNINGS"

    # 2. Publish with acknowledged_warning_codes -> 201 CREATED
    pub_ack = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "publish-warning-ack"},
        json={
            "expected_revision": 2,
            "changelog": "Published with acknowledged overlap warning",
            "acknowledged_warning_codes": ["OVERLAPPING_TRANSITIONS"],
        },
    )
    assert pub_ack.status_code == 201
    assert pub_ack.json()["version"] == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_publish_cas_revision_conflict(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Publishing with stale revision returns 409 REVISION_CONFLICT."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "publish_cas@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "CAS Publish Agent"},
    )
    agent_id = create_res.json()["id"]

    valid_flow = load_text_only_flow("valid-loan-en-hi.json")
    await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": valid_flow},
    )

    # Current revision is 2; try to publish with expected_revision=1 -> 409
    pub_stale = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "publish-stale-revision"},
        json={"expected_revision": 1},
    )
    assert pub_stale.status_code == 409
    assert pub_stale.json()["error"]["code"] == "REVISION_CONFLICT"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_publish_detects_stale_stored_authoring_hash(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """A draft whose stored bytes no longer match its hash cannot be published."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "stale_authoring@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Stale Authoring Agent"},
    )
    agent_id = create_res.json()["id"]

    valid_flow = load_text_only_flow("valid-loan-en-hi.json")
    patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": valid_flow},
    )
    assert patch_res.status_code == 200

    # Simulate corruption/out-of-band mutation: change bytes, leave hash stale.
    async with initialized_pg.sessions() as session:
        draft_res = await session.execute(
            select(AgentDraft).where(AgentDraft.agent_id == uuid.UUID(agent_id))
        )
        draft = draft_res.scalar_one()
        draft.authoring_json = {**draft.authoring_json, "entry_step_id": "tampered"}
        await session.commit()

    pub_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "publish-stale-hash"},
        json={"expected_revision": 2},
    )
    assert pub_res.status_code == 409
    assert pub_res.json()["error"]["code"] == "AUTHORING_HASH_STALE"

    versions_res = await app_client.get(
        f"/api/v1/agents/{agent_id}/versions",
        headers={"Cookie": headers["Cookie"]},
    )
    assert versions_res.json()["items"] == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lost_response_publish_idempotency(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Idempotency-Key on publish replays exact outcome without creating new versions."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "lost_response@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Idempotent Publish Agent"},
    )
    agent_id = create_res.json()["id"]

    valid_flow = load_text_only_flow("valid-loan-en-hi.json")
    await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": valid_flow},
    )

    idem_headers = {**headers, "Idempotency-Key": "publish-key-lost-001"}
    publish_payload = {"expected_revision": 2, "changelog": "First publish attempt"}

    # 1. Initial publish call
    pub1 = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=idem_headers,
        json=publish_payload,
    )
    assert pub1.status_code == 201
    pub1_body = pub1.json()
    version_id = pub1_body["version_id"]
    assert pub1_body["version"] == 1

    # 2. Lost-response replay with identical key and payload
    pub2 = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=idem_headers,
        json=publish_payload,
    )
    assert pub2.status_code == 201
    assert pub2.json()["version_id"] == version_id
    assert pub2.json()["version"] == 1

    # Verify only version 1 exists in DB (no duplicate version 2 created)
    vers_res = await app_client.get(
        f"/api/v1/agents/{agent_id}/versions",
        headers={"Cookie": headers["Cookie"]},
    )
    assert len(vers_res.json()["items"]) == 1

    # 3. Key reuse with different payload -> 409 IDEMPOTENCY_CONFLICT
    conflicting_publish = {"expected_revision": 2, "changelog": "Different changelog text"}
    pub3 = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=idem_headers,
        json=conflicting_publish,
    )
    assert pub3.status_code == 409
    assert pub3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"



@pytest_asyncio.fixture
async def repeatable_read_app_client(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    monkeypatch: pytest.MonkeyPatch,
):
    """Run routes with a PostgreSQL connection default of REPEATABLE READ."""
    engine = create_platform_engine(
        test_platform_settings,
        isolation_level="REPEATABLE READ",
    )
    monkeypatch.setattr(platform_app, "create_engine", lambda _settings: engine)
    app = platform_app.create_app(test_platform_settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            yield client


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_same_key_publish_replays_committed_outcome(
    repeatable_read_app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Serialize identical publish retries with a REPEATABLE READ connection default."""
    _, headers = await create_test_user_session(
        initialized_pg,
        test_platform_settings,
        repeatable_read_app_client,
        "concurrent_publish@example.com",
    )

    create_res = await repeatable_read_app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Concurrent Publish Agent"},
    )
    agent_id = create_res.json()["id"]
    patch_res = await repeatable_read_app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={
            "expected_revision": 1,
            "config": load_text_only_flow("valid-loan-en-hi.json"),
        },
    )
    assert patch_res.status_code == 200

    publish_url = f"/api/v1/agents/{agent_id}/publish"
    publish_headers = {**headers, "Idempotency-Key": "concurrent-publish-key"}
    publish_payload = {"expected_revision": 2, "changelog": "Concurrent publish"}
    first, second = await asyncio.gather(
        repeatable_read_app_client.post(
            publish_url,
            headers=publish_headers,
            json=publish_payload,
        ),
        repeatable_read_app_client.post(
            publish_url,
            headers=publish_headers,
            json=publish_payload,
        ),
    )

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    versions = await repeatable_read_app_client.get(
        f"/api/v1/agents/{agent_id}/versions",
        headers={"Cookie": headers["Cookie"]},
    )
    assert [item["version"] for item in versions.json()["items"]] == [1]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_publish_requires_idempotency_key(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """A publish without its required key is rejected without creating a version."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "publish_without_key@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Publish Key Required Agent"},
    )
    agent_id = create_res.json()["id"]

    patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={
            "expected_revision": 1,
            "config": load_text_only_flow("valid-loan-en-hi.json"),
        },
    )
    assert patch_res.status_code == 200

    publish_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers=headers,
        json={"expected_revision": 2},
    )
    assert publish_res.status_code == 422

    versions_res = await app_client.get(
        f"/api/v1/agents/{agent_id}/versions",
        headers={"Cookie": headers["Cookie"]},
    )
    assert versions_res.json()["items"] == []


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_voice_publish_without_capabilities_is_rejected(
    initialized_pg: FreshPostgres,
):
    """Voice-enabled drafts cannot publish without deployment qualification."""
    user_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    flow = load_fixture("valid-credit-card-en-hi.json")

    async with initialized_pg.sessions() as session:
        from platform_api.agents.models import Agent, AgentDraft
        from bot.flow.canonical import compute_authoring_hash

        user = User(
            id=user_id,
            email="caps_test@example.com",
            email_key="caps_test@example.com",
            display_name="Caps User",
            password_hash="hash",
            role="user",
            status="active",
            email_verified_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add(user)
        await session.flush()
        agent = Agent(
            id=agent_id,
            owner_user_id=user_id,
            name="Caps Test Agent",
            created_at=now,
            updated_at=now,
        )
        session.add(agent)
        await session.flush()
        draft = AgentDraft(
            agent_id=agent_id,
            authoring_json=flow,
            authoring_hash=compute_authoring_hash(flow),
            revision=1,
            updated_at=now,
        )
        session.add(draft)
        await session.commit()

    # 1. Attempt publication with capabilities=None -> ValidationError(PLAYBACK_UNQUALIFIED)
    async with initialized_pg.sessions() as session:
        with pytest.raises(ValidationError) as exc_info:
            await publish_agent_version(
                session,
                agent_id=agent_id,
                owner_user_id=user_id,
                expected_revision=1,
                capabilities=None,
            )
        assert any(f.get("code") == "PLAYBACK_UNQUALIFIED" for f in exc_info.value.fields)

    # 2. Attempt publication with unqualified playback -> ValidationError(PLAYBACK_UNQUALIFIED)
    unqualified_caps = AdapterCapabilities(
        adapter_id="unqualified_adapter",
        adapter_version="1.0.0",
        model_revision="pinned-v1",
        metadata="none",
        speech_gating=False,
        cancel_generation=False,
        playback="unqualified",
        locales=["en"],
        report_hash="0" * 64,
    )
    async with initialized_pg.sessions() as session:
        with pytest.raises(ValidationError) as exc_info:
            await publish_agent_version(
                session,
                agent_id=agent_id,
                owner_user_id=user_id,
                expected_revision=1,
                capabilities=unqualified_caps,
            )
        assert any(f.get("code") == "PLAYBACK_UNQUALIFIED" for f in exc_info.value.fields)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_changing_persona_prompt_changes_base_prompt_hash_and_artifact_hash(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Changing only persona_prompt changes base_prompt_hash and artifact_hash; policy_hash and flow_hash are unchanged."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "persona_hash@example.com"
    )

    valid_flow = load_text_only_flow("valid-loan-en-hi.json")

    # 1. Create agent with Persona 1
    persona_1 = "Persona 1: You are an encouraging financial counselor."
    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Persona Diff Agent", "persona_prompt": persona_1},
    )
    assert create_res.status_code == 201
    agent_id = create_res.json()["id"]

    # 2. Put valid flow in draft
    put_draft = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": valid_flow},
    )
    assert put_draft.status_code == 200

    # 3. Publish version 1 as a text-only agent
    async with initialized_pg.sessions() as session:
        v1, _ = await publish_agent_version(
            session,
            agent_id=uuid.UUID(agent_id),
            owner_user_id=user.id,
            expected_revision=2,
            capabilities=None,
        )
        await session.commit()

    v1_base_hash = v1.immutable_config_json["manifest"]["base_prompt_hash"]
    v1_artifact_hash = v1.artifact_hash
    v1_flow_hash = v1.flow_hash
    v1_policy_hash = v1.immutable_config_json["policy"]["policy_hash"]
    assert v1.immutable_config_json["persona_prompt"] == persona_1

    # 4. Update ONLY agent's persona_prompt to Persona 2
    persona_2 = "Persona 2: You are a strict compliance auditor."
    patch_agent = await app_client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=headers,
        json={"persona_prompt": persona_2},
    )
    assert patch_agent.status_code == 200
    assert patch_agent.json()["persona_prompt"] == persona_2

    # 5. Publish version 2 (draft was NOT edited, so expected_revision is still 2)
    async with initialized_pg.sessions() as session:
        v2, _ = await publish_agent_version(
            session,
            agent_id=uuid.UUID(agent_id),
            owner_user_id=user.id,
            expected_revision=2,
            capabilities=None,
        )
        await session.commit()

    v2_base_hash = v2.immutable_config_json["manifest"]["base_prompt_hash"]
    v2_artifact_hash = v2.artifact_hash
    v2_flow_hash = v2.flow_hash
    v2_policy_hash = v2.immutable_config_json["policy"]["policy_hash"]
    assert v2.immutable_config_json["persona_prompt"] == persona_2

    # Changing only persona_prompt MUST change base_prompt_hash and artifact_hash
    assert v1_base_hash != v2_base_hash
    assert v1_artifact_hash != v2_artifact_hash

    # Policy revision and flow graph were unchanged
    assert v1_policy_hash == v2_policy_hash
    assert v1_flow_hash == v2_flow_hash


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_changing_policy_revision_changes_policy_hash_and_base_prompt_hash(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Changing only the platform policy revision changes policy_hash and compiled base_prompt_hash."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "policy_rev@example.com"
    )

    valid_flow = load_text_only_flow("valid-loan-en-hi.json")
    fixed_persona = "Persona: Stable assistant instructions."

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Policy Diff Agent", "persona_prompt": fixed_persona},
    )
    assert create_res.status_code == 201
    agent_id = create_res.json()["id"]

    await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": valid_flow},
    )


    # Publish version 1 under default policy (version 1)
    async with initialized_pg.sessions() as session:
        v1, _ = await publish_agent_version(
            session,
            agent_id=uuid.UUID(agent_id),
            owner_user_id=user.id,
            expected_revision=2,
            capabilities=None,
        )
        await session.commit()

    v1_policy_hash = v1.immutable_config_json["policy"]["policy_hash"]
    v1_base_hash = v1.immutable_config_json["manifest"]["base_prompt_hash"]
    v1_art_hash = v1.artifact_hash

    # Now simulate a platform policy upgrade: insert active version 2 with different policy text
    new_policy_text = (
        "Updated platform policy: Strictly enforce conversational turn limits and concise sentences."
    )
    async with initialized_pg.sessions() as session:
        new_policy = PlatformPromptVersion(
            id=uuid.uuid4(),
            slug="default-base",
            version=2,
            prompt_text=new_policy_text,
            policy_hash=compute_policy_hash(new_policy_text),
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        session.add(new_policy)
        await session.commit()

    # Publish version 2 for the same agent
    async with initialized_pg.sessions() as session:
        v2, _ = await publish_agent_version(
            session,
            agent_id=uuid.UUID(agent_id),
            owner_user_id=user.id,
            expected_revision=2,
            capabilities=None,
        )
        await session.commit()

    v2_policy_hash = v2.immutable_config_json["policy"]["policy_hash"]
    v2_base_hash = v2.immutable_config_json["manifest"]["base_prompt_hash"]
    v2_art_hash = v2.artifact_hash

    # Policy hash must change
    assert v1_policy_hash != v2_policy_hash
    # Compiled base prompt hash must change because platform policy text changed
    assert v1_base_hash != v2_base_hash
    # Artifact hash must change
    assert v1_art_hash != v2_art_hash
    # Flow hash is identical
    assert v1.flow_hash == v2.flow_hash


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_published_version_persona_and_hashes_immutable_after_draft_and_agent_edits(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """A published version's persona and hashes do not change when the draft or agent is edited afterwards."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "immutable_edits@example.com"
    )

    valid_flow = load_text_only_flow("valid-loan-en-hi.json")
    original_persona = "Original published persona text."

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Immutable Publish Agent", "persona_prompt": original_persona},
    )
    assert create_res.status_code == 201
    agent_id = create_res.json()["id"]

    await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": valid_flow},
    )


    async with initialized_pg.sessions() as session:
        v1, _ = await publish_agent_version(
            session,
            agent_id=uuid.UUID(agent_id),
            owner_user_id=user.id,
            expected_revision=2,
            capabilities=None,
        )
        await session.commit()

    assert v1.version == 1
    v1_persona = v1.immutable_config_json["persona_prompt"]
    v1_base_hash = v1.immutable_config_json["manifest"]["base_prompt_hash"]
    v1_art_hash = v1.artifact_hash
    v1_flow_hash = v1.flow_hash
    v1_auth_hash = v1.authoring_hash

    # 1. Edit the agent's draft flow
    mutated_flow = copy.deepcopy(valid_flow)
    mutated_flow["steps"][0]["title"] = "Modified Step Title"
    patch_draft = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 2, "config": mutated_flow},
    )
    assert patch_draft.status_code == 200

    # 2. Edit the agent metadata with a new persona
    patch_agent = await app_client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=headers,
        json={"persona_prompt": "Completely new mutated persona prompt."},
    )
    assert patch_agent.status_code == 200

    # 3. Read back Version 1 through the API
    detail_res = await app_client.get(
        f"/api/v1/agents/{agent_id}/versions/1",
        headers={"Cookie": headers["Cookie"]},
    )
    assert detail_res.status_code == 200
    v1_after = detail_res.json()

    # Immutable config persona and hashes must remain exactly identical
    assert v1_after["immutable_config"]["persona_prompt"] == original_persona
    assert (
        v1_after["immutable_config"]["manifest"]["base_prompt_hash"]
        == v1_base_hash
    )
    assert (
        v1_after["immutable_config"]["hashes"]["artifact_hash"]
        == v1_art_hash
    )
    assert v1_after["authoring_hash"] == v1_auth_hash
    assert v1_after["flow_hash"] == v1_flow_hash
    assert v1_after["artifact_hash"] == v1_art_hash
