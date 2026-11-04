"""PostgreSQL-backed public guest admission and capability contracts."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr
from sqlalchemy import select

from platform_api.agents.config import DEFAULT_AGENT_SETTINGS
from platform_api.app import create_app
from platform_api.auth.errors import DependencyUnavailableError
from platform_api.auth.sessions import create_authenticated_session
from platform_api.billing.models import CallBudget, WalletAccount
from platform_api.config import PlatformSettings
from platform_api.models.auth import User
from platform_api.models.provider_model_entry import ProviderModelEntry
from platform_api.models.session import Session
from platform_api.sessions.models import (
    RuntimeRegistration,
    SessionJoinJti,
    SessionParticipant,
    SessionPeer,
    SessionSlot,
)
from platform_api.sessions.admission import public_session_admission_config
from platform_api.sessions import service as session_service
from tests.support.billing import create_funded_wallet, create_test_user
from tests.support.postgres import FreshPostgres


@pytest_asyncio.fixture
async def guest_client(
    initialized_pg: FreshPostgres,
    test_platform_settings: PlatformSettings,
    qualified_test_llm,
    monkeypatch: pytest.MonkeyPatch,
):
    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    settings = test_platform_settings.model_copy(
        update={
            "enable_voice_test": True,
            "enable_public_calls": True,
            "enable_real_paid_calls": False,
            "session_runner_id": "runner-task7-test",
            "session_runner_url": "http://127.0.0.1:43000",
            "runtime_service_token": SecretStr("local-runtime-token-" + "x" * 32),
            "session_runner_capacity": 4,
            "session_owner_capacity": 3,
            "session_signaling_url": "http://127.0.0.1:43001/signal",
            "public_signaling_url": "https://127.0.0.1:43001/api/offer",
            "session_join_issuer": "platform-task7-test",
            "session_join_private_key_b64": SecretStr(
                base64.urlsafe_b64encode(private_bytes).decode("ascii").rstrip("=")
            ),
            "session_join_public_key_b64": SecretStr(
                base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")
            ),
            "session_join_ttl_seconds": 180,
            "session_max_duration_ms": 60_000,
            "session_setup_timeout_seconds": 60,
            "session_reconnect_grace_seconds": 10,
            "session_rate_minor_per_minute": 6000,
            "session_tariff_version": "task7-contract-test-1",
            "session_runtime_timeout_seconds": 2.0,
            "billing_slice_ms": 1000,
            "billing_permit_ttl_ms": 60_000,
            "public_admission_ip_limit": 20,
            "public_admission_agent_limit": 20,
            "public_admission_window_seconds": 60,
            "public_tls_qualified": True,
            "public_turn_qualified": True,
        }
    )
    app = create_app(settings)

    async def register_runtime(config, command):
        from platform_api.sessions.schemas import RuntimeReservation

        return RuntimeReservation(
            session_id=command.session_id,
            runner_id=command.runner_id,
            registration_id=command.registration_id,
            expires_at=command.connect_deadline,
        )

    async def release_runtime(config, command):
        from platform_api.sessions.schemas import RuntimeReleaseAcknowledgement

        return RuntimeReleaseAcknowledgement(
            session_id=command.session_id,
            runner_id=command.runner_id,
            registration_id=command.registration_id,
            session_version=command.session_version,
            released=True,
        )

    monkeypatch.setattr(session_service, "register_with_runtime", register_runtime)
    monkeypatch.setattr(session_service, "release_from_runtime", release_runtime)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8000",
        ) as client:
            yield client, settings


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "signaling_url",
    [
        "http://127.0.0.1:43001/api/offer",
        "https://127.0.0.1:43001/signal",
        "https://127.0.0.1:43001/api/offer?target=runner",
    ],
)
async def test_public_signaling_requires_https_api_offer(
    guest_client,
    signaling_url: str,
):
    _client, settings = guest_client
    invalid_settings = settings.model_copy(update={"public_signaling_url": signaling_url})

    with pytest.raises(DependencyUnavailableError):
        public_session_admission_config(invalid_settings)


def _guest_agent_config() -> dict[str, object]:
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "flow"
        / "valid-loan-en-hi.json"
    )
    flow = json.loads(fixture_path.read_text(encoding="utf-8"))["flow"]
    return {
        **flow,
        **DEFAULT_AGENT_SETTINGS,
        "llm_provider": "ollama",
        "llm_model": "qwen3.5:4b",
    }


async def _owner_and_agent(
    initialized_pg: FreshPostgres,
    settings: PlatformSettings,
    client: httpx.AsyncClient,
    *,
    email: str,
    wallet_minor: int = 10_000,
) -> tuple[User, uuid.UUID, uuid.UUID, dict[str, str]]:
    now = datetime.now(timezone.utc)
    async with initialized_pg.sessions() as db:
        owner = await create_test_user(db, email=email)
        _auth_session, secret = await create_authenticated_session(
            db,
            settings,
            user=owner,
            client_ip="127.0.0.1",
            user_agent="Task 7 guest contract test",
            now=now,
        )
        await create_funded_wallet(db, user_id=owner.id, credited_minor=wallet_minor)
        await db.commit()

    cookie = f"{settings.session_cookie_name}={secret}"
    boot = await client.get(
        "/api/v1/auth/session",
        headers={"Origin": settings.web_origin, "Cookie": cookie},
    )
    assert boot.status_code == 200
    owner_headers = {
        "Origin": settings.web_origin,
        "Cookie": cookie,
        "X-CSRF-Token": boot.json()["csrf_token"],
        "Content-Type": "application/json",
    }

    created = await client.post(
        "/api/v1/agents",
        headers=owner_headers,
        json={"name": "Guest contract agent", "description": "Public guest fixture"},
    )
    assert created.status_code == 201, created.text
    agent_id = uuid.UUID(created.json()["id"])
    patched = await client.patch(
        f"/api/v1/agents/{agent_id}/draft",
        headers=owner_headers,
        json={"expected_revision": 1, "config": _guest_agent_config()},
    )
    assert patched.status_code == 200, patched.text
    published = await client.post(
        f"/api/v1/agents/{agent_id}/publish",
        headers={**owner_headers, "Idempotency-Key": f"guest-publish-{agent_id}"},
        json={"expected_revision": 2, "changelog": "Publish guest contract agent"},
    )
    assert published.status_code == 201, published.text
    version_id = uuid.UUID(published.json()["version_id"])
    enabled = await client.post(
        f"/api/v1/agents/{agent_id}/enable",
        headers=owner_headers,
    )
    assert enabled.status_code == 200, enabled.text
    return owner, agent_id, version_id, owner_headers


async def _create_deployment(
    client: httpx.AsyncClient,
    *,
    agent_id: uuid.UUID,
    owner_headers: dict[str, str],
    key: str = "deployment-create",
) -> str:
    response = await client.post(
        f"/api/v1/agents/{agent_id}/public-deployment",
        headers={**owner_headers, "Idempotency-Key": key},
        json={},
    )
    assert response.status_code == 201, response.text
    return response.json()["public_id"]


async def _bootstrap_and_admit(
    client: httpx.AsyncClient,
    settings: PlatformSettings,
    public_id: str,
    *,
    key: str = "guest-admit",
    extra: dict[str, object] | None = None,
    csrf_override: str | None = None,
    origin: str | None = None,
) -> httpx.Response:
    headers = {"Origin": settings.web_origin}
    bootstrap = await client.get(
        f"/api/v1/public/deployments/{public_id}",
        headers=headers,
    )
    if bootstrap.status_code != 200:
        return bootstrap
    payload: dict[str, object] = {
        "disclosure_version": bootstrap.json()["disclosure_version"],
        "disclosure_acknowledged": True,
    }
    if extra:
        payload.update(extra)
    return await client.post(
        f"/api/v1/public/deployments/{public_id}/sessions",
        headers={
            "Origin": origin or settings.web_origin,
            "Content-Type": "application/json",
            "X-Public-CSRF": csrf_override or bootstrap.json()["csrf_token"],
            "Idempotency-Key": key,
        },
        json=payload,
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_guest_admission_uses_deployment_owner_and_scopes_capability_to_one_session(
    guest_client,
    initialized_pg: FreshPostgres,
):
    client, settings = guest_client
    owner_a, agent_a, version_a, owner_headers_a = await _owner_and_agent(
        initialized_pg,
        settings,
        client,
        email="guest-owner-a@example.com",
    )
    owner_b, agent_b, version_b, owner_headers_b = await _owner_and_agent(
        initialized_pg,
        settings,
        client,
        email="guest-owner-b@example.com",
    )
    public_a = await _create_deployment(
        client, agent_id=agent_a, owner_headers=owner_headers_a, key="guest-deploy-a"
    )
    public_b = await _create_deployment(
        client, agent_id=agent_b, owner_headers=owner_headers_b, key="guest-deploy-b"
    )
    assert public_a != public_b

    browser_bootstrap = await client.get(f"/api/v1/public/deployments/{public_a}")
    assert browser_bootstrap.status_code == 200
    assert public_a != str(agent_a)

    # Caller-supplied owner, agent, and version identifiers are rejected, not trusted.
    rejected = await _bootstrap_and_admit(
        client,
        settings,
        public_a,
        key="guest-owner-substitution",
        extra={
            "owner_user_id": str(owner_b.id),
            "agent_id": str(agent_b),
            "agent_version_id": str(version_b),
        },
    )
    assert rejected.status_code == 422

    admitted_a = await _bootstrap_and_admit(client, settings, public_a, key="guest-a")
    admitted_b = await _bootstrap_and_admit(client, settings, public_b, key="guest-b")
    assert admitted_a.status_code == 201, admitted_a.text
    set_cookie = admitted_a.headers["set-cookie"]
    assert admitted_b.status_code == 201, admitted_b.text
    body_a = admitted_a.json()
    body_b = admitted_b.json()
    assert body_a["session_id"] != body_b["session_id"]
    assert "owner_user_id" not in body_a
    assert "wallet" not in body_a
    assert "agent_version_id" not in body_a
    assert "participant_id" not in body_a
    assert "auth_session_id" not in body_a
    assert body_a["signaling_url"] == "https://127.0.0.1:43001/api/offer"

    session_a_id = uuid.UUID(body_a["session_id"])
    async with initialized_pg.sessions() as db:
        session_a = await db.get(Session, session_a_id)
        participant_a = (
            await db.execute(
                select(SessionParticipant).where(
                    SessionParticipant.session_id == session_a_id
                )
            )
        ).scalar_one()
        join_a = (
            await db.execute(
                select(SessionJoinJti).where(SessionJoinJti.session_id == session_a_id)
            )
        ).scalar_one()
        wallet_a = (
            await db.execute(
                select(WalletAccount).where(WalletAccount.user_id == owner_a.id)
            )
        ).scalar_one()
        wallet_b = (
            await db.execute(
                select(WalletAccount).where(WalletAccount.user_id == owner_b.id)
            )
        ).scalar_one()
        assert session_a is not None
        assert session_a.owner_user_id == owner_a.id
        assert session_a.agent_version_id == version_a
        assert session_a.kind == "voice"
        assert session_a.tariff_snapshot["public"] is True
        assert session_a.tariff_snapshot["paid"] is False
        assert participant_a.owner_user_id == owner_a.id
        assert participant_a.kind == "guest"
        assert participant_a.auth_session_id is None
        assert join_a.owner_user_id == owner_a.id
        assert join_a.auth_session_id is None
        assert join_a.participant_kind == "guest"
        assert wallet_a.reserved_minor > 0
        assert wallet_b.reserved_minor > 0

    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()
    assert (
        f"path=/api/v1/guest/sessions/{body_a['session_id']}"
        in set_cookie.lower()
    )
    assert body_a["csrf_token"]

    # A guest capability cannot be moved to another session or an owner-only history route.
    guest_cookie = set_cookie.split(";", 1)[0]
    status_a = await client.get(
        f"/api/v1/guest/sessions/{body_a['session_id']}/status",
        headers={"Origin": settings.web_origin},
    )
    assert status_a.status_code == 200

    same_origin_status = await client.get(
        f"/api/v1/guest/sessions/{body_a['session_id']}/status",
    )
    assert same_origin_status.status_code == 200
    legacy_status = await client.get(
        f"/api/v1/public/sessions/{body_a['session_id']}",
        headers={"Origin": settings.web_origin, "Cookie": guest_cookie},
    )
    assert legacy_status.status_code == 404
    status_b_with_a_capability = await client.get(
        f"/api/v1/guest/sessions/{body_b['session_id']}/status",
        headers={"Origin": settings.web_origin, "Cookie": guest_cookie},
    )
    assert status_b_with_a_capability.status_code in {401, 404}
    owner_history = await client.get(
        f"/api/v1/sessions/{body_a['session_id']}",
        headers={"Origin": settings.web_origin, "Cookie": guest_cookie},
    )
    assert owner_history.status_code == 401


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_public_origin_csrf_disclosure_and_rate_limits_are_enforced(
    guest_client,
    initialized_pg: FreshPostgres,
):
    client, settings = guest_client
    _owner, agent_id, _version_id, owner_headers = await _owner_and_agent(
        initialized_pg,
        settings,
        client,
        email="guest-origin-owner@example.com",
    )
    public_id = await _create_deployment(
        client, agent_id=agent_id, owner_headers=owner_headers
    )

    bootstrap = await client.get(
        f"/api/v1/public/deployments/{public_id}",
        headers={"Origin": settings.web_origin},
    )
    assert bootstrap.status_code == 200
    body = {
        "disclosure_version": bootstrap.json()["disclosure_version"],
        "disclosure_acknowledged": True,
    }
    missing_csrf = await client.post(
        f"/api/v1/public/deployments/{public_id}/sessions",
        headers={"Origin": settings.web_origin, "Content-Type": "application/json", "Idempotency-Key": "missing-csrf"},
        json=body,
    )
    assert missing_csrf.status_code == 403

    wrong_origin = await client.post(
        f"/api/v1/public/deployments/{public_id}/sessions",
        headers={
            "Origin": "https://attacker.example",
            "Content-Type": "application/json",
            "X-Public-CSRF": bootstrap.json()["csrf_token"],
            "Idempotency-Key": "wrong-origin",
        },
        json=body,
    )
    assert wrong_origin.status_code == 403

    no_disclosure = await client.post(
        f"/api/v1/public/deployments/{public_id}/sessions",
        headers={
            "Origin": settings.web_origin,
            "Content-Type": "application/json",
            "X-Public-CSRF": bootstrap.json()["csrf_token"],
            "Idempotency-Key": "no-disclosure",
        },
        json={"disclosure_version": body["disclosure_version"], "disclosure_acknowledged": False},
    )
    assert no_disclosure.status_code == 422

    limited_settings = settings.model_copy(update={"public_admission_ip_limit": 1})
    client._transport.app.state.settings = limited_settings
    first = await _bootstrap_and_admit(client, limited_settings, public_id, key="rate-first")
    assert first.status_code == 201, first.text
    second = await _bootstrap_and_admit(client, limited_settings, public_id, key="rate-second")
    assert second.status_code == 429


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_guest_agent_quota_is_shared_across_deployment_links(
    guest_client,
    initialized_pg: FreshPostgres,
):
    client, settings = guest_client
    _owner, agent_id, _version_id, owner_headers = await _owner_and_agent(
        initialized_pg,
        settings,
        client,
        email="guest-shared-agent-rate@example.com",
    )
    public_a = await _create_deployment(
        client, agent_id=agent_id, owner_headers=owner_headers, key="shared-link-a"
    )
    public_b = await _create_deployment(
        client, agent_id=agent_id, owner_headers=owner_headers, key="shared-link-b"
    )
    assert public_a != public_b

    limited_settings = settings.model_copy(
        update={"public_admission_agent_limit": 1, "public_admission_ip_limit": 20}
    )
    client._transport.app.state.settings = limited_settings
    first, second = await asyncio.gather(
        _bootstrap_and_admit(
            client, limited_settings, public_a, key="shared-agent-a"
        ),
        _bootstrap_and_admit(
            client, limited_settings, public_b, key="shared-agent-b"
        ),
    )

    assert sorted((first.status_code, second.status_code)) == [201, 429]


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/public/deployments/{'p' * 32}/sessions",
        "/api/v1/guest/sessions/00000000-0000-0000-0000-000000000001/reconnect-token",
    ],
)
async def test_oversized_public_guest_posts_are_rejected_before_body_parsing(
    guest_client,
    path: str,
):
    client, settings = guest_client
    response = await client.post(
        path,
        content=b"not-json-" * 1024,
        headers={
            "Origin": settings.web_origin,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 413


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_chunked_public_guest_post_is_bounded_before_body_parsing(guest_client):
    client, settings = guest_client

    class ChunkedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"payload":"' + b"x" * 4096
            yield b"x" * 4096
            yield b'"}'

        async def aclose(self) -> None:
            return None

    response = await client.post(
        f"/api/v1/public/deployments/{'c' * 32}/sessions",
        content=ChunkedBody(),
        headers={
            "Origin": settings.web_origin,
            "Content-Type": "application/json",
            "Transfer-Encoding": "chunked",
        },
    )

    assert response.status_code == 413


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_disabled_deployment_blocks_new_calls_and_reconnect_but_allows_end(
    guest_client,
    initialized_pg: FreshPostgres,
):
    client, settings = guest_client
    owner, agent_id, _version_id, owner_headers = await _owner_and_agent(
        initialized_pg,
        settings,
        client,
        email="guest-disabled-owner@example.com",
    )
    public_id = await _create_deployment(client, agent_id=agent_id, owner_headers=owner_headers)
    admitted = await _bootstrap_and_admit(client, settings, public_id, key="disable-admit")
    assert admitted.status_code == 201, admitted.text
    body = admitted.json()
    session_id = uuid.UUID(body["session_id"])
    csrf = body["csrf_token"]
    guest_cookie = admitted.headers["set-cookie"].split(";", 1)[0]

    disabled = await client.delete(
        f"/api/v1/public/deployments/{public_id}",
        headers={**owner_headers, "Idempotency-Key": "disable-public"},
    )
    assert disabled.status_code == 200
    hidden = await client.get(
        f"/api/v1/public/deployments/{public_id}",
        headers={"Origin": settings.web_origin},
    )
    assert hidden.status_code == 404

    async with initialized_pg.sessions() as db:
        session = await db.get(Session, session_id)
        assert session is not None
        participant = (
            await db.execute(
                select(SessionParticipant).where(
                    SessionParticipant.session_id == session_id,
                    SessionParticipant.kind == "guest",
                )
            )
        ).scalar_one()
        session.state = "RECONNECTING"
        session.version += 1
        peer = SessionPeer(
            session_id=session_id,
            peer_id=uuid.uuid4(),
            participant_id=participant.participant_id,
            runner_id="runner-task7-test",
            status="RELEASED",
            joined_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            released_at=datetime.now(timezone.utc),
            reconnect_until=datetime.now(timezone.utc) + timedelta(seconds=10),
        )
        db.add(peer)
        await db.commit()

    reconnect = await client.post(
        f"/api/v1/guest/sessions/{session_id}/reconnect-token",
        headers={
            "Origin": settings.web_origin,
            "Cookie": guest_cookie,
            "X-Guest-CSRF": csrf,
            "Idempotency-Key": "disabled-reconnect",
            "Content-Type": "application/json",
        },
        json={},
    )
    assert reconnect.status_code == 409

    ended = await client.post(
        f"/api/v1/guest/sessions/{session_id}/end",
        headers={
            "Origin": settings.web_origin,
            "Cookie": guest_cookie,
            "X-Guest-CSRF": csrf,
            "Idempotency-Key": "disabled-end",
            "Content-Type": "application/json",
        },
        json={},
    )
    assert ended.status_code == 202
    assert ended.json()["state"] == "ENDING"

    owner_status = await client.get(
        f"/api/v1/guest/sessions/{session_id}/status",
        headers={"Origin": settings.web_origin, "Cookie": guest_cookie},
    )
    assert owner_status.status_code == 200
    assert "owner_user_id" not in owner_status.json()
    assert owner_status.json()["session_id"] == str(session_id)
    assert owner.id is not None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_expired_guest_capability_is_rejected_and_revoked_on_terminal_state(
    guest_client,
    initialized_pg: FreshPostgres,
):
    client, settings = guest_client
    _owner, agent_id, _version, owner_headers = await _owner_and_agent(
        initialized_pg,
        settings,
        client,
        email="guest-expiry-owner@example.com",
    )
    public_id = await _create_deployment(client, agent_id=agent_id, owner_headers=owner_headers)
    admitted = await _bootstrap_and_admit(client, settings, public_id, key="expiry-admit")
    assert admitted.status_code == 201, admitted.text
    session_id = uuid.UUID(admitted.json()["session_id"])
    guest_cookie = admitted.headers["set-cookie"].split(";", 1)[0]

    from platform_api.public.models import PublicGuestCapability

    async with initialized_pg.sessions() as db:
        capability = await db.get(PublicGuestCapability, session_id)
        assert capability is not None
        expired_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        capability.expires_at = expired_at
        capability.created_at = expired_at - timedelta(seconds=1)
        await db.commit()

    expired = await client.get(
        f"/api/v1/guest/sessions/{session_id}/status",
        headers={"Origin": settings.web_origin, "Cookie": guest_cookie},
    )
    assert expired.status_code == 401


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_guest_admission_rejects_disabled_pinned_model(
    guest_client,
    initialized_pg: FreshPostgres,
):
    client, settings = guest_client
    owner, agent_id, _version_id, owner_headers = await _owner_and_agent(
        initialized_pg,
        settings,
        client,
        email="guest-disabled-model@example.com",
    )
    public_id = await _create_deployment(
        client, agent_id=agent_id, owner_headers=owner_headers
    )

    async with initialized_pg.sessions() as db:
        model = (
            await db.execute(
                select(ProviderModelEntry).where(
                    ProviderModelEntry.provider_id == "ollama",
                    ProviderModelEntry.model_id == "qwen3.5:4b",
                )
            )
        ).scalar_one()
        model.enabled = False
        await db.commit()

    admitted = await _bootstrap_and_admit(
        client, settings, public_id, key="disabled-model-admission"
    )
    assert admitted.status_code == 409, admitted.text
    assert admitted.json()["error"]["message"] == (
        "Published agent runtime model is no longer qualified"
    )

    async with initialized_pg.sessions() as db:
        sessions = (
            await db.execute(
                select(Session).where(Session.owner_user_id == owner.id)
            )
        ).scalars().all()
        assert not sessions


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("remove_model", [False, True], ids=["disabled", "deleted"])
async def test_guest_admission_compensates_when_pinned_model_changes_after_reservation(
    guest_client,
    initialized_pg: FreshPostgres,
    monkeypatch: pytest.MonkeyPatch,
    remove_model: bool,
):
    client, settings = guest_client
    owner, agent_id, _version_id, owner_headers = await _owner_and_agent(
        initialized_pg,
        settings,
        client,
        email=f"guest-model-race-{remove_model}@example.com",
    )
    public_id = await _create_deployment(
        client,
        agent_id=agent_id,
        owner_headers=owner_headers,
        key=f"guest-model-race-deployment-{remove_model}",
    )
    original_register = session_service.register_runtime
    registration_calls = []
    release_calls = []

    async def disable_before_pre_registration(db, **kwargs):
        async with initialized_pg.sessions() as model_db:
            model = await model_db.get(ProviderModelEntry, ("ollama", "qwen3.5:4b"))
            assert model is not None
            if remove_model:
                await model_db.delete(model)
            else:
                model.enabled = False
            await model_db.commit()
        return await original_register(db, **kwargs)

    async def register_runner(config, command):
        from platform_api.sessions.schemas import RuntimeReservation

        registration_calls.append(command)
        return RuntimeReservation(
            session_id=command.session_id,
            runner_id=command.runner_id,
            registration_id=command.registration_id,
            expires_at=command.connect_deadline,
        )

    async def release_runner(config, command):
        from platform_api.sessions.schemas import RuntimeReleaseAcknowledgement

        release_calls.append(command)
        return RuntimeReleaseAcknowledgement(
            session_id=command.session_id,
            runner_id=command.runner_id,
            registration_id=command.registration_id,
            session_version=command.session_version,
            released=True,
        )

    monkeypatch.setattr(session_service, "register_runtime", disable_before_pre_registration)
    monkeypatch.setattr(session_service, "register_with_runtime", register_runner)
    monkeypatch.setattr(session_service, "release_from_runtime", release_runner)

    admitted = await _bootstrap_and_admit(
        client,
        settings,
        public_id,
        key=f"guest-model-race-admission-{remove_model}",
    )
    assert admitted.status_code == 409, admitted.text
    assert admitted.json()["error"]["message"] == (
        "Published agent runtime model is no longer qualified"
    )
    assert registration_calls == []
    assert release_calls == []

    async with initialized_pg.sessions() as db:
        sessions = (
            await db.execute(
                select(Session).where(Session.owner_user_id == owner.id)
            )
        ).scalars().all()
        assert len(sessions) == 1
        session = sessions[0]
        assert session.state == "FAILED"
        assert session.failure_code == "MODEL_UNAVAILABLE"
        registration = await db.get(RuntimeRegistration, session.id)
        assert registration is not None
        assert registration.status == "RELEASED"
        slot = (
            await db.execute(
                select(SessionSlot).where(SessionSlot.session_id == session.id)
            )
        ).scalar_one()
        assert slot.released_at is not None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_zero_credit_admission_is_compensated_without_guest_join_credentials(
    guest_client,
    initialized_pg: FreshPostgres,
):
    client, settings = guest_client
    _owner, agent_id, _version, owner_headers = await _owner_and_agent(
        initialized_pg,
        settings,
        client,
        email="guest-zero-credit@example.com",
        wallet_minor=0,
    )
    public_id = await _create_deployment(client, agent_id=agent_id, owner_headers=owner_headers)
    admitted = await _bootstrap_and_admit(client, settings, public_id, key="zero-credit")
    assert admitted.status_code == 409

    async with initialized_pg.sessions() as db:
        sessions = (
            await db.execute(
                select(Session).where(Session.owner_user_id == _owner.id)
            )
        ).scalars().all()
        assert len(sessions) == 1
        assert sessions[0].state != "REGISTERED"
        assert not (
            await db.execute(
                select(SessionJoinJti).where(SessionJoinJti.session_id == sessions[0].id)
            )
        ).scalars().all()
        registration = await db.get(RuntimeRegistration, sessions[0].id)
        assert registration is not None
        assert registration.status == "RELEASED"
        budget = await db.get(CallBudget, sessions[0].id)
        assert budget is None or budget.status != "OPEN"
