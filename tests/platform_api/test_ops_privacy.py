from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from platform_api.agents.models import Agent
from platform_api.auth.errors import ValidationError
from platform_api.billing.models import WalletEntry
from platform_api.events.models import EventQuarantine, TranscriptSegment, TranscriptState
from platform_api.models.auth import AuditLog, AuthMfa, User
from platform_api.models.session import Session
from platform_api.models.wallet import WalletAccount
from platform_api.ops.privacy import (
    apply_retention,
    export_user_data,
    redact_user_transcripts,
    validate_retention_database_url,
)
from platform_api.public.models import PublicDeployment, PublicGuestCapability
from platform_api.sessions.models import SessionParticipant
from tests.support.postgres import FreshPostgres


async def _seed_privacy_fixture(db, *, created_at: datetime | None = None):
    now = datetime.now(timezone.utc)
    created_at = created_at or now
    user_id = uuid.uuid4()
    session_id = uuid.uuid4()
    event_id = uuid.uuid4()
    deployment_id = uuid.uuid4()
    participant_id = uuid.uuid4()
    capability_digest = "c" * 64
    agent_id = uuid.uuid4()
    public_id = "p" * 32
    user = User(
        id=user_id,
        email=f"privacy-{user_id}@example.test",
        email_key=f"privacy-{user_id}@example.test",
        display_name="Privacy Test",
        password_hash="not-exportable-password-hash",
        role="user",
        status="active",
        email_verified_at=now,
        preferred_language="en",
        timezone="UTC",
        low_balance_notifications=True,
        auth_version=1,
        created_at=now,
        updated_at=now,
    )
    agent = Agent(
        id=agent_id,
        owner_user_id=user_id,
        name="Privacy guest agent",
        description="Public guest export fixture",
        persona_prompt="Be helpful.",
        enabled=True,
        draft_revision=1,
        current_published_version_id=None,
        created_at=created_at,
        updated_at=created_at,
    )
    deployment = PublicDeployment(
        id=deployment_id,
        public_id=public_id,
        owner_user_id=user_id,
        agent_id=agent_id,
        origin="https://guest.example.test",
        disclosure_version="guest-disclosure-v1",
        enabled=True,
        created_at=created_at,
        disabled_at=None,
    )
    wallet = WalletAccount(
        user_id=user_id,
        currency="INR",
        posted_minor=100,
        reserved_minor=0,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session = Session(
        id=session_id,
        owner_user_id=user_id,
        tariff_snapshot={"rate_minor_per_minute": 200, "api_key": "PRIVATE_API_KEY"},
        kind="voice",
        state="COMPLETED",
        version=1,
        created_at=created_at,
        ended_at=created_at,
    )
    participant = SessionParticipant(
        participant_id=participant_id,
        session_id=session_id,
        owner_user_id=user_id,
        auth_session_id=None,
        kind="guest",
        origin="https://guest.example.test",
        created_at=created_at,
        revoked_at=None,
    )
    capability = PublicGuestCapability(
        session_id=session_id,
        deployment_id=deployment_id,
        owner_user_id=user_id,
        participant_id=participant_id,
        capability_digest=capability_digest,
        expires_at=created_at + timedelta(hours=1),
        created_at=created_at,
        revoked_at=None,
    )
    ledger = WalletEntry(
        id=uuid.uuid4(),
        user_id=user_id,
        kind="credit",
        amount_minor=100,
        session_id=None,
        operation_id=f"privacy-{user_id}",
        actor="operator",
        reason="synthetic test credit",
        created_at=created_at,
    )
    transcript = TranscriptSegment(
        event_id=event_id,
        session_id=session_id,
        sequence=1,
        speaker="user",
        final_text="PRIVATE_TRANSCRIPT_TEXT password=PRIVATE_PASSWORD",
        start_ms=0,
        end_ms=1000,
        created_at=created_at,
    )
    transcript_state = TranscriptState(
        session_id=session_id,
        revision=1,
        processing=False,
        last_sequence=1,
        updated_at=created_at,
    )
    audit = AuditLog(
        id=uuid.uuid4(),
        actor_kind="operator",
        actor_id=None,
        session_id=None,
        action="test.privacy.seed",
        target_type="wallet_entry",
        target_id=ledger.id,
        reason="privacy test record",
        result="success",
        created_at=created_at,
    )
    mfa = AuthMfa(
        user_id=user_id,
        secret_ciphertext="PRIVATE_MFA_CIPHERTEXT",
        pending_secret_ciphertext="PRIVATE_PENDING_MFA_CIPHERTEXT",
    )
    quarantine_id = uuid.uuid4()
    quarantined = EventQuarantine(
        id=quarantine_id,
        event_id=uuid.uuid4(),
        session_id=session_id,
        sequence=2,
        reason="invalid_payload",
        body='{"transcript":"PRIVATE_QUARANTINE_TRANSCRIPT"}',
        fingerprint=user_id.hex * 2,
        received_at=created_at,
    )
    db.add(user)
    await db.flush()
    db.add(agent)
    await db.flush()
    db.add_all([wallet, session, mfa, deployment])
    await db.flush()
    db.add(participant)
    await db.flush()
    db.add(capability)
    await db.flush()
    db.add_all([ledger, transcript, transcript_state, audit, quarantined])
    await db.flush()
    return {
        "user_id": user_id,
        "session_id": session_id,
        "event_id": event_id,
        "ledger_id": ledger.id,
        "audit_id": audit.id,
        "quarantine_id": quarantine_id,
        "public_id": public_id,
        "capability_digest": capability_digest,
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_user_export_redacts_credentials_and_transcripts_but_keeps_ledger(
    initialized_pg: FreshPostgres, tmp_path: Path
):
    export_path = tmp_path / "user-export.json"
    full_export_path = tmp_path / "user-export-with-transcripts.json"
    async with initialized_pg.new_session() as db:
        async with db.begin():
            fixture = await _seed_privacy_fixture(db)
            summary = await export_user_data(
                db,
                user_id=fixture["user_id"],
                output_path=export_path,
                reason="user requested account export",
            )
            full_summary = await export_user_data(
                db,
                user_id=fixture["user_id"],
                output_path=full_export_path,
                reason="approved transcript export ticket",
                include_transcripts=True,
            )

    payload = json.loads(export_path.read_text())
    serialized = export_path.read_text()
    full_payload = json.loads(full_export_path.read_text())
    full_serialized = full_export_path.read_text()
    assert summary.user_id == fixture["user_id"]
    assert full_summary.include_transcripts is True
    assert payload["records"]["wallet_entries"][0]["amount_minor"] == 100
    assert payload["records"]["public_deployments"][0]["public_id"] == fixture["public_id"]
    assert summary.record_counts["public_deployments"] == 1
    assert "public_guest_capabilities" not in payload["records"]
    assert "capability_digest" not in serialized
    assert fixture["capability_digest"] not in serialized
    assert payload["records"]["transcript_segments"][0]["final_text"] == "[REDACTED]"
    assert payload["records"]["event_quarantine"][0]["body"] == "[REDACTED]"
    assert payload["records"]["auth_mfa"][0]["secret_ciphertext"] == "[REDACTED]"
    assert payload["audit"][0]["action"] == "test.privacy.seed"
    assert "PRIVATE_TRANSCRIPT_TEXT" not in serialized
    assert "PRIVATE_QUARANTINE_TRANSCRIPT" not in serialized
    assert "PRIVATE_MFA_CIPHERTEXT" not in serialized
    assert "PRIVATE_PENDING_MFA_CIPHERTEXT" not in serialized
    assert "PRIVATE_API_KEY" not in serialized
    assert "not-exportable-password-hash" not in serialized
    assert full_payload["records"]["transcript_segments"][0]["final_text"] == (
        "PRIVATE_TRANSCRIPT_TEXT password=[REDACTED]"
    )
    assert "PRIVATE_PASSWORD" not in full_serialized
    assert export_path.stat().st_mode & 0o777 == 0o600
    assert full_export_path.stat().st_mode & 0o777 == 0o600

    async with initialized_pg.new_session() as db:
        ledger_count = await db.scalar(select(func.count()).select_from(WalletEntry))
        audit_count = await db.scalar(select(func.count()).select_from(AuditLog))
        assert ledger_count == 1
        assert audit_count == 3


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("limits", [{"max_bytes": 32}, {"max_records": 1}])
async def test_export_limits_leave_no_partial_artifact_or_audit(
    initialized_pg: FreshPostgres, tmp_path: Path, limits: dict[str, int]
):
    export_path = tmp_path / "bounded-export.json"
    async with initialized_pg.new_session() as db:
        async with db.begin():
            fixture = await _seed_privacy_fixture(db)

        async with db.begin():
            with pytest.raises(ValidationError):
                await export_user_data(
                    db,
                    user_id=fixture["user_id"],
                    output_path=export_path,
                    reason="test bounded export policy",
                    **limits,
                )
            assert await db.scalar(select(func.count()).select_from(AuditLog)) == 1
    assert not export_path.exists()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_transcript_deletion_redacts_content_and_preserves_ledger_and_audit(
    initialized_pg: FreshPostgres,
):
    async with initialized_pg.new_session() as db:
        async with db.begin():
            fixture = await _seed_privacy_fixture(db)

        async with db.begin():
            result = await redact_user_transcripts(
                db,
                user_id=fixture["user_id"],
                reason="user requested transcript deletion",
            )
        assert result["transcript_segments"] == 1

        transcript = await db.get(TranscriptSegment, fixture["event_id"])
        assert transcript.final_text == "[REDACTED]"
        quarantine = await db.get(EventQuarantine, fixture["quarantine_id"])
        assert quarantine.body == "[REDACTED]"
        state = await db.get(TranscriptState, fixture["session_id"])
        assert state.revision == 2
        assert await db.scalar(select(func.count()).select_from(WalletEntry)) == 1
        assert await db.scalar(select(func.count()).select_from(AuditLog)) == 2
        assert await db.get(AuditLog, fixture["audit_id"]) is not None
        audit_actions = set((await db.execute(select(AuditLog.action))).scalars())
        assert "ops.user.transcripts_redacted" in audit_actions


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_retention_is_cutoff_scoped_bounded_and_audited(
    initialized_pg: FreshPostgres,
):
    old = datetime.now(timezone.utc) - timedelta(days=5)
    recent = datetime.now(timezone.utc)
    async with initialized_pg.new_session() as db:
        async with db.begin():
            old_fixture = await _seed_privacy_fixture(db, created_at=old)
            recent_fixture = await _seed_privacy_fixture(db, created_at=recent)

        async with db.begin():
            result = await apply_retention(
                db,
                before=datetime.now(timezone.utc) - timedelta(days=1),
                reason="configured transcript retention cutoff",
                batch_size=1,
            )

        assert result["transcript_segments"] == 1
        assert result["event_quarantine_bodies"] == 1
        old_segment = await db.get(TranscriptSegment, old_fixture["event_id"])
        recent_segment = await db.get(TranscriptSegment, recent_fixture["event_id"])
        assert old_segment.final_text == "[REDACTED]"
        assert recent_segment.final_text == "PRIVATE_TRANSCRIPT_TEXT password=PRIVATE_PASSWORD"
        old_quarantine = await db.get(EventQuarantine, old_fixture["quarantine_id"])
        recent_quarantine = await db.get(EventQuarantine, recent_fixture["quarantine_id"])
        assert old_quarantine.body == "[REDACTED]"
        assert "PRIVATE_QUARANTINE_TRANSCRIPT" in recent_quarantine.body
        assert await db.scalar(select(func.count()).select_from(WalletEntry)) == 2
        audit_rows = (await db.execute(select(AuditLog))).scalars().all()
        assert len(audit_rows) == 3
        assert {row.action for row in audit_rows} >= {"ops.retention.applied"}


def test_retention_credential_must_be_distinct_same_postgres_database():
    database_url = "postgresql+asyncpg://api:api-pass@127.0.0.1:56884/voice_ai"
    maintenance_url = "postgresql+asyncpg://maintenance:maint-pass@127.0.0.1:56884/voice_ai"
    assert validate_retention_database_url(database_url, maintenance_url) == maintenance_url

    with pytest.raises(ValueError):
        validate_retention_database_url(database_url, database_url)

    with pytest.raises(ValueError):
        validate_retention_database_url(
            database_url,
            "postgresql+asyncpg://maintenance:maint-pass@127.0.0.1:56884/other_db",
        )
