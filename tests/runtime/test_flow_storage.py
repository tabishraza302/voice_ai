from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from bot.flow.schemas import (
    FragmentQueuedOutputEvent,
    Reduction,
    SessionState,
    UserTurnAcceptedEvent,
)
from bot.flow.playback_receipts import (
    PlaybackBinding,
    PlaybackReceiptVerifier,
    PcmSampleRange,
    RenderedFrameRange,
)
from bot.runtime.flow_storage import (
    DurablePostgresFlowStore,
    FlowStoreConflict,
    FlowStoreUnavailable,
    PostgresVerifiedPlaybackUsageMeter,
)
from bot.runtime.session_task import RuntimeStopRecord
from platform_api.billing.schemas import RuntimeStopAttestation
from platform_api.billing.tariffs import InitialSessionTariff
from platform_api.db.schema import initialize_schema
from platform_api.db.session import create_engine, get_session_factory
from platform_api.models import Session, User
from platform_api.sessions.models import (
    CommittedPlaybackReceipt,
    FlowRuntimeEvent,
    RuntimeRegistration,
)


REPORT_HASH = "a" * 64
FLOW_HASH = "b" * 64
ARTIFACT_HASH = "c" * 64
TEST_TARIFF = InitialSessionTariff(
    rate_minor_per_minute=6000,
    billing_slice_ms=1000,
    permit_ttl_ms=5000,
)


