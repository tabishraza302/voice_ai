"""Task 6 platform admission, local-runner bridge, and teardown contracts."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr
from sqlalchemy import select

from bot.flow.qualification import AgentQualificationSettings
from bot.flow.schemas import AdapterCapabilities
from platform_api.agents.config import DEFAULT_AGENT_SETTINGS
from platform_api.agents.models import IdempotencyRecord
from platform_api.auth.errors import ConflictError, DependencyUnavailableError, ForbiddenError
from platform_api.auth.sessions import create_authenticated_session
from platform_api.auth.types import Principal
from platform_api.billing.models import CallBudget
from platform_api.billing.schemas import BudgetPermit, RuntimeStopAttestation
from platform_api.billing.service import RuntimePrincipal, close_budget
from platform_api.billing.tariffs import append_tariff_version
from platform_api.config import PlatformSettings
from platform_api.models.auth import AuditLog, User
from platform_api.models.session import Session
from platform_api.sessions import admission, service
from platform_api.sessions.models import (
    RuntimeRegistration,
    SessionJoinJti,
    SessionPeer,
    SessionSlot,
)
from platform_api.sessions.schemas import (
    JoinConsumptionRequest,
    RuntimeRegistrationRequest,
    RuntimeReleaseAcknowledgement,
    RuntimeReleaseRequest,
    RuntimeReservation,
    SessionTransitionRequest,
)
from tests.support.billing import (
    create_funded_wallet,
    create_test_session,
    create_test_user,
)
from tests.support.postgres import FreshPostgres


TEST_PINS = dict(DEFAULT_AGENT_SETTINGS)
TEST_QUALIFICATION_PINS = dict(TEST_PINS)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _session_settings(
    settings: PlatformSettings,
    *,
    owner_capacity: int = 1,
    runner_capacity: int = 1,
) -> PlatformSettings:
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
    return settings.model_copy(
        update={
            "enable_voice_test": True,
            "enable_public_calls": False,
            "enable_real_paid_calls": False,
            "session_runner_id": "runner-task6-test",
            "session_runner_url": "http://127.0.0.1:43000",
            "runtime_service_token": SecretStr("local-runtime-token-" + "x" * 32),
            "session_runner_capacity": runner_capacity,
            "session_owner_capacity": owner_capacity,
            "session_signaling_url": "http://127.0.0.1:43001/signal",
            "session_join_issuer": "platform-task6-test",
            "session_join_private_key_b64": SecretStr(_b64url(private_bytes)),
            "session_join_public_key_b64": SecretStr(_b64url(public_bytes)),
            "session_join_ttl_seconds": 180,
            "session_max_duration_ms": 60_000,
            "session_setup_timeout_seconds": 60,
            "session_reconnect_grace_seconds": 10,
            "session_runtime_timeout_seconds": 2.0,
        }
    )


def _qualification() -> tuple[AdapterCapabilities, AgentQualificationSettings]:
    return (
        AdapterCapabilities(
            adapter_id="contract-test-adapter",
            adapter_version="1.0.0",
            model_revision="pinned-test-revision",
            metadata="separate_stream",
            speech_gating=True,
            cancel_generation=True,
            playback="qualified_fragment_completion",
            locales=["en", "hi"],
            report_hash="a" * 64,
        ),
        AgentQualificationSettings(**TEST_QUALIFICATION_PINS),
    )


async def _owner_auth(
    db,
    settings: PlatformSettings,
) -> tuple[object, object, Principal]:
    user = await create_test_user(db)
    now = datetime.now(timezone.utc)
    auth_session, _ = await create_authenticated_session(
        db,
        settings,
        user=user,
        client_ip="127.0.0.1",
        user_agent="Task 6 contract test",
        now=now,
    )
    await db.commit()
    principal = Principal(
        user_id=user.id,
        role="user",
        session_id=auth_session.id,
        email_verified_at=user.email_verified_at,
        password_verified_at=auth_session.password_verified_at,
        mfa_verified_at=auth_session.mfa_verified_at,
        step_up_at=auth_session.step_up_at,
    )
    return user, auth_session, principal


async def _fake_owned_voice_version(*args, **kwargs):
    return None, None, {"model_pins": dict(TEST_PINS)}

async def _fake_session_model_identity(*args, **kwargs):
    """Match the synthetic version used by the funding and retry tests."""



@pytest.mark.asyncio
async def test_runtime_configuration_fails_closed_and_bridge_binds_acknowledgements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_platform_settings = PlatformSettings(
        database_url="postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:5439/voice_ai_test",
        auth_csrf_key=base64.urlsafe_b64encode(b"C" * 32).decode("ascii"),
        auth_rate_key=base64.urlsafe_b64encode(b"R" * 32).decode("ascii"),
        auth_mfa_key=base64.urlsafe_b64encode(b"M" * 32).decode("ascii"),
    )
    settings = _session_settings(test_platform_settings)
    config = admission.session_admission_config(settings)
    assert config.runner_id == "runner-task6-test"
    assert config.runner_url == "http://127.0.0.1:43000"

    with pytest.raises(DependencyUnavailableError):
        admission.session_admission_config(
            settings.model_copy(update={"enable_voice_test": False})
        )
    with pytest.raises(DependencyUnavailableError):
        admission.session_admission_config(
            settings.model_copy(update={"session_runner_url": "http://runner.invalid:43000"})
        )
    with pytest.raises(DependencyUnavailableError):
        admission.session_admission_config(
            settings.model_copy(update={"enable_public_calls": True})
        )
    with pytest.raises(DependencyUnavailableError):
        admission.session_admission_config(
            settings.model_copy(
                update={"session_join_public_key_b64": SecretStr(_b64url(b"z" * 32))}
            )
        )

    session_id = uuid.uuid4()
    registration_id = uuid.uuid4()
    version_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    command = RuntimeRegistrationRequest(
        session_id=session_id,
        runner_id=config.runner_id,
        registration_id=registration_id,
        config_version_id=version_id,
        session_version=2,
        connect_deadline=now + timedelta(seconds=30),
        hard_end_deadline=now + timedelta(minutes=1),
    )
    reservation = RuntimeReservation(
        session_id=session_id,
        runner_id=config.runner_id,
        registration_id=registration_id,
        expires_at=now + timedelta(seconds=20),
    )
    response_body = reservation.model_dump(mode="json")
    response_status = [201]
    client_options: list[dict[str, object]] = []
    requests: list[tuple[str, dict[str, object], dict[str, str]]] = []

    class FakeClient:
        def __init__(self, **kwargs):
            client_options.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, json, headers):
            requests.append((url, json, headers))
            return httpx.Response(
                response_status[0],
                json=response_body,
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(admission.httpx, "AsyncClient", FakeClient)
    accepted = await admission.register_runtime(config, command)
    assert accepted == reservation
    assert requests[0][0] == (
        f"{config.runner_url}/internal/runtime/sessions/{session_id}/register"
    )
    assert requests[0][1]["registration_id"] == str(registration_id)
    assert requests[0][2]["X-Runner-ID"] == config.runner_id
    assert client_options[0]["trust_env"] is False
    assert client_options[0]["follow_redirects"] is False

    release_command = RuntimeReleaseRequest(
        session_id=session_id,
        runner_id=config.runner_id,
        registration_id=registration_id,
        session_version=4,
    )
    response_body.clear()
    response_status[0] = 200
    response_body.update(
        RuntimeReleaseAcknowledgement(
            session_id=session_id,
            runner_id=config.runner_id,
            registration_id=registration_id,
            session_version=3,
            released=True,
        ).model_dump(mode="json")
    )
    with pytest.raises(DependencyUnavailableError) as error:
        await admission.release_runtime(config, release_command)
    assert error.value.retryable


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_owner_and_runtime_session_routes_are_mounted(
    app_client: httpx.AsyncClient,
) -> None:
    session_id = uuid.uuid4()
    owner_response = await app_client.get(f"/api/v1/sessions/{session_id}")
    assert owner_response.status_code == 401

    runtime_response = await app_client.get(
        f"/internal/sessions/{session_id}/runtime-config"
    )
    assert runtime_response.status_code == 503



@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admission_funds_before_jti_and_releases_capacity_before_budget_close(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    qualified_test_llm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _session_settings(test_platform_settings, owner_capacity=1, runner_capacity=1)
    capabilities, qualification_settings = _qualification()
    monkeypatch.setattr(service, "_load_owned_version", _fake_owned_voice_version)
    monkeypatch.setattr(service, "_validate_session_model_identity", _fake_session_model_identity)
    registration_calls: list[RuntimeRegistrationRequest] = []
    release_calls: list[RuntimeReleaseRequest] = []

    async def register_bridge(config, command):
        registration_calls.append(command)
        return RuntimeReservation(
            session_id=command.session_id,
            runner_id=command.runner_id,
            registration_id=command.registration_id,
            expires_at=command.connect_deadline,
        )

    async def release_bridge(config, command):
        release_calls.append(command)
        return RuntimeReleaseAcknowledgement(
            session_id=command.session_id,
            runner_id=command.runner_id,
            registration_id=command.registration_id,
            session_version=command.session_version,
            released=True,
        )

    monkeypatch.setattr(service, "register_with_runtime", register_bridge)
    monkeypatch.setattr(service, "release_from_runtime", release_bridge)
    runtime_principal = RuntimePrincipal(
        runner_id="runner-task6-test",
        service_id="voice-runtime",
    )
    agent_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()

    async with initialized_pg.sessions() as db:
        owner, auth_session, principal = await _owner_auth(db, settings)
        auth_session_id = auth_session.id
        await create_funded_wallet(db, user_id=owner.id, credited_minor=10_000)
        registered = await service.reserve_session(
            db,
            principal=principal,
            auth_session_id=auth_session_id,
            origin=settings.web_origin,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            capabilities=capabilities,
            qualification_settings=qualification_settings,
            operation_id="task6-admit-once",
            settings=settings,
        )
        config = admission.session_runtime_config(settings)
        winning_jti = (
            await db.execute(
                select(SessionJoinJti).where(
                    SessionJoinJti.session_id == registered.session_id
                )
            )
        ).scalar_one()
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == registered.session_id)
            )
        ).scalar_one()
        # A concurrent retry may reach finalization after the winner commits.
        replayed_jti = await service._finalize_registered_session(
            db,
            session_id=registered.session_id,
            owner_user_id=owner.id,
            operation_id="task6-admit-once",
            config=config,
            permit=BudgetPermit(
                session_id=registered.session_id,
                budget_version=budget.version,
                proven_ms=budget.cumulative_proven_ms,
                authorized_through_ms=budget.cumulative_authorized_ms,
                reserved_minor=budget.reserved_minor,
                permit_id=uuid.uuid4(),
                expires_at=registered.join_details.expires_at,
                funds_exhausted=False,
            ),
            now=datetime.now(timezone.utc),
        )
        assert replayed_jti == winning_jti.jti
        join_rows = (
            await db.execute(
                select(SessionJoinJti).where(
                    SessionJoinJti.session_id == registered.session_id
                )
            )
        ).scalars().all()
        assert len(join_rows) == 1
        await db.commit()
        replay = await service.reserve_session(
            db,
            principal=principal,
            auth_session_id=auth_session_id,
            origin=settings.web_origin,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            capabilities=capabilities,
            qualification_settings=qualification_settings,
            operation_id="task6-admit-once",
            settings=settings,
        )
        assert registered.session_id == replay.session_id
        assert registered.join_details.join_token == replay.join_details.join_token
        assert len(registration_calls) == 1
        claims = jwt.decode(
            registered.join_details.join_token,
            config.join_public_key,
            algorithms=["EdDSA"],
            audience="voice-runtime",
            issuer=config.join_issuer,
        )
        assert claims["session_id"] == str(registered.session_id)
        assert claims["auth_session_id"] == str(auth_session_id)
        assert claims["origin"] == settings.web_origin
        assert claims["runner_id"] == config.runner_id
        assert claims["registration_id"] == str(registration_calls[0].registration_id)

        with pytest.raises(ForbiddenError):
            await service.consume_join(
                db,
                session_id=registered.session_id,
                runtime_principal=runtime_principal,
                payload=JoinConsumptionRequest(
                    join_token=registered.join_details.join_token,
                    peer_id=uuid.uuid4(),
                    origin="http://127.0.0.1:3002",
                ),
                settings=settings,
            )
        await db.rollback()


        peer_id = uuid.uuid4()
        consumed = await service.consume_join(
            db,
            session_id=registered.session_id,
            runtime_principal=runtime_principal,
            payload=JoinConsumptionRequest(
                join_token=registered.join_details.join_token,
                peer_id=peer_id,
                origin=settings.web_origin,
            ),
            settings=settings,
        )
        assert consumed.peer_id == peer_id
        assert consumed.state == "CONNECTING"
        with pytest.raises(ConflictError):
            await service.consume_join(
                db,
                session_id=registered.session_id,
                runtime_principal=runtime_principal,
                payload=JoinConsumptionRequest(
                    join_token=registered.join_details.join_token,
                    peer_id=uuid.uuid4(),
                    origin=settings.web_origin,
                ),
                settings=settings,
            )
        await db.rollback()


        second_owner, second_auth, second_principal = await _owner_auth(db, settings)
        second_auth_id = second_auth.id
        with pytest.raises(ConflictError, match="Owner session capacity"):
            await service.reserve_session(
                db,
                principal=principal,
                auth_session_id=auth_session_id,
                origin=settings.web_origin,
                agent_id=agent_id,
                agent_version_id=uuid.uuid4(),
                capabilities=capabilities,
                qualification_settings=qualification_settings,
                operation_id="task6-owner-capacity",
                settings=settings,
            )
        await db.rollback()

        with pytest.raises(ConflictError, match="Configured runner capacity"):
            await service.reserve_session(
                db,
                principal=second_principal,
                auth_session_id=second_auth_id,
                origin=settings.web_origin,
                agent_id=agent_id,
                agent_version_id=uuid.uuid4(),
                capabilities=capabilities,
                qualification_settings=qualification_settings,
                operation_id="task6-runner-capacity",
                settings=settings,
            )
        await db.rollback()

        ending = await service.end_owner_session(
            db,
            session_id=registered.session_id,
            principal=principal,
            auth_session_id=auth_session_id,
            origin=settings.web_origin,
            operation_id="task6-end-once",
            settings=settings,
        )
        assert ending.state == "ENDING"
        assert len(release_calls) == 1
        slot = (
            await db.execute(
                select(SessionSlot).where(SessionSlot.session_id == registered.session_id)
            )
        ).scalar_one()
        registration = (
            await db.execute(
                select(RuntimeRegistration).where(
                    RuntimeRegistration.session_id == registered.session_id
                )
            )
        ).scalar_one()
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == registered.session_id)
            )
        ).scalar_one()
        assert slot.released_at is not None
        assert registration.released_at is not None
        assert registration.status == "REGISTERED"
        assert budget.status == "OPEN"

        stopped_at = datetime.now(timezone.utc)
        attestation = RuntimeStopAttestation(
            source="voice-runtime",
            stop_id=uuid.uuid4(),
            session_id=registered.session_id,
            runner_id=config.runner_id,
            registration_id=registration.registration_id,
            session_version=ending.session_version,
            stopped_at=stopped_at,
            reason="OWNER_STOP",
        )
        await close_budget(
            db,
            runtime_principal=runtime_principal,
            session_id=registered.session_id,
            operation_id=f"task6-stop-{registered.session_id}",
            expected_budget_version=budget.version,
            cumulative_billable_ms=budget.cumulative_proven_ms,
            stop_evidence=attestation.model_dump(mode="json"),
            stop_attestation=attestation,
            now=stopped_at,
        )
        terminal = await service.transition_runtime_session(
            db,
            session_id=registered.session_id,
            runtime_principal=runtime_principal,
            payload=SessionTransitionRequest(
                registration_id=registration.registration_id,
                expected_version=ending.session_version,
                target_state="COMPLETED",
            ),
            settings=settings,
        )
        assert terminal.state == "COMPLETED"
        registration_after_close = (
            await db.execute(
                select(RuntimeRegistration).where(
                    RuntimeRegistration.session_id == registered.session_id
                )
            )
        ).scalar_one()
        assert registration_after_close.status == "RELEASED"
        peer = (
            await db.execute(
                select(SessionPeer).where(SessionPeer.session_id == registered.session_id)
            )
        ).scalar_one()
        assert peer.peer_id == peer_id
        jti = (
            await db.execute(
                select(SessionJoinJti).where(SessionJoinJti.session_id == registered.session_id)
            )
        ).scalar_one()
        assert jti.consumed_peer_id == peer_id
        assert jti.consumed_at is not None
        stored_session = (
            await db.execute(select(Session).where(Session.id == registered.session_id))
        ).scalar_one()
        assert stored_session.state == "COMPLETED"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admission_retry_uses_the_session_tariff_after_active_rate_changes(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    qualified_test_llm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry reuses the rate/slice/TTL pinned before a current tariff change."""
    settings = _session_settings(test_platform_settings)
    capabilities, qualification_settings = _qualification()
    monkeypatch.setattr(service, "_load_owned_version", _fake_owned_voice_version)
    monkeypatch.setattr(service, "_validate_session_model_identity", _fake_session_model_identity)
    start = datetime.now(timezone.utc)
    monkeypatch.setattr(service, "_utc_now", lambda: start)
    registration_calls: list[RuntimeRegistrationRequest] = []
    agent_id = uuid.uuid4()
    agent_version_id = uuid.uuid4()
    pause_checks: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def allow_admission(db, owner_user_id, checked_agent_id):
        pause_checks.append((owner_user_id, checked_agent_id))

    monkeypatch.setattr(service, "check_admission_pause", allow_admission)

    async def register_bridge(config, command):
        registration_calls.append(command)
        return RuntimeReservation(
            session_id=command.session_id,
            runner_id=command.runner_id,
            registration_id=command.registration_id,
            expires_at=command.connect_deadline,
        )

    monkeypatch.setattr(service, "register_with_runtime", register_bridge)
    original_finalize = service._finalize_registered_session

    async def crash_after_first_permit(*args, **kwargs):
        raise RuntimeError("simulated interruption after the first permit commit")

    monkeypatch.setattr(
        service, "_finalize_registered_session", crash_after_first_permit
    )

    async with initialized_pg.sessions() as db:
        owner, auth_session, principal = await _owner_auth(db, settings)
        owner_id = owner.id
        auth_session_id = auth_session.id
        await create_funded_wallet(db, user_id=owner_id, credited_minor=10_000)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            await service.reserve_session(
                db,
                principal=principal,
                auth_session_id=auth_session_id,
                origin=settings.web_origin,
                agent_id=agent_id,
                agent_version_id=agent_version_id,
                capabilities=capabilities,
                qualification_settings=qualification_settings,
                operation_id="tariff-pinned-retry",
                settings=settings,
            )

    async with initialized_pg.sessions() as db:
        session = (
            await db.execute(
                select(Session).where(Session.owner_user_id == owner_id)
            )
        ).scalar_one()
        session_id = session.id
        pinned_snapshot = session.tariff_snapshot
        assert pinned_snapshot["tariff_version"] == "v1"
        assert pinned_snapshot["rate_minor_per_minute"] == 6_000
        assert pinned_snapshot["billing_slice_ms"] == 1_000
        assert pinned_snapshot["permit_ttl_ms"] == 5_000

    async with initialized_pg.sessions() as db:
        changed_tariff = await append_tariff_version(
            db,
            expected_revision=1,
            rate_minor_per_minute=12_000,
            now=start + timedelta(seconds=1),
        )
        assert changed_tariff.revision == 2
        assert changed_tariff.rate_minor_per_minute == 12_000
        await db.commit()

    async def paused_replay(db, owner_user_id, checked_agent_id):
        raise ConflictError("Admission is paused")

    monkeypatch.setattr(service, "check_admission_pause", paused_replay)

    monkeypatch.setattr(service, "_finalize_registered_session", original_finalize)
    async with initialized_pg.sessions() as db:
        replay = await service.reserve_session(
            db,
            principal=principal,
            auth_session_id=auth_session_id,
            origin=settings.web_origin,
            agent_id=agent_id,
            agent_version_id=agent_version_id,
            capabilities=capabilities,
            qualification_settings=qualification_settings,
            operation_id="tariff-pinned-retry",
            settings=settings,
        )
        assert pause_checks == [(owner_id, agent_id)]
        assert replay.session_id == session_id
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session_id)
            )
        ).scalar_one()
        stored_session = (
            await db.execute(select(Session).where(Session.id == session_id))
        ).scalar_one()
        assert budget.pinned_rate == 6_000
        assert budget.reserved_minor == 100
        assert budget.version == 2
        assert stored_session.tariff_snapshot == pinned_snapshot
        assert len(registration_calls) == 2

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admission_pause_blocks_new_owner_and_guest_sessions(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    qualified_test_llm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _session_settings(test_platform_settings)
    public_settings = settings.model_copy(
        update={
            "enable_public_calls": True,
            "public_signaling_url": "https://127.0.0.1:43002/api/offer",
        }
    )
    capabilities, qualification_settings = _qualification()
    pause_checks: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def reject_paused_admission(db, owner_user_id, agent_id):
        pause_checks.append((owner_user_id, agent_id))
        raise ConflictError("Admission is paused")

    monkeypatch.setattr(service, "check_admission_pause", reject_paused_admission)
    async with initialized_pg.sessions() as db:
        owner, auth_session, principal = await _owner_auth(db, settings)
        owner_id = owner.id

        async def public_deployment_owner(
            db,
            *,
            deployment_id,
            owner_user_id,
            agent_id,
            agent_version_id,
            origin,
        ):
            assert owner_user_id == owner_id
            verified_owner = await db.get(User, owner_id)
            assert verified_owner is not None
            return verified_owner

        monkeypatch.setattr(
            service, "_verify_public_deployment", public_deployment_owner
        )
        owner_agent_id = uuid.uuid4()
        with pytest.raises(ConflictError, match="Admission is paused"):
            await service.reserve_session(
                db,
                principal=principal,
                auth_session_id=auth_session.id,
                origin=settings.web_origin,
                agent_id=owner_agent_id,
                agent_version_id=uuid.uuid4(),
                capabilities=capabilities,
                qualification_settings=qualification_settings,
                operation_id="paused-owner-admission",
                settings=settings,
            )
        await db.rollback()

        guest_agent_id = uuid.uuid4()
        with pytest.raises(ConflictError, match="Admission is paused"):
            await service.reserve_guest_session(
                db,
                owner_user_id=owner_id,
                deployment_id=uuid.uuid4(),
                origin=public_settings.web_origin,
                agent_id=guest_agent_id,
                agent_version_id=uuid.uuid4(),
                capabilities=capabilities,
                qualification_settings=qualification_settings,
                operation_id="paused-guest-admission",
                capability_digest="a" * 64,
                settings=public_settings,
            )
        await db.rollback()
        sessions = (
            await db.execute(select(Session).where(Session.owner_user_id == owner_id))
        ).scalars().all()
        assert sessions == []
        assert pause_checks == [
            (owner_id, owner_agent_id),
            (owner_id, guest_agent_id),
        ]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_admin_termination_is_audited_idempotent_and_waits_for_runner_ack(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _session_settings(test_platform_settings)
    now = datetime.now(timezone.utc)
    release_calls: list[RuntimeReleaseRequest] = []

    async def release_bridge(config, command):
        release_calls.append(command)
        return RuntimeReleaseAcknowledgement(
            session_id=command.session_id,
            runner_id=command.runner_id,
            registration_id=command.registration_id,
            session_version=command.session_version,
            released=True,
        )

    monkeypatch.setattr(service, "release_from_runtime", release_bridge)
    async with initialized_pg.sessions() as db:
        admin, admin_auth, _ = await _owner_auth(db, settings)
        second_auth, _ = await create_authenticated_session(
            db,
            settings,
            user=admin,
            client_ip="127.0.0.1",
            user_agent="Second admin session",
            now=now,
        )
        owner = await create_test_user(db)
        session, _ = await create_test_session(
            db,
            owner_user_id=owner.id,
            runner_id=settings.session_runner_id,
            state="ACTIVE",
            created_at=now,
        )
        registration_id = uuid.uuid4()
        db.add(
            RuntimeRegistration(
                session_id=session.id,
                runner_id=settings.session_runner_id,
                registration_id=registration_id,
                status="REGISTERED",
                expires_at=now + timedelta(minutes=1),
                created_at=now,
            )
        )
        db.add(
            SessionSlot(
                session_id=session.id,
                owner_user_id=owner.id,
                runner_id=settings.session_runner_id,
                reserved_at=now,
            )
        )
        await db.commit()

        reason = "Operator requested emergency shutdown"
        operation_id = "admin-stop-session-once"
        request_id = uuid.uuid4()
        request_hash = service.compute_payload_hash(
            {
                "admin_user_id": str(admin.id),
                "reason": reason,
                "session_id": str(session.id),
            }
        )
        result = await service.terminate_admin_session(
            db,
            session_id=session.id,
            admin_user_id=admin.id,
            auth_session_id=admin_auth.id,
            operation_id=operation_id,
            request_hash=request_hash,
            reason=reason,
            request_id=request_id,
            source_ip="127.0.0.1",
            settings=settings,
            now=now,
        )
        replay = await service.terminate_admin_session(
            db,
            session_id=session.id,
            admin_user_id=admin.id,
            auth_session_id=second_auth.id,
            operation_id=operation_id,
            request_hash=request_hash,
            reason=reason,
            request_id=uuid.uuid4(),
            source_ip="127.0.0.1",
            settings=settings,
            now=now,
        )
        assert result.state == replay.state == "ENDING"
        assert result.session_version == replay.session_version == 2
        assert len(release_calls) == 1
        assert release_calls[0].session_version == 2

        audit_rows = (
            await db.execute(
                select(AuditLog).where(
                    AuditLog.action == "admin.session.terminate",
                    AuditLog.target_id == session.id,
                )
            )
        ).scalars().all()
        assert len(audit_rows) == 1
        assert audit_rows[0].actor_id == admin.id
        assert audit_rows[0].session_id == admin_auth.id
        assert audit_rows[0].reason == reason
        assert audit_rows[0].change_summary == {
            "from_state": "ACTIVE",
            "to_state": "ENDING",
        }
        idempotency_rows = (
            await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.user_id == admin.id,
                    IdempotencyRecord.idempotency_key == operation_id,
                    IdempotencyRecord.action == "admin.session.terminate",
                )
            )
        ).scalars().all()
        assert len(idempotency_rows) == 1
        stored_session = (
            await db.execute(select(Session).where(Session.id == session.id))
        ).scalar_one()
        assert stored_session.state == "ENDING"
        budget = (
            await db.execute(
                select(CallBudget).where(CallBudget.session_id == session.id)
            )
        ).scalar_one()
        assert budget.status == "OPEN"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_funding_denial_releases_registered_runner_and_closes_any_open_budget(
    test_platform_settings: PlatformSettings,
    initialized_pg: FreshPostgres,
    qualified_test_llm,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _session_settings(test_platform_settings)
    capabilities, qualification_settings = _qualification()
    monkeypatch.setattr(service, "_load_owned_version", _fake_owned_voice_version)
    monkeypatch.setattr(service, "_validate_session_model_identity", _fake_session_model_identity)
    registrations: list[uuid.UUID] = []
    releases: list[RuntimeReleaseRequest] = []

    async def register_bridge(config, command):
        registrations.append(command.registration_id)
        return RuntimeReservation(
            session_id=command.session_id,
            runner_id=command.runner_id,
            registration_id=command.registration_id,
            expires_at=command.connect_deadline,
        )

    async def release_bridge(config, command):
        releases.append(command)
        return RuntimeReleaseAcknowledgement(
            session_id=command.session_id,
            runner_id=command.runner_id,
            registration_id=command.registration_id,
            session_version=command.session_version,
            released=True,
        )

    monkeypatch.setattr(service, "register_with_runtime", register_bridge)
    monkeypatch.setattr(service, "release_from_runtime", release_bridge)

    async with initialized_pg.sessions() as db:
        owner, auth_session, principal = await _owner_auth(db, settings)
        await create_funded_wallet(db, user_id=owner.id, credited_minor=0)
        with pytest.raises(ConflictError, match="funding was denied"):
            await service.reserve_session(
                db,
                principal=principal,
                auth_session_id=auth_session.id,
                origin=settings.web_origin,
                agent_id=uuid.uuid4(),
                agent_version_id=uuid.uuid4(),
                capabilities=capabilities,
                qualification_settings=qualification_settings,
                operation_id="task6-funding-denied",
                settings=settings,
            )
        assert len(registrations) == 1
        assert len(releases) == 1
        session_id = releases[0].session_id
        session = (
            await db.execute(select(Session).where(Session.id == session_id))
        ).scalar_one()
        registration = (
            await db.execute(
                select(RuntimeRegistration).where(RuntimeRegistration.session_id == session_id)
            )
        ).scalar_one()
        slot = (
            await db.execute(select(SessionSlot).where(SessionSlot.session_id == session_id))
        ).scalar_one()
        budget = (
            await db.execute(select(CallBudget).where(CallBudget.session_id == session_id))
        ).scalar_one()
        assert session.state == "FAILED"
        assert session.failure_code == "INSUFFICIENT_FUNDS"
        assert registration.status == "RELEASED"
        assert registration.released_at is not None
        assert slot.released_at is not None
        assert budget.status == "CLOSED"
        assert budget.reserved_minor == 0
