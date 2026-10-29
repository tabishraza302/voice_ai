"""Contract tests for platform agent lifecycle, two-owner isolation, templates, and idempotency.

Follows Task 3 requirements:
- Two-owner CRUD/duplicate/restore isolation: other-owner routes return 404 NOT_FOUND
- Agent creation copies default or template flow into initial draft (revision=1, enabled=False)
- Profile updates, enable/disable toggle, and deletion
- Duplicating an agent creates a new isolated agent and draft
- Idempotency-Key deduplication and payload conflict handling
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

import httpx
import pytest
from sqlalchemy import select

from platform_api.auth.passwords import get_password_hasher
from platform_api.auth.sessions import create_authenticated_session
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuditLog, User
from tests.support.postgres import FreshPostgres


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
async def test_two_owner_agent_crud_isolation(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """User B cannot view, update, delete, duplicate, or mutate User A's agent (404 NOT_FOUND)."""
    user_a, headers_a = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "owner_a@example.com", "Owner A"
    )
    user_b, headers_b = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "owner_b@example.com", "Owner B"
    )

    # 1. User A creates an agent
    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers_a,
        json={"name": "Agent Alpha", "description": "Owner A's private agent"},
    )
    assert create_res.status_code == 201
    agent_a_id = create_res.json()["id"]

    # 2. User B lists agents -> must NOT see User A's agent
    list_b_res = await app_client.get("/api/v1/agents", headers={"Cookie": headers_b["Cookie"]})
    assert list_b_res.status_code == 200
    b_agent_ids = [item["id"] for item in list_b_res.json()["items"]]
    assert agent_a_id not in b_agent_ids

    # 3. User B attempts GET /agents/{agent_a_id} -> 404 NOT_FOUND
    get_res = await app_client.get(
        f"/api/v1/agents/{agent_a_id}",
        headers={"Cookie": headers_b["Cookie"]},
    )
    assert get_res.status_code == 404
    assert get_res.json()["error"]["code"] == "NOT_FOUND"

    # 4. User B attempts PATCH /agents/{agent_a_id} -> 404 NOT_FOUND
    patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_a_id}",
        headers=headers_b,
        json={"name": "Hacked Name"},
    )
    assert patch_res.status_code == 404
    assert patch_res.json()["error"]["code"] == "NOT_FOUND"

    # 5. User B attempts DELETE /agents/{agent_a_id} -> 404 NOT_FOUND
    del_res = await app_client.delete(
        f"/api/v1/agents/{agent_a_id}",
        headers=headers_b,
    )
    assert del_res.status_code == 404
    assert del_res.json()["error"]["code"] == "NOT_FOUND"

    # 6. User B attempts enable/disable -> 404 NOT_FOUND
    en_res = await app_client.post(
        f"/api/v1/agents/{agent_a_id}/enable",
        headers=headers_b,
    )
    assert en_res.status_code == 404
    dis_res = await app_client.post(
        f"/api/v1/agents/{agent_a_id}/disable",
        headers=headers_b,
    )
    assert dis_res.status_code == 404

    # 7. User B attempts draft access -> 404 NOT_FOUND
    draft_get_res = await app_client.get(
        f"/api/v1/agents/{agent_a_id}/draft",
        headers={"Cookie": headers_b["Cookie"]},
    )
    assert draft_get_res.status_code == 404

    draft_patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_a_id}/draft",
        headers=headers_b,
        json={"expected_revision": 1, "config": {"schema_version": 1, "steps": []}},
    )
    assert draft_patch_res.status_code == 404

    # 8. User B attempts duplicate -> 404 NOT_FOUND
    dup_res = await app_client.post(
        f"/api/v1/agents/{agent_a_id}/duplicate",
        headers=headers_b,
        json={"name": "Stolen Duplicate"},
    )
    assert dup_res.status_code == 404

    # 9. User B attempts restore-draft -> 404 NOT_FOUND
    restore_res = await app_client.post(
        f"/api/v1/agents/{agent_a_id}/restore-draft",
        headers=headers_b,
        json={"version": 1, "expected_revision": 1},
    )
    assert restore_res.status_code == 404

    # 10. User B attempts publish -> 404 NOT_FOUND
    pub_res = await app_client.post(
        f"/api/v1/agents/{agent_a_id}/publish",
        headers={**headers_b, "Idempotency-Key": "foreign-publish"},
        json={"expected_revision": 1},
    )
    assert pub_res.status_code == 404


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_owner_agent_crud_lifecycle(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Complete agent lifecycle: create, get, patch, enable/disable, delete."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "owner_lifecycle@example.com"
    )

    # 1. Create agent
    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Support Bot", "description": "Customer support assistant"},
    )
    assert create_res.status_code == 201
    body = create_res.json()
    agent_id = body["id"]
    assert body["name"] == "Support Bot"
    assert body["description"] == "Customer support assistant"
    assert body["enabled"] is False
    assert body["draft_revision"] == 1
    assert body["published_version_id"] is None

    # 2. Get agent
    get_res = await app_client.get(
        f"/api/v1/agents/{agent_id}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "Support Bot"

    # 3. Patch agent profile
    patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=headers,
        json={"name": "Support Bot v2", "description": "Updated description"},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["name"] == "Support Bot v2"
    assert patch_res.json()["description"] == "Updated description"

    # 4. Enable agent
    en_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/enable",
        headers=headers,
    )
    assert en_res.status_code == 200
    assert en_res.json()["enabled"] is True

    # 5. Disable agent
    dis_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/disable",
        headers=headers,
    )
    assert dis_res.status_code == 200
    assert dis_res.json()["enabled"] is False

    # 6. Delete agent
    del_res = await app_client.delete(
        f"/api/v1/agents/{agent_id}",
        headers=headers,
    )
    assert del_res.status_code == 204

    # 7. Confirm deletion
    get_after = await app_client.get(
        f"/api/v1/agents/{agent_id}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert get_after.status_code == 404


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_update_audit_records_changed_field_names_only(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "audit_summary@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Audit Summary Agent"},
    )
    agent_id = uuid.UUID(create_res.json()["id"])
    private_description = "private-description-sentinel"
    private_prompt = "private-persona-prompt-sentinel"

    update_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=headers,
        json={
            "name": "Updated Audit Summary Agent",
            "description": private_description,
            "persona_prompt": private_prompt,
        },
    )
    assert update_res.status_code == 200

    async with initialized_pg.sessions() as session:
        audit_res = await session.execute(
            select(AuditLog)
            .where(
                AuditLog.action == "agent.update",
                AuditLog.target_id == agent_id,
            )
            .order_by(AuditLog.created_at.desc())
        )
        audit_entry = audit_res.scalar_one()
        create_audit_res = await session.execute(
            select(AuditLog).where(
                AuditLog.action == "agent.create",
                AuditLog.target_id == agent_id,
            )
        )
        create_audit_entry = create_audit_res.scalar_one()

    assert audit_entry.change_summary == {
        "changed_fields": ["description", "name", "persona_prompt"]
    }
    assert create_audit_entry.change_summary == {"agent_id": str(agent_id)}
    assert "Audit Summary Agent" not in json.dumps(create_audit_entry.change_summary)
    serialized_summary = json.dumps(audit_entry.change_summary)
    assert private_description not in serialized_summary
    assert private_prompt not in serialized_summary


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_duplicate(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Duplicating an agent creates a new agent entity and deep-copies the draft flow."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "owner_duplicate@example.com"
    )

    # 1. Create source agent
    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Original Agent", "description": "Original description"},
    )
    assert create_res.status_code == 201
    source_id = create_res.json()["id"]

    # 2. Patch source draft with custom steps
    custom_flow = {
        "schema_version": 1,
        "steps": [
            {
                "id": "intro",
                "title": "Introduction",
                "type": "inform",
                "instruction": "Say hello",
                "required": True,
                "messages": [{"id": "m1", "en": "Hello there.", "hi": "नमस्ते।"}],
            }
        ],
    }
    patch_draft_res = await app_client.patch(
        f"/api/v1/agents/{source_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": custom_flow},
    )
    assert patch_draft_res.status_code == 200
    assert patch_draft_res.json()["draft_revision"] == 2

    # 3. Duplicate agent
    dup_res = await app_client.post(
        f"/api/v1/agents/{source_id}/duplicate",
        headers=headers,
        json={"name": "Cloned Agent", "description": "Cloned description"},
    )
    assert dup_res.status_code == 201
    dup_body = dup_res.json()
    new_agent_id = dup_body["id"]
    assert new_agent_id != source_id
    assert dup_body["name"] == "Cloned Agent"
    assert dup_body["draft_revision"] == 1
    assert dup_body["enabled"] is False

    async with initialized_pg.sessions() as session:
        duplicate_audit_res = await session.execute(
            select(AuditLog).where(
                AuditLog.action == "agent.duplicate",
                AuditLog.target_id == uuid.UUID(new_agent_id),
            )
        )
        duplicate_audit_entry = duplicate_audit_res.scalar_one()
    assert duplicate_audit_entry.change_summary == {
        "source_agent_id": source_id,
        "new_agent_id": new_agent_id,
    }
    assert "Cloned Agent" not in json.dumps(duplicate_audit_entry.change_summary)

    # 4. Verify duplicated draft content
    dup_draft_res = await app_client.get(
        f"/api/v1/agents/{new_agent_id}/draft",
        headers={"Cookie": headers["Cookie"]},
    )
    assert dup_draft_res.status_code == 200
    dup_draft = dup_draft_res.json()
    assert dup_draft["draft_revision"] == 1
    assert dup_draft["config"]["steps"][0]["id"] == "intro"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_creation_from_template(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Creating an agent with template_version_id copies template flow into initial draft."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "owner_template@example.com"
    )

    # 1. Fetch template list
    tpl_res = await app_client.get(
        "/api/v1/templates",
        headers={"Cookie": headers["Cookie"]},
    )
    assert tpl_res.status_code == 200
    templates = tpl_res.json()["items"]
    assert len(templates) >= 1
    selected_template = templates[0]

    # 2. Create agent from template
    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={
            "name": "Template Agent",
            "description": "Agent created from template",
            "source": "template",
            "template_version_id": selected_template["id"],
        },
    )
    assert create_res.status_code == 201
    agent_id = create_res.json()["id"]

    # 3. Verify draft has template flow
    draft_res = await app_client.get(
        f"/api/v1/agents/{agent_id}/draft",
        headers={"Cookie": headers["Cookie"]},
    )
    assert draft_res.status_code == 200
    draft_body = draft_res.json()
    assert len(draft_body["config"].get("steps", [])) > 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_list_cursor_pagination_returns_every_agent_once(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Cursor pagination must not drop the boundary item."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "owner_pagination@example.com"
    )

    created_ids = []
    for index in range(3):
        create_res = await app_client.post(
            "/api/v1/agents",
            headers=headers,
            json={"name": f"Pagination Agent {index}"},
        )
        assert create_res.status_code == 201
        created_ids.append(create_res.json()["id"])

    first_page = await app_client.get(
        "/api/v1/agents?limit=2",
        headers={"Cookie": headers["Cookie"]},
    )
    assert first_page.status_code == 200
    first_body = first_page.json()
    assert len(first_body["items"]) == 2
    assert first_body["next_cursor"] is not None

    second_page = await app_client.get(
        f"/api/v1/agents?limit=2&cursor={first_body['next_cursor']}",
        headers={"Cookie": headers["Cookie"]},
    )
    assert second_page.status_code == 200
    second_body = second_page.json()
    assert len(second_body["items"]) == 1

    seen = [item["id"] for item in first_body["items"] + second_body["items"]]
    assert len(seen) == len(set(seen)) == 3
    assert set(seen) == set(created_ids)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_create_idempotency(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Idempotency-Key replays identical creation and rejects mismatched payload with 409."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "owner_idem@example.com"
    )
    idem_headers = {**headers, "Idempotency-Key": "agent-create-key-001"}

    # 1. First creation
    payload = {"name": "Idempotent Bot", "description": "First attempt"}
    res1 = await app_client.post(
        "/api/v1/agents",
        headers=idem_headers,
        json=payload,
    )
    assert res1.status_code == 201
    agent_id = res1.json()["id"]

    # 2. Replay with identical key and payload -> returns exact same outcome
    res2 = await app_client.post(
        "/api/v1/agents",
        headers=idem_headers,
        json=payload,
    )
    assert res2.status_code == 201
    assert res2.json()["id"] == agent_id

    # 3. Reuse key with different payload -> 409 IDEMPOTENCY_CONFLICT
    conflicting_payload = {"name": "Conflicting Bot", "description": "Second attempt"}
    res3 = await app_client.post(
        "/api/v1/agents",
        headers=idem_headers,
        json=conflicting_payload,
    )
    assert res3.status_code == 409
    assert res3.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
