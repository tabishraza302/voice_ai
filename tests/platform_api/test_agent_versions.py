"""Contract tests for draft CAS revisions, incomplete autosave, validation, and restoration.

Follows Task 3 requirements:
- Mutable draft CAS with strict 409 REVISION_CONFLICT on collision
- Structurally valid incomplete autosave succeeds with executable diagnostics
- Structurally invalid drafts fail save with 422 VALIDATION
- Draft validation endpoint enforces expected_authoring_hash (409 AUTHORING_HASH_STALE)
- Restore draft from published version with CAS tracking
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
import uuid

import httpx
import pytest

from bot.flow.schemas import FlowLimits
from platform_api.app import create_app
from platform_api.catalog import load_catalog

from platform_api.auth.passwords import get_password_hasher
from platform_api.auth.sessions import create_authenticated_session
from platform_api.config import PlatformSettings
from platform_api.models.auth import User
from tests.support.postgres import FreshPostgres


def load_valid_flow() -> dict[str, Any]:
    """Load valid flow fixture from test fixtures."""
    path = Path(__file__).resolve().parents[1] / "fixtures" / "flow" / "valid-loan-en-hi.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["flow"]


def load_text_only_flow() -> dict[str, Any]:
    """Load a flow with both TTS voices explicitly disabled."""
    return {
        **load_valid_flow(),
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
async def test_draft_cas_and_revision_conflict(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """CAS prevents concurrent overwrite; wrong expected_revision returns 409 REVISION_CONFLICT."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "draft_cas@example.com"
    )

    # 1. Create agent (starts at draft_revision 1)
    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "CAS Agent"},
    )
    agent_id = create_res.json()["id"]

    # 2. Patch draft with expected_revision=1 -> succeeds, increments to 2
    update_1 = {
        "schema_version": 1,
        "steps": [
            {
                "id": "step_one",
                "title": "First Step",
                "type": "inform",
                "instruction": "Explain service",
                "required": True,
                "messages": [{"id": "m1", "en": "Welcome.", "hi": "स्वागत है।"}],
            }
        ],
    }
    patch_1_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": update_1},
    )
    assert patch_1_res.status_code == 200
    assert patch_1_res.json()["draft_revision"] == 2
    authoring_hash_1 = patch_1_res.json()["authoring_hash"]

    # 3. Concurrent / stale patch with expected_revision=1 -> 409 REVISION_CONFLICT
    patch_stale_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": update_1},
    )
    assert patch_stale_res.status_code == 409
    assert patch_stale_res.json()["error"]["code"] == "REVISION_CONFLICT"

    # 4. Patch with expected_revision=2 -> succeeds, increments to 3
    update_2 = {
        "schema_version": 1,
        "steps": [
            {
                "id": "step_one",
                "title": "First Step Updated",
                "type": "inform",
                "instruction": "Explain service updated",
                "required": True,
                "messages": [{"id": "m1", "en": "Welcome updated.", "hi": "स्वागत है।"}],
            }
        ],
    }
    patch_2_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 2, "config": update_2},
    )
    assert patch_2_res.status_code == 200
    assert patch_2_res.json()["draft_revision"] == 3
    assert patch_2_res.json()["authoring_hash"] != authoring_hash_1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_structurally_valid_incomplete_autosave(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Schema-valid but semantically incomplete drafts save successfully with diagnostics."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "autosave_incomplete@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Incomplete Draft Agent"},
    )
    agent_id = create_res.json()["id"]

    # Incomplete flow: valid schema, but missing transitions, entry/exit steps, etc.
    incomplete_flow = {
        "schema_version": 1,
        "steps": [
            {
                "id": "only_step",
                "title": "Solo Step",
                "type": "collect",
                "instruction": "Ask for employment",
                "required": True,
                "captures": [
                    {
                        "field": "emp_type",
                        "kind": "string",
                    }
                ],
            }
        ],
    }

    patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": incomplete_flow},
    )
    # Must save successfully (200 OK)
    assert patch_res.status_code == 200
    body = patch_res.json()
    assert body["draft_revision"] == 2
    # But validation summary must report semantic errors!
    assert len(body["validation"]["errors"]) > 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_structurally_invalid_draft_fails_save(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Drafts violating authoring JSON schema fail save with 422 VALIDATION."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "structural_fail@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Bad Schema Agent"},
    )
    agent_id = create_res.json()["id"]

    # Unknown property in authoring flow (schema forbids unknown properties)
    bad_flow = {
        "schema_version": 1,
        "forbidden_unknown_property": "not_allowed",
        "steps": [],
    }

    patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": bad_flow},
    )
    assert patch_res.status_code == 422
    assert patch_res.json()["error"]["code"] == "VALIDATION"

    # Verify revision was NOT incremented
    get_draft = await app_client.get(
        f"/api/v1/agents/{agent_id}/draft",
        headers={"Cookie": headers["Cookie"]},
    )
    assert get_draft.json()["draft_revision"] == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_envelope_unknown_setting_fails_save(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Envelope keys outside the documented setting set are rejected, not silently ignored."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "envelope_unknown@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Envelope Agent"},
    )
    agent_id = create_res.json()["id"]

    envelope = {
        "flow": load_valid_flow(),
        "llm_model": "qwen3.5:4b",
        "unexpected_setting": "not_allowed",
    }
    patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": envelope},
    )
    assert patch_res.status_code == 422
    assert patch_res.json()["error"]["code"] == "VALIDATION"
    fields = patch_res.json()["error"]["fields"]
    assert any(field["field"] == "/unexpected_setting" for field in fields)

    get_draft = await app_client.get(
        f"/api/v1/agents/{agent_id}/draft",
        headers={"Cookie": headers["Cookie"]},
    )
    assert get_draft.json()["draft_revision"] == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_draft_validate_endpoint_and_stale_hash(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """POST /agents/{id}/validate validates draft and detects stale authoring hashes."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "validate_endpoint@example.com"
    )

    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Validate Test Agent"},
    )
    agent_id = create_res.json()["id"]

    # 1. Validate empty draft
    val_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/validate",
        headers=headers,
        json={},
    )
    assert val_res.status_code == 200
    val_body = val_res.json()
    assert val_body["valid"] is False  # Empty flow is not eligible for publish

    # 2. Validate with wrong expected_authoring_hash -> 409 AUTHORING_HASH_STALE
    stale_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/validate",
        headers=headers,
        json={"expected_authoring_hash": "deadbeef" * 8},
    )
    assert stale_res.status_code == 409
    assert stale_res.json()["error"]["code"] == "AUTHORING_HASH_STALE"

    # 3. Patch with complete valid flow
    valid_flow = load_text_only_flow()
    patch_res = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": valid_flow},
    )
    assert patch_res.status_code == 200
    current_hash = patch_res.json()["authoring_hash"]

    # 4. Validate with correct authoring hash -> valid and eligible
    val_valid_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/validate",
        headers=headers,
        json={"expected_authoring_hash": current_hash, "expected_revision": 2},
    )
    assert val_valid_res.status_code == 200
    assert val_valid_res.json()["valid"] is True
    assert val_valid_res.json()["eligible_for_publish"] is True


def test_explicit_missing_catalog_path_fails_closed(tmp_path) -> None:
    with pytest.raises(ValueError):
        load_catalog(tmp_path / "missing-catalog.json")


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_draft_bounds_reject_without_changing_saved_hash(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    _, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "draft_bounds@example.com"
    )
    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Draft Bounds Agent"},
    )
    agent_id = create_res.json()["id"]
    before_res = await app_client.get(
        f"/api/v1/agents/{agent_id}/draft",
        headers={"Cookie": headers["Cookie"]},
    )
    before = before_res.json()

    overlong_setting = {
        **load_text_only_flow(),
        "llm_model": "x" * 65,
    }

    near_limit_flow = load_valid_flow()
    message_step = next(step for step in near_limit_flow["steps"] if "messages" in step)
    messages = [
        {"id": f"limit-message-{index}", "en": "y", "hi": "y"}
        for index in range(32)
    ]
    message_step["messages"] = messages
    limits = FlowLimits()
    target_size = limits.max_flow_bytes - 1
    base_size = len(json.dumps(near_limit_flow).encode("utf-8"))
    extra_chars = target_size - base_size
    assert 0 <= extra_chars <= len(messages) * 8191
    for message in messages:
        added = min(8191, extra_chars)
        message["en"] = "x" * (1 + added)
        extra_chars -= added
    assert extra_chars == 0
    assert len(json.dumps(near_limit_flow).encode("utf-8")) == target_size

    oversized_full_config = {
        **near_limit_flow,
        "llm_model": "qwen3.5:4b",
        "stt_provider": "mlx-audio",
        "tts_voice_en": "",
        "tts_voice_hi": "",
    }
    assert len(json.dumps(oversized_full_config).encode("utf-8")) > limits.max_flow_bytes

    for config in (overlong_setting, oversized_full_config):
        patch_res = await app_client.patch(
            f"/api/v1/agents/{agent_id}/draft",
            headers=headers,
            json={"expected_revision": before["draft_revision"], "config": config},
        )
        assert patch_res.status_code == 422

        after_res = await app_client.get(
            f"/api/v1/agents/{agent_id}/draft",
            headers={"Cookie": headers["Cookie"]},
        )
        after = after_res.json()
        assert after["draft_revision"] == before["draft_revision"]
        assert after["authoring_hash"] == before["authoring_hash"]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_agent_routes_use_the_configured_catalog(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    tmp_path,
):
    catalog_path = tmp_path / "empty-catalog.json"
    catalog_path.write_text(
        json.dumps({"llm_models": [], "stt_providers": [], "tts_voices": []}),
        encoding="utf-8",
    )
    settings = test_platform_settings.model_copy(
        update={"catalog_path": str(catalog_path)}
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            _, headers = await create_test_user_session(
                initialized_pg, settings, client, "configured_catalog@example.com"
            )
            create_res = await client.post(
                "/api/v1/agents",
                headers=headers,
                json={"name": "Configured Catalog Agent"},
            )
            agent_id = create_res.json()["id"]
            patch_res = await client.patch(
                f"/api/v1/agents/{agent_id}/draft",
                headers=headers,
                json={
                    "expected_revision": 1,
                    "config": load_text_only_flow(),
                },
            )
            assert patch_res.status_code == 200
            assert any(
                diagnostic["code"] == "MODEL_UNQUALIFIED"
                for diagnostic in patch_res.json()["validation"]["errors"]
            )

            validate_res = await client.post(
                f"/api/v1/agents/{agent_id}/validate",
                headers={"Cookie": headers["Cookie"]},
                json={},
            )
            assert validate_res.status_code == 200
            assert any(
                diagnostic["code"] == "MODEL_UNQUALIFIED"
                for diagnostic in validate_res.json()["errors"]
            )

            publish_res = await client.post(
                f"/api/v1/agents/{agent_id}/publish",
                headers={**headers, "Idempotency-Key": "configured-catalog-publish"},
                json={"expected_revision": 2},
            )
            assert publish_res.status_code == 422
            assert any(
                field["code"] == "MODEL_UNQUALIFIED"
                for field in publish_res.json()["error"]["fields"]
            )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_restore_draft_from_published_version(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
):
    """Restoring draft from a published version copies the pinned authoring flow into the draft."""
    user, headers = await create_test_user_session(
        initialized_pg, test_platform_settings, app_client, "restore_draft@example.com"
    )

    # 1. Create agent and patch valid flow
    create_res = await app_client.post(
        "/api/v1/agents",
        headers=headers,
        json={"name": "Restore Version Agent"},
    )
    agent_id = create_res.json()["id"]

    valid_flow = load_text_only_flow()
    await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 1, "config": valid_flow},
    )

    # 2. Publish Version 1
    pub_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**headers, "Idempotency-Key": "restore-publish-v1"},
        json={"expected_revision": 2, "changelog": "Initial v1 release"},
    )
    assert pub_res.status_code == 201
    v1_id = pub_res.json()["version_id"]

    # 3. Mutate draft to revision 3 with different steps
    mutated_flow = {
        "schema_version": 1,
        "steps": [
            {
                "id": "mutated_step",
                "title": "Mutated",
                "type": "inform",
                "instruction": "Say something",
                "required": True,
                "messages": [{"id": "m1", "en": "Different.", "hi": "भिन्न।"}],
            }
        ],
    }
    patch_mutated = await app_client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=headers,
        json={"expected_revision": 2, "config": mutated_flow},
    )
    assert patch_mutated.status_code == 200
    assert patch_mutated.json()["draft_revision"] == 3

    # 4. Attempt restore with stale revision 2 -> 409 REVISION_CONFLICT
    stale_restore = await app_client.post(
        f"/api/v1/agents/{agent_id}/restore-draft",
        headers=headers,
        json={"version_id": v1_id, "expected_revision": 2},
    )
    assert stale_restore.status_code == 409
    assert stale_restore.json()["error"]["code"] == "REVISION_CONFLICT"

    # 5. Restore draft from Version 1 with expected_revision=3
    restore_res = await app_client.post(
        f"/api/v1/agents/{agent_id}/restore-draft",
        headers=headers,
        json={"version_id": v1_id, "expected_revision": 3},
    )
    assert restore_res.status_code == 200
    restored_body = restore_res.json()
    assert restored_body["draft_revision"] == 4
    # Content must match the v1 flow!
    assert len(restored_body["config"]["steps"]) == len(valid_flow["steps"])
