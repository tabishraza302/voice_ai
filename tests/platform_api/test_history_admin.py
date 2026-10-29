"""PostgreSQL regression tests for owner history and restricted admin APIs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import httpx
import pytest
from sqlalchemy import select

from platform_api.agents.defaults import DEFAULT_AGENT_TEMPLATES
from platform_api.agents.models import Agent, AgentVersion, IdempotencyRecord
from platform_api.auth.sessions import create_authenticated_session
from platform_api.billing.service import RuntimePrincipal, checkpoint_and_reserve
from platform_api.config import PlatformSettings
from platform_api.events.repository import HistoryRepository
from platform_api.events.schemas import build_event
from platform_api.models.auth import AuditLog, AuthMfa, User
from platform_api.models.session import Session
from tests.support.billing import create_funded_wallet, create_test_session, create_test_user
from tests.support.postgres import FreshPostgres


async def _authenticated_headers(
    initialized_pg: FreshPostgres,
    settings: PlatformSettings,
    client: httpx.AsyncClient,
    *,
    email: str,
    role: str = "user",
    step_up: bool = True,
) -> tuple[User, dict[str, str]]:
    now = datetime.now(timezone.utc)
    async with initialized_pg.sessions() as db:
        user = await create_test_user(db, email=email, role=role)
        if role == "admin":
            db.add(
                AuthMfa(
                    user_id=user.id,
                    secret_ciphertext="encrypted-test-secret",
                    enabled_at=now,
                    last_accepted_step=1,
                )
            )
            await db.flush()
        auth_session, secret = await create_authenticated_session(
            db,
            settings,
            user=user,
            client_ip="127.0.0.1",
            user_agent="History admin regression test",
            now=now,
            mfa_verified=role == "admin",
        )
        if role == "admin":
            auth_session.step_up_at = now if step_up else None
        await db.commit()

    cookie_header = f"{settings.session_cookie_name}={secret}"
    bootstrap = await client.get(
        "/api/v1/auth/session",
        headers={"Origin": settings.web_origin, "Cookie": cookie_header},
    )
    assert bootstrap.status_code == 200
    return user, {
        "Origin": settings.web_origin,
        "Cookie": cookie_header,
        "X-CSRF-Token": bootstrap.json()["csrf_token"],
        "Content-Type": "application/json",
    }


async def _append_transcript_segment(
    initialized_pg: FreshPostgres,
    *,
    session_id: uuid.UUID,
    sequence: int,
    text_content: str,
) -> None:
    event = build_event(
        session_id=session_id,
        sequence=sequence,
        event_type="transcript.segment",
        payload={
            "speaker": "user",
            "final_text": text_content,
            "start_ms": (sequence - 1) * 100,
            "end_ms": sequence * 100,
        },
    )
    async with initialized_pg.sessions() as db:
        async with db.begin():
            await HistoryRepository().consume_event(db, event.to_json())


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_owner_history_snapshot_transcript_and_usage_are_two_owner_scoped(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    owner_a, headers_a = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="history-owner-a@example.com",
    )
    owner_b, headers_b = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="history-owner-b@example.com",
    )
    now = datetime.now(timezone.utc)

    async with initialized_pg.sessions() as db:
        await create_funded_wallet(db, user_id=owner_a.id, credited_minor=1000)
        session_a, _ = await create_test_session(
            db,
            owner_user_id=owner_a.id,
            runner_id="history-runner-a",
            pinned_rate=200,
            created_at=now,
        )
        session_b, _ = await create_test_session(
            db,
            owner_user_id=owner_b.id,
            runner_id="history-runner-b",
            created_at=now + timedelta(seconds=1),
        )
        agent = Agent(
            id=uuid.uuid4(),
            owner_user_id=owner_a.id,
            name="Pinned snapshot agent",
            description="Safe test metadata",
            enabled=True,
            draft_revision=1,
            created_at=now,
            updated_at=now,
        )
        db.add(agent)
        await db.flush()
        version = AgentVersion(
            id=uuid.uuid4(),
            agent_id=agent.id,
            version=1,
            immutable_config_json={
                "persona_prompt": "Pinned safe prompt",
                "flow": {"entry_step_id": "welcome"},
            },
            authoring_hash="a" * 64,
            flow_hash="b" * 64,
            artifact_hash="c" * 64,
            created_at=now,
        )
        db.add(version)
        await db.flush()
        stored_session_a = await db.get(Session, session_a.id)
        assert stored_session_a is not None
        stored_session_a.agent_version_id = version.id
        await db.commit()

    async with initialized_pg.sessions() as db:
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="history-runner-a"),
            session_id=session_a.id,
            operation_id="history-reserve",
            expected_budget_version=1,
            cumulative_billable_ms=0,
            requested_future_ms=10_000,
            billing_slice_ms=10_000,
            permit_ttl_ms=5_000,
            now=now,
        )
        await checkpoint_and_reserve(
            db,
            runtime_principal=RuntimePrincipal(runner_id="history-runner-a"),
            session_id=session_a.id,
            operation_id="history-charge",
            expected_budget_version=2,
            cumulative_billable_ms=5_000,
            requested_future_ms=5_000,
            billing_slice_ms=10_000,
            permit_ttl_ms=5_000,
            now=now + timedelta(milliseconds=500),
        )

    await _append_transcript_segment(
        initialized_pg,
        session_id=session_a.id,
        sequence=1,
        text_content="owner A private transcript",
    )

    page_a = await app_client.get("/api/v1/sessions", headers=headers_a)
    page_b = await app_client.get("/api/v1/sessions", headers=headers_b)
    assert page_a.status_code == page_b.status_code == 200
    assert [row["session_id"] for row in page_a.json()["items"]] == [str(session_a.id)]
    assert [row["session_id"] for row in page_b.json()["items"]] == [str(session_b.id)]

    snapshot_a = await app_client.get(
        f"/api/v1/sessions/{session_a.id}/config-snapshot", headers=headers_a
    )
    assert snapshot_a.status_code == 200
    assert snapshot_a.json()["agent_version_id"] == str(version.id)
    assert snapshot_a.json()["immutable_config"]["persona_prompt"] == "Pinned safe prompt"
    snapshot_b = await app_client.get(
        f"/api/v1/sessions/{session_a.id}/config-snapshot", headers=headers_b
    )
    assert snapshot_b.status_code == 404

    transcript_a = await app_client.get(
        f"/api/v1/sessions/{session_a.id}/transcript", headers=headers_a
    )
    transcript_b = await app_client.get(
        f"/api/v1/sessions/{session_a.id}/transcript", headers=headers_b
    )
    assert transcript_a.status_code == 200
    assert transcript_a.json()["segments"][0]["final_text"] == "owner A private transcript"
    assert transcript_b.status_code == 404

    usage_a = await app_client.get("/api/v1/usage", headers=headers_a)
    usage_b = await app_client.get("/api/v1/usage", headers=headers_b)
    assert usage_a.status_code == usage_b.status_code == 200
    usage_item = usage_a.json()["items"][0]
    assert usage_item["session"]["session_id"] == str(session_a.id)
    assert usage_item["billing"] == {
        "budget_status": "OPEN",
        "charge_status": "provisional",
        "pinned_rate_minor_per_minute": 200,
        "billed_ms": 5_000,
        "authorized_ms": 10_000,
        "authorized_estimate_minor": 34,
        "charged_minor": 17,
        "reserved_minor": 17,
        "budget_version": 3,
        "waived_unknown_usage": False,
    }
    assert [item["session"]["session_id"] for item in usage_b.json()["items"]] == [
        str(session_b.id)
    ]

    guest_history = await app_client.get("/api/v1/sessions")
    assert guest_history.status_code == 401


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_owner_history_cursor_is_bound_to_owner_and_filters(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    owner_a, headers_a = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="cursor-owner-a@example.com",
    )
    _, headers_b = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="cursor-owner-b@example.com",
    )
    async with initialized_pg.sessions() as db:
        for index in range(3):
            await create_test_session(
                db,
                owner_user_id=owner_a.id,
                runner_id=f"cursor-runner-{index}",
                created_at=datetime.now(timezone.utc) + timedelta(seconds=index),
            )

    first_page = await app_client.get("/api/v1/sessions?limit=1", headers=headers_a)
    assert first_page.status_code == 200
    cursor = first_page.json()["next_cursor"]
    assert cursor

    wrong_filter = await app_client.get(
        "/api/v1/sessions",
        params={"cursor": cursor, "state": "ACTIVE"},
        headers=headers_a,
    )
    wrong_owner = await app_client.get(
        "/api/v1/sessions",
        params={"cursor": cursor},
        headers=headers_b,
    )
    assert wrong_filter.status_code == 422
    assert wrong_owner.status_code == 422


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_transcript_access_requires_step_up_csrf_reason_and_audit(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    owner, owner_headers = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="transcript-owner@example.com",
    )
    admin, admin_headers = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="transcript-admin@example.com",
        role="admin",
        step_up=True,
    )
    _, stale_admin_headers = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="transcript-stale-admin@example.com",
        role="admin",
        step_up=False,
    )
    async with initialized_pg.sessions() as db:
        session, _ = await create_test_session(db, owner_user_id=owner.id)
    transcript = "restricted transcript content"
    await _append_transcript_segment(
        initialized_pg,
        session_id=session.id,
        sequence=1,
        text_content=transcript,
    )

    session_metadata = await app_client.get(
        f"/api/v1/admin/sessions/{session.id}", headers=admin_headers
    )
    assert session_metadata.status_code == 200
    assert session_metadata.json()["state"] == "ACTIVE"
    assert "transcript" not in session_metadata.json()
    assert transcript not in session_metadata.text

    access_path = f"/api/v1/admin/sessions/{session.id}/transcript-access"
    reason = "Support case CS-4812 requires a transcript review"
    body = {"reason": reason, "limit": 20}
    headers = {**admin_headers, "Idempotency-Key": "transcript-access-4812"}

    denied_owner = await app_client.post(
        access_path,
        headers={**owner_headers, "Idempotency-Key": "owner-cannot-read"},
        json=body,
    )
    denied_step_up = await app_client.post(
        access_path,
        headers={**stale_admin_headers, "Idempotency-Key": "stale-admin-cannot-read"},
        json=body,
    )
    missing_csrf_headers = dict(headers)
    missing_csrf_headers.pop("X-CSRF-Token")
    denied_csrf = await app_client.post(
        access_path,
        headers=missing_csrf_headers,
        json=body,
    )
    denied_reason = await app_client.post(
        access_path,
        headers={**admin_headers, "Idempotency-Key": "missing-support-reason"},
        json={"reason": "short"},
    )
    assert denied_owner.status_code == 403
    assert denied_step_up.status_code == 403
    assert denied_step_up.json()["error"]["code"] == "STEP_UP_REQUIRED"
    assert denied_csrf.status_code == 403
    assert denied_csrf.json()["error"]["code"] == "CSRF_INVALID"
    assert denied_reason.status_code == 422

    first = await app_client.post(access_path, headers=headers, json=body)
    replay = await app_client.post(access_path, headers=headers, json=body)
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["segments"][0]["final_text"] == transcript

    async with initialized_pg.sessions() as db:
        audit_rows = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "admin.conversation.transcript.view",
                        AuditLog.target_id == session.id,
                    )
                )
            ).scalars().all()
        )
        records = list(
            (
                await db.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.user_id == admin.id,
                        IdempotencyRecord.action == "admin.transcript.access",
                        IdempotencyRecord.idempotency_key == "transcript-access-4812",
                    )
                )
            ).scalars().all()
        )
    assert len(audit_rows) == 1
    assert audit_rows[0].reason == reason
    assert transcript not in str(audit_rows[0].change_summary)
    assert len(records) == 1
    assert transcript not in str(records[0].response_json)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_transcript_cursor_conflicts_after_revision_changes(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    owner, _ = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="revision-owner@example.com",
    )
    _, headers = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="revision-admin@example.com",
        role="admin",
        step_up=True,
    )
    async with initialized_pg.sessions() as db:
        session, _ = await create_test_session(db, owner_user_id=owner.id)
    await _append_transcript_segment(
        initialized_pg,
        session_id=session.id,
        sequence=1,
        text_content="first transcript line",
    )
    await _append_transcript_segment(
        initialized_pg,
        session_id=session.id,
        sequence=2,
        text_content="second transcript line",
    )

    path = f"/api/v1/admin/sessions/{session.id}/transcript-access"
    first = await app_client.post(
        path,
        headers={**headers, "Idempotency-Key": "revision-page-one"},
        json={"reason": "Reviewing support case CS-7120", "limit": 1},
    )
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor

    await _append_transcript_segment(
        initialized_pg,
        session_id=session.id,
        sequence=3,
        text_content="late transcript line",
    )
    stale_page = await app_client.post(
        path,
        headers={**headers, "Idempotency-Key": "revision-page-two"},
        json={
            "reason": "Reviewing support case CS-7120",
            "cursor": cursor,
            "limit": 1,
        },
    )
    assert stale_page.status_code == 409
    audit_rows = await _audit_count(initialized_pg, session.id)
    assert audit_rows == 1


async def _audit_count(initialized_pg: FreshPostgres, session_id: uuid.UUID) -> int:
    async with initialized_pg.sessions() as db:
        rows = await db.execute(
            select(AuditLog.id).where(
                AuditLog.action == "admin.conversation.transcript.view",
                AuditLog.target_id == session_id,
            )
        )
        return len(rows.all())


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_wallet_reads_the_shared_owner_ledger(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    owner, owner_headers = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="wallet-history-owner@example.com",
    )
    _, admin_headers = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="wallet-history-admin@example.com",
        role="admin",
        step_up=True,
    )
    async with initialized_pg.sessions() as db:
        await create_funded_wallet(db, user_id=owner.id, credited_minor=275)

    admin_wallet = await app_client.get(
        f"/api/v1/admin/users/{owner.id}/wallet", headers=admin_headers
    )
    admin_entries = await app_client.get(
        f"/api/v1/admin/users/{owner.id}/wallet/entries", headers=admin_headers
    )
    owner_wallet = await app_client.get("/api/v1/wallet", headers=owner_headers)
    assert admin_wallet.status_code == admin_entries.status_code == owner_wallet.status_code == 200
    assert admin_wallet.json() == owner_wallet.json()
    assert admin_wallet.json()["posted_minor"] == 275
    assert admin_entries.json()["items"][0]["kind"] == "credit"
    assert admin_entries.json()["items"][0]["amount_minor"] == 275
    overview = await app_client.get("/api/v1/admin/overview", headers=admin_headers)
    assert overview.status_code == 200
    assert overview.json()["wallet_posted_minor"] == 275
    assert overview.json()["wallet_reserved_minor"] == 0
    assert overview.json()["wallet_available_minor"] == 275
    assert overview.json()["provisional_charged_minor"] == 0
    assert overview.json()["settled_charged_minor"] == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_config_versions_are_validated_idempotent_and_audited(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    admin, headers = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="version-admin@example.com",
        role="admin",
        step_up=True,
    )
    template = next(
        item for item in DEFAULT_AGENT_TEMPLATES if item["slug"] == "loan-qualification"
    )
    template_payload = {
        "name": "Loan template reviewed",
        "description": "Support-approved template revision",
        "flow_json": template["flow"],
        "reason": "Approved authoring flow for release review",
    }
    template_path = f"/api/v1/admin/templates/{template['slug']}/versions"
    secret_template = await app_client.post(
        template_path,
        headers={**headers, "Idempotency-Key": "reject-template-secret"},
        json={
            **template_payload,
            "flow_json": {"api_key": "private-provider-key-not-for-storage"},
        },
    )
    assert secret_template.status_code == 422
    assert "private-provider-key-not-for-storage" not in secret_template.text

    template_headers = {**headers, "Idempotency-Key": "template-version-2"}
    template_first = await app_client.post(
        template_path, headers=template_headers, json=template_payload
    )
    template_replay = await app_client.post(
        template_path, headers=template_headers, json=template_payload
    )
    assert template_first.status_code == template_replay.status_code == 201
    assert template_first.json() == template_replay.json()
    assert template_first.json()["slug"] == "loan-qualification"
    assert template_first.json()["version"] == 2
    template_conflict = await app_client.post(
        template_path,
        headers=template_headers,
        json={**template_payload, "description": "Different payload"},
    )
    assert template_conflict.status_code == 409

    prompt_payload = {
        "prompt_text": "Use the published conversation policy and never invent facts.",
        "reason": "Approved policy wording for release review",
    }
    prompt_path = "/api/v1/admin/prompts/default-base/versions"
    prompt_headers = {**headers, "Idempotency-Key": "prompt-version-2"}
    prompt_first = await app_client.post(
        prompt_path, headers=prompt_headers, json=prompt_payload
    )
    prompt_replay = await app_client.post(
        prompt_path, headers=prompt_headers, json=prompt_payload
    )
    assert prompt_first.status_code == prompt_replay.status_code == 201
    assert prompt_first.json() == prompt_replay.json()
    assert prompt_first.json()["version"] == 2
    assert prompt_first.json()["is_active"] is True

    async with initialized_pg.sessions() as db:
        audits = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.actor_id == admin.id,
                        AuditLog.action.in_(
                            [
                                "admin.template.version.create",
                                "admin.prompt.version.create",
                            ]
                        ),
                    )
                )
            ).scalars().all()
        )
    assert len(audits) == 2
    assert {row.reason for row in audits} == {
        template_payload["reason"],
        prompt_payload["reason"],
    }
    assert all(prompt_payload["prompt_text"] not in str(row.change_summary) for row in audits)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_transcript_content_is_absent_from_metadata_views(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    owner, _ = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="metadata-owner@example.com",
    )
    _, admin_headers = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="metadata-admin@example.com",
        role="admin",
        step_up=True,
    )
    async with initialized_pg.sessions() as db:
        session, _ = await create_test_session(db, owner_user_id=owner.id)
    transcript = "metadata views must not contain this private text"
    await _append_transcript_segment(
        initialized_pg,
        session_id=session.id,
        sequence=1,
        text_content=transcript,
    )

    list_response = await app_client.get("/api/v1/admin/sessions", headers=admin_headers)
    detail_response = await app_client.get(
        f"/api/v1/admin/sessions/{session.id}", headers=admin_headers
    )
    assert list_response.status_code == detail_response.status_code == 200
    assert transcript not in list_response.text
    assert transcript not in detail_response.text
    assert all("transcript" not in item for item in list_response.json()["items"])


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_owner_history_and_admin_routes_reject_guest_and_non_admin(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    owner, owner_headers = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="history-admin-guard-owner@example.com",
    )
    async with initialized_pg.sessions() as db:
        session, _ = await create_test_session(db, owner_user_id=owner.id)
        agent = Agent(
            id=uuid.uuid4(),
            owner_user_id=owner.id,
            name="Guarded agent",
            description="",
            enabled=True,
            draft_revision=1,
            created_at=session.created_at,
            updated_at=session.created_at,
        )
        db.add(agent)
        await db.commit()

    owner_routes = [
        ("GET", "/api/v1/sessions", None),
        ("GET", "/api/v1/usage", None),
        ("GET", f"/api/v1/sessions/{session.id}/config-snapshot", None),
        ("GET", f"/api/v1/sessions/{session.id}/transcript", None),
    ]
    for method, path, body in owner_routes:
        guest = await app_client.request(method, path, json=body)
        assert guest.status_code == 401

    admin_get_routes = [
        "/api/v1/admin/overview",
        "/api/v1/admin/sessions",
        f"/api/v1/admin/sessions/{session.id}",
        f"/api/v1/admin/users/{owner.id}/wallet",
        f"/api/v1/admin/users/{owner.id}/wallet/entries",
        "/api/v1/admin/agents",
        "/api/v1/admin/templates",
        "/api/v1/admin/prompts",
        "/api/v1/admin/audit-logs",
    ]
    for path in admin_get_routes:
        guest = await app_client.get(path)
        denied_owner = await app_client.get(path, headers=owner_headers)
        assert guest.status_code == 401
        assert denied_owner.status_code == 403

    admin_posts = [
        (
            f"/api/v1/admin/sessions/{session.id}/transcript-access",
            {"reason": "Support case CS-7771 requires review", "limit": 20},
        ),
        (
            "/api/v1/admin/templates/loan-qualification/versions",
            {
                "name": "Guard test template",
                "description": "",
                "flow_json": next(
                    item["flow"]
                    for item in DEFAULT_AGENT_TEMPLATES
                    if item["slug"] == "loan-qualification"
                ),
                "reason": "Support case CS-7771 requires review",
            },
        ),
        (
            "/api/v1/admin/prompts/default-base/versions",
            {
                "prompt_text": "Use published policy and never invent facts.",
                "reason": "Support case CS-7771 requires review",
            },
        ),
        (
            f"/api/v1/admin/agents/{agent.id}/disable",
            {"reason": "Support case CS-7771 requires review"},
        ),
    ]
    for index, (path, body) in enumerate(admin_posts):
        denied_owner = await app_client.post(
            path,
            headers={
                **owner_headers,
                "Idempotency-Key": f"non-admin-denied-{index}",
            },
            json=body,
        )
        assert denied_owner.status_code == 403


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_agent_disable_is_audited_and_does_not_end_existing_sessions(
    app_client: httpx.AsyncClient,
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
) -> None:
    owner, _ = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="agent-disable-owner@example.com",
    )
    _, headers = await _authenticated_headers(
        initialized_pg,
        test_platform_settings,
        app_client,
        email="agent-disable-admin@example.com",
        role="admin",
        step_up=True,
    )
    async with initialized_pg.sessions() as db:
        session, _ = await create_test_session(db, owner_user_id=owner.id)
        agent = Agent(
            id=uuid.uuid4(),
            owner_user_id=owner.id,
            name="Abusive agent",
            description="",
            enabled=True,
            draft_revision=1,
            created_at=session.created_at,
            updated_at=session.created_at,
        )
        db.add(agent)
        await db.commit()

    path = f"/api/v1/admin/agents/{agent.id}/disable"
    body = {"reason": "Verified abuse report CS-9821"}
    request_headers = {**headers, "Idempotency-Key": "agent-disable-9821"}
    first = await app_client.post(path, headers=request_headers, json=body)
    replay = await app_client.post(path, headers=request_headers, json=body)
    conflict = await app_client.post(
        path,
        headers=request_headers,
        json={"reason": "Verified different abuse report CS-9822"},
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert conflict.status_code == 409
    assert first.json()["enabled"] is False

    async with initialized_pg.sessions() as db:
        stored_agent = await db.get(Agent, agent.id)
        stored_session = await db.get(Session, session.id)
        audits = list(
            (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.action == "admin.agent.disable",
                        AuditLog.target_id == agent.id,
                    )
                )
            ).scalars().all()
        )
    assert stored_agent is not None and stored_agent.enabled is False
    assert stored_session is not None and stored_session.state == "ACTIVE"
    assert len(audits) == 1
    assert audits[0].reason == body["reason"]
    assert audits[0].change_summary == {
        "enabled_before": True,
        "enabled_after": False,
    }