@pytest_asyncio.fixture
async def flow_store(fresh_pg):
    engine = create_engine(fresh_pg.url, schema="platform")
    await initialize_schema(engine, initial_tariff=TEST_TARIFF)
    session_factory = get_session_factory(engine)
    registration_id = uuid4()
    session_id = uuid4()
    owner_id = uuid4()
    async with session_factory() as db:
        db.add(
            User(
                id=owner_id,
                email=f"{owner_id}@example.test",
                email_key=f"{owner_id}@example.test",
                display_name="Test owner",
                password_hash="test-only-password-hash",
                email_verified_at=datetime.now(timezone.utc),
            )
        )
        db.add(
            Session(
                id=session_id,
                owner_user_id=owner_id,
                kind="voice_test",
                state="ACTIVE",
                version=2,
                runner_id="runner-test",
            )
        )
        db.add(
            RuntimeRegistration(
                session_id=session_id,
                runner_id="runner-test",
                registration_id=registration_id,
                status="REGISTERED",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        await db.commit()
    store = DurablePostgresFlowStore(engine, schema="platform")
    try:
        yield store, session_factory, session_id, registration_id
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_readiness_rejects_schema_drift_without_migrating(flow_store):
    store, _, _, _ = flow_store
    await store.ensure_ready()
    async with store.engine.begin() as conn:
        await conn.execute(text("DROP TABLE platform.runtime_stop_records"))

    with pytest.raises(FlowStoreUnavailable):
        await store.ensure_ready()
    async with store.engine.connect() as conn:
        assert await conn.scalar(
            text("SELECT to_regclass('platform.runtime_stop_records')")
        ) is None


def _state(session_id, version: int = 1) -> SessionState:
    return SessionState(
        session_id=str(session_id),
        transport_epoch="epoch-1",
        version=version,
        flow_hash=FLOW_HASH,
        artifact_hash=ARTIFACT_HASH,
    )


def _event(session_id, event_id: str = "turn-1") -> UserTurnAcceptedEvent:
    return UserTurnAcceptedEvent(
        event_id=event_id,
        session_id=str(session_id),
        transport_epoch="epoch-1",
        turn_id="turn-1",
        generation_id="generation-1",
        text="hello",
    )


def _verifier(session_id, *, epoch: str = "epoch-1") -> PlaybackReceiptVerifier:
    verifier = PlaybackReceiptVerifier(
        session_id=str(session_id),
        transport_epoch=epoch,
        active_peer_id="peer-1",
        capability_report_hash=REPORT_HASH,
    )
    verifier.activate_generation("turn-1", "generation-1")
    return verifier


def _binding(session_id, fragment: str, *, epoch: str = "epoch-1") -> PlaybackBinding:
    return PlaybackBinding(
        session_id=str(session_id),
        transport_epoch=epoch,
        turn_id="turn-1",
        generation_id="generation-1",
        manifest_id="manifest-1",
        fragment_id=fragment,
        content_hash="d" * 64,
        capability_report_hash=REPORT_HASH,
    )


def _verified_receipt(
    verifier: PlaybackReceiptVerifier,
    binding: PlaybackBinding,
    sample_range: PcmSampleRange,
    *,
    evidence_id: str,
    rendered_frames: RenderedFrameRange | None = None,
):
    queue_event = FragmentQueuedOutputEvent(
        event_id=f"queue:{binding.fragment_id}",
        session_id=binding.session_id,
        transport_epoch=binding.transport_epoch,
        turn_id=binding.turn_id,
        generation_id=binding.generation_id,
        manifest_id=binding.manifest_id,
        fragment_id=binding.fragment_id,
    )
    verifier.register_queued_fragment(queue_event, binding, sample_range)
    assert verifier.verify_event(queue_event)
    assert verifier.confirm_queued_fragment(queue_event)
    if rendered_frames is None:
        frame_count = (sample_range.sample_end - sample_range.sample_start) * 2
        rendered_frames = RenderedFrameRange(
            frame_start=sample_range.sample_start * 2,
            frame_end=sample_range.sample_start * 2 + frame_count,
            sample_rate=sample_range.sample_rate * 2,
        )
    wire = {
        "type": "flow-playback-receipt",
        "version": 1,
        "binding": binding.to_message(),
        "media_samples": sample_range.to_message(),
        "rendered_frames": rendered_frames.to_message(),
        "evidence_id": evidence_id,
    }
    receipt = verifier.accept_render_receipt(wire, sender_peer_id="peer-1")
    assert receipt is not None
    assert verifier.verify_event(receipt.event)
    return verifier.verified_receipt_for_event(receipt.event)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_commits_event_and_restores_state_after_store_recreation(flow_store):
    store, _, session_id, _ = flow_store
    event = _event(session_id)
    reduction = Reduction(state=_state(session_id))

    await store.commit(event, reduction)
    await store.commit(event, reduction)

    restored = await store.load_state(str(session_id), FLOW_HASH)
    assert restored == reduction.state

    async with store.session_factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(FlowRuntimeEvent).where(
                FlowRuntimeEvent.session_id == session_id
            )
        )
    assert count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_receipt_commit_is_idempotent_and_survives_store_recreation(flow_store):
    store, _, session_id, _ = flow_store
    verifier = _verifier(session_id)
    meter = PostgresVerifiedPlaybackUsageMeter(store, str(session_id), verifier)
    binding = _binding(session_id, "fragment-1")
    receipt = _verified_receipt(
        verifier,
        binding,
        PcmSampleRange(0, 24000, 24000),
        evidence_id="evidence-1",
    )
    reduction = Reduction(state=_state(session_id))

    await store.commit(receipt.event, reduction, verified_receipts=(receipt,))
    assert verifier.confirm_event_committed(receipt.event)
    await store.commit(receipt.event, reduction, verified_receipts=(receipt,))

    assert await meter.cumulative_billable_ms() == 1000
    assert len(verifier.committed_receipts()) == 1
    async with store.session_factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(CommittedPlaybackReceipt).where(
                CommittedPlaybackReceipt.session_id == session_id
            )
        )
    assert count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_meter_floors_combined_sample_duration_not_each_fragment(flow_store):
    store, _, session_id, _ = flow_store
    verifier = _verifier(session_id)
    meter = PostgresVerifiedPlaybackUsageMeter(store, str(session_id), verifier)

    for index in range(24):
        binding = _binding(session_id, f"fragment-{index}")
        receipt = _verified_receipt(
            verifier,
            binding,
            PcmSampleRange(index, index + 1, 24000),
            evidence_id=f"evidence-{index}",
        )
        reduction = Reduction(state=_state(session_id, index + 1))
        await store.commit(receipt.event, reduction, verified_receipts=(receipt,))
        assert verifier.confirm_event_committed(receipt.event)

    assert await meter.cumulative_billable_ms() == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_overlapping_receipt_rolls_back_event_state_and_usage(flow_store):
    store, _, session_id, _ = flow_store
    verifier = _verifier(session_id)
    meter = PostgresVerifiedPlaybackUsageMeter(store, str(session_id), verifier)
    first = _verified_receipt(
        verifier,
        _binding(session_id, "fragment-1"),
        PcmSampleRange(0, 240, 24000),
        evidence_id="evidence-1",
    )
    await store.commit(first.event, Reduction(state=_state(session_id)), verified_receipts=(first,))
    assert verifier.confirm_event_committed(first.event)

    overlapping = _verified_receipt(
        verifier,
        _binding(session_id, "fragment-2"),
        PcmSampleRange(200, 440, 24000),
        evidence_id="evidence-2",
    )
    with pytest.raises(FlowStoreConflict):
        await store.commit(
            overlapping.event,
            Reduction(state=_state(session_id, 2)),
            verified_receipts=(overlapping,),
        )

    assert await store.load_state(str(session_id), FLOW_HASH) == _state(session_id)
    assert await meter.cumulative_billable_ms() == 10
    async with store.session_factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(FlowRuntimeEvent).where(
                FlowRuntimeEvent.session_id == session_id
            )
        )
    assert count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_cross_rate_render_overlap_commits_at_most_once(flow_store):
    store, _, session_id, _ = flow_store
    verifier = _verifier(session_id)
    meter = PostgresVerifiedPlaybackUsageMeter(store, str(session_id), verifier)
    first = _verified_receipt(
        verifier,
        _binding(session_id, "fragment-1"),
        PcmSampleRange(0, 24000, 24000),
        evidence_id="evidence-1",
        rendered_frames=RenderedFrameRange(0, 48000, 48000),
    )
    second = _verified_receipt(
        verifier,
        _binding(session_id, "fragment-2"),
        PcmSampleRange(24000, 48000, 24000),
        evidence_id="evidence-2",
        rendered_frames=RenderedFrameRange(0, 24000, 24000),
    )

    results = await asyncio.gather(
        store.commit(
            first.event,
            Reduction(state=_state(session_id)),
            verified_receipts=(first,),
        ),
        store.commit(
            second.event,
            Reduction(state=_state(session_id, 2)),
            verified_receipts=(second,),
        ),
        return_exceptions=True,
    )

    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, FlowStoreConflict) for result in results) == 1
    assert await meter.cumulative_billable_ms() == 1000
    async with store.session_factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(FlowRuntimeEvent).where(
                FlowRuntimeEvent.session_id == session_id
            )
        )
    assert count == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_store_rejects_receipt_without_exact_registered_verifier_object(flow_store):
    store, _, session_id, _ = flow_store
    verifier = _verifier(session_id)
    meter = PostgresVerifiedPlaybackUsageMeter(store, str(session_id), verifier)
    receipt = _verified_receipt(
        verifier,
        _binding(session_id, "fragment-1"),
        PcmSampleRange(0, 240, 24000),
        evidence_id="evidence-1",
    )
    fabricated = replace(receipt)
    with pytest.raises(FlowStoreConflict, match="verifier"):
        await store.commit(
            receipt.event,
            Reduction(state=_state(session_id)),
            verified_receipts=(fabricated,),
        )
    assert await meter.cumulative_billable_ms() == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stop_record_is_idempotent_exact_and_matches_committed_usage(flow_store):
    store, _, session_id, registration_id = flow_store
    attestation = RuntimeStopAttestation(
        source="voice-runtime",
        stop_id=uuid4(),
        session_id=session_id,
        runner_id="runner-test",
        registration_id=registration_id,
        session_version=2,
        stopped_at=datetime.now(timezone.utc),
        reason="platform_release",
    )
    record = RuntimeStopRecord(
        attestation=attestation,
        expected_budget_version=3,
        cumulative_billable_ms=0,
    )

    await store.save_stop_record(str(session_id), 2, record)
    await store.save_stop_record(str(session_id), 2, record)
    assert await store.load_stop_record(str(session_id), 2) == record

    conflicting = RuntimeStopRecord(
        attestation=attestation,
        expected_budget_version=4,
        cumulative_billable_ms=0,
    )
    with pytest.raises(FlowStoreConflict):
        await store.save_stop_record(str(session_id), 2, conflicting)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_stop_record_refuses_usage_not_matching_persisted_receipts(flow_store):
    store, _, session_id, registration_id = flow_store
    verifier = _verifier(session_id)
    PostgresVerifiedPlaybackUsageMeter(store, str(session_id), verifier)
    receipt = _verified_receipt(
        verifier,
        _binding(session_id, "fragment-1"),
        PcmSampleRange(0, 24000, 24000),
        evidence_id="evidence-1",
    )
    await store.commit(receipt.event, Reduction(state=_state(session_id)), verified_receipts=(receipt,))
    assert verifier.confirm_event_committed(receipt.event)

    attestation = RuntimeStopAttestation(
        source="voice-runtime",
        stop_id=uuid4(),
        session_id=session_id,
        runner_id="runner-test",
        registration_id=registration_id,
        session_version=2,
        stopped_at=datetime.now(timezone.utc),
        reason="platform_release",
    )
    inaccurate = RuntimeStopRecord(
        attestation=attestation,
        expected_budget_version=3,
        cumulative_billable_ms=999,
    )
    with pytest.raises(FlowStoreConflict, match="usage"):
        await store.save_stop_record(str(session_id), 2, inaccurate)

    accurate = RuntimeStopRecord(
        attestation=attestation,
        expected_budget_version=3,
        cumulative_billable_ms=1000,
    )
    await store.save_stop_record(str(session_id), 2, accurate)
    with pytest.raises(FlowStoreConflict):
        later = _verified_receipt(
            verifier,
            _binding(session_id, "fragment-2"),
            PcmSampleRange(24000, 48000, 24000),
            evidence_id="evidence-2",
        )
        await store.commit(
            later.event,
            Reduction(state=_state(session_id, 2)),
            verified_receipts=(later,),
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_meter_uses_separate_transport_epoch_sample_clocks(flow_store):
    store, _, session_id, _ = flow_store
    first_verifier = _verifier(session_id, epoch="epoch-1")
    first_meter = PostgresVerifiedPlaybackUsageMeter(store, str(session_id), first_verifier)
    first = _verified_receipt(
        first_verifier,
        _binding(session_id, "fragment-1", epoch="epoch-1"),
        PcmSampleRange(0, 24000, 24000),
        evidence_id="evidence-1",
    )
    await store.commit(first.event, Reduction(state=_state(session_id)), verified_receipts=(first,))
    assert first_verifier.confirm_event_committed(first.event)

    second_verifier = _verifier(session_id, epoch="epoch-2")
    second_meter = PostgresVerifiedPlaybackUsageMeter(store, str(session_id), second_verifier)
    second = _verified_receipt(
        second_verifier,
        _binding(session_id, "fragment-2", epoch="epoch-2"),
        PcmSampleRange(0, 12000, 24000),
        evidence_id="evidence-2",
    )
    await store.commit(second.event, Reduction(state=_state(session_id, 2)), verified_receipts=(second,))
    assert second_verifier.confirm_event_committed(second.event)

    assert await first_meter.cumulative_billable_ms() == 1500
    assert await second_meter.cumulative_billable_ms() == 1500
