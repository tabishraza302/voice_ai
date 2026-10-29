"""Tests for the runtime's durable event spool (Task 8)."""

from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import os
import uuid

import pytest

from bot.runtime.outbox import (
    DEFAULT_SPOOL_DIR,
    EventConflict,
    EventSpool,
    PublishNotConfirmed,
    SpoolCapacityError,
    SpoolCorruptionError,
    spool_path_for_runner,
)
from platform_api.events.schemas import EventValidationError, build_event
from tests.runtime.worker import append_worker


class RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[bytes, str]] = []

    async def publish(self, body, *, routing_key, exchange=None, mandatory=True):
        self.calls.append((body, routing_key))
        return True


class FailingPublisher:
    async def publish(self, body, *, routing_key, exchange=None, mandatory=True):
        raise PublishNotConfirmed("broker is down")


class NoConfirmationPublisher:
    async def publish(self, body, *, routing_key, exchange=None, mandatory=True):
        return False


def _segment(text: str = "hello") -> dict:
    return {
        "speaker": "user",
        "final_text": text,
        "start_ms": 0,
        "end_ms": 100,
    }


@pytest.mark.asyncio
async def test_sequence_allocation_and_retry_identity(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    event_id = uuid.uuid4()
    async with EventSpool(path) as spool:
        first = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("one"),
            event_id=event_id,
        )
        second = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("two"),
        )
        third = await spool.append_event(
            session_id=session_id,
            event_type="usage.provider",
            payload={
                "provider": "local",
                "model": "stt",
                "kind": "audio_ms",
                "quantity": 100,
                "unit": "ms",
            },
        )
        assert [first.sequence, second.sequence, third.sequence] == [1, 2, 3]
        assert first.event_id == event_id

        # Retrying the same identity must not allocate sequence 4 or rewrite
        # the body, even if the caller regenerated emitted_at.
        replay = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("one"),
            event_id=event_id,
        )
        assert replay.sequence == first.sequence
        assert replay.body == first.body
        assert await spool.pending_count() == 3

        with pytest.raises(EventConflict):
            await spool.append_event(
                session_id=session_id,
                event_type="transcript.segment",
                payload=_segment("different"),
                event_id=event_id,
            )
        assert await spool.pending_count() == 3

    async with EventSpool(path) as reopened:
        assert await reopened.pending_count() == 3
        fourth = await reopened.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("four"),
        )
        assert fourth.sequence == 4


@pytest.mark.asyncio
async def test_restart_replays_identical_body_and_identity(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    async with EventSpool(path) as spool:
        record = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("durable"),
        )

    publisher = RecordingPublisher()
    async with EventSpool(path) as reopened:
        result = await reopened.publish_pending(publisher=publisher)
        assert result.published == 1
        assert result.remaining == 0
        assert result.blocked is False
        assert len(publisher.calls) == 1
        body, routing_key = publisher.calls[0]
        assert body == record.body
        assert routing_key == f"session.{session_id}"
        assert await reopened.pending_count() == 0

    # A second restart has nothing to publish; the identity was not regenerated.
    async with EventSpool(path) as empty:
        again = await empty.publish_pending(publisher=publisher)
        assert again.published == 0
        assert again.remaining == 0
        assert len(publisher.calls) == 1
        duplicate = await empty.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("durable"),
            event_id=record.event_id,
        )
        assert duplicate.sequence == record.sequence
        assert duplicate.body == record.body
        assert await empty.pending_count() == 0

        after_ack = await empty.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("after acknowledgement"),
        )
        assert after_ack.sequence == 2


@pytest.mark.asyncio
async def test_append_prebuilt_rejects_mutated_payload_without_corrupting_journal(tmp_path):
    path = tmp_path / "journal.jsonl"
    envelope = build_event(
        session_id=uuid.uuid4(),
        sequence=1,
        event_type="transcript.segment",
        payload=_segment("validated before mutation"),
    )
    envelope.payload["final_text"] = "mutated after validation"

    async with EventSpool(path) as spool:
        with pytest.raises(EventValidationError):
            await spool.append_prebuilt(envelope)
        assert path.read_bytes() == b""
        assert await spool.pending_count() == 0

    async with EventSpool(path) as recovered:
        assert await recovered.pending_count() == 0


@pytest.mark.asyncio
async def test_acknowledged_bytes_are_reclaimed_without_losing_pending_order(
    tmp_path,
):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    payloads = [_segment(f"event-{index}-" + "x" * 512) for index in range(1, 4)]

    async with EventSpool(path, max_bytes=4_000, min_free_bytes=0) as spool:
        first = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=payloads[0],
        )
        second = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=payloads[1],
        )
        first_batch = await spool.publish_pending(
            limit=1,
            publisher=RecordingPublisher(),
        )
        assert first_batch.published == 1
        assert first_batch.remaining == 1

        third = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=payloads[2],
        )
        assert (first.sequence, second.sequence, third.sequence) == (1, 2, 3)
        assert await spool.pending_count() == 2

    with path.open("ab") as journal:
        journal.write(b'{"kind":"event","position":4')
        journal.flush()
        os.fsync(journal.fileno())

    async with EventSpool(path, max_bytes=4_000, min_free_bytes=0) as recovered:
        assert await recovered.pending_count() == 2
        publisher = RecordingPublisher()
        batch = await recovered.publish_pending(publisher=publisher)
        assert batch.published == 2
        assert batch.remaining == 0
        assert [body for body, _ in publisher.calls] == [second.body, third.body]


@pytest.mark.asyncio
async def test_failed_atomic_replace_keeps_original_journal_and_pending_events(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    payloads = [_segment(f"event-{index}-" + "x" * 512) for index in range(1, 4)]

    async with EventSpool(path, max_bytes=4_000, min_free_bytes=0) as spool:
        await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=payloads[0],
        )
        second = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=payloads[1],
        )
        await spool.publish_pending(limit=1, publisher=RecordingPublisher())
        original_journal = path.read_bytes()

        def fail_replace(_source, _destination):
            raise OSError("injected atomic replace failure")

        with monkeypatch.context() as patch:
            patch.setattr("bot.runtime.outbox.os.replace", fail_replace)
            with pytest.raises(OSError, match="atomic replace"):
                await spool.append_event(
                    session_id=session_id,
                    event_type="transcript.segment",
                    payload=payloads[2],
                )

        assert path.read_bytes() == original_journal
        assert await spool.pending_count() == 1
        third = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=payloads[2],
        )
        assert third.sequence == 3
        assert await spool.pending_count() == 2
        assert second.sequence == 2


@pytest.mark.asyncio
async def test_directory_fsync_failure_after_replace_recovers_compacted_events(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    payloads = [_segment(f"event-{index}-" + "x" * 512) for index in range(1, 4)]

    async with EventSpool(path, max_bytes=4_000, min_free_bytes=0) as spool:
        await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=payloads[0],
        )
        second = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=payloads[1],
        )
        await spool.publish_pending(limit=1, publisher=RecordingPublisher())

        def fail_directory_fsync(_directory):
            raise OSError("injected directory fsync failure")

        with monkeypatch.context() as patch:
            patch.setattr(
                EventSpool,
                "_fsync_directory",
                staticmethod(fail_directory_fsync),
            )
            with pytest.raises(OSError, match="directory fsync"):
                await spool.append_event(
                    session_id=session_id,
                    event_type="transcript.segment",
                    payload=payloads[2],
                )

        assert await spool.pending_count() == 1
        third = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=payloads[2],
        )
        assert third.sequence == 3

    async with EventSpool(path, max_bytes=4_000, min_free_bytes=0) as recovered:
        assert await recovered.pending_count() == 2
        publisher = RecordingPublisher()
        batch = await recovered.publish_pending(publisher=publisher)
        assert batch.published == 2
        assert batch.remaining == 0
        assert [body for body, _ in publisher.calls] == [second.body, third.body]


@pytest.mark.asyncio
async def test_publish_failure_keeps_row_for_same_identity_retry(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    async with EventSpool(path) as spool:
        record = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("retry me"),
        )
        failed = await spool.publish_pending(publisher=FailingPublisher())
        assert failed.published == 0
        assert failed.remaining == 1
        assert failed.blocked is True
        assert failed.retry_after_seconds > 0
        assert await spool.pending_count() == 1

    publisher = RecordingPublisher()
    async with EventSpool(path) as reopened:
        result = await reopened.publish_pending(publisher=publisher)
        assert result.published == 1
        body, _ = publisher.calls[0]
        assert body == record.body


@pytest.mark.asyncio
async def test_removal_requires_explicit_confirmation(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    async with EventSpool(path) as spool:
        await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("no confirm"),
        )
        result = await spool.publish_pending(publisher=NoConfirmationPublisher())
        assert result.published == 0
        assert result.remaining == 1
        assert await spool.pending_count() == 1


        class InspectingPublisher:
            async def publish(self, body, *, routing_key, exchange=None, mandatory=True):
                async with EventSpool(path) as probe:
                    assert await probe.pending_count() == 1
                return True

        confirmed = await spool.publish_pending(publisher=InspectingPublisher())
        assert confirmed.published == 1
        assert await spool.pending_count() == 0


@pytest.mark.asyncio
async def test_capacity_rejects_admission_without_allocating_sequence(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    async with EventSpool(path, max_events=1, min_free_bytes=0) as spool:
        first = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("one"),
        )
        with pytest.raises(SpoolCapacityError):
            await spool.append_event(
                session_id=session_id,
                event_type="transcript.segment",
                payload=_segment("two"),
            )
        assert await spool.pending_count() == 1
        assert (await spool.capacity()).saturated is True

        publisher = RecordingPublisher()
        await spool.publish_pending(publisher=publisher)
        third = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("three"),
        )
        # The rejected append did not consume sequence 2.
        assert first.sequence == 1
        assert third.sequence == 2


@pytest.mark.asyncio
async def test_byte_capacity_is_bounded(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    async with EventSpool(path, max_bytes=10, min_free_bytes=0) as spool:
        with pytest.raises(SpoolCapacityError):
            await spool.append_event(
                session_id=session_id,
                event_type="transcript.segment",
                payload=_segment("too large for ten bytes"),
            )
        assert await spool.pending_count() == 0


@pytest.mark.asyncio
async def test_corrupt_complete_record_fails_closed_on_recovery(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    async with EventSpool(path) as spool:
        await spool.append_event(
            session_id=session_id, event_type="transcript.segment",
            payload=_segment("original"),
        )
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b'"sha256":"', b'"sha256":"0', 1))
    with pytest.raises(SpoolCorruptionError, match="invalid complete journal record"):
        await EventSpool(path).open()


def test_sequence_allocation_is_atomic_across_processes(tmp_path):
    path = str(tmp_path / "journal.jsonl")
    session_id = str(uuid.uuid4())
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        futures = [
            pool.submit(append_worker, path, session_id, 5)
            for _ in range(4)
        ]
        results = [future.result(timeout=60) for future in futures]

    sequences = sorted(sequence for batch in results for sequence in batch)
    assert sequences == list(range(1, 21))

    async def inspect() -> int:
        async with EventSpool(path) as spool:
            return await spool.pending_count()

    assert asyncio.run(inspect()) == 20


@pytest.mark.asyncio
async def test_torn_final_record_is_discarded_and_sequence_recovers(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    async with EventSpool(path) as spool:
        first = await spool.append_event(
            session_id=session_id, event_type="transcript.segment",
            payload=_segment("first"),
        )
    with path.open("ab") as journal:
        journal.write(b'{"kind":"event","position":2')
        journal.flush()
        os.fsync(journal.fileno())
    async with EventSpool(path) as recovered:
        second = await recovered.append_event(
            session_id=session_id, event_type="transcript.segment",
            payload=_segment("second"),
        )
    assert first.sequence == 1
    assert second.sequence == 2


def test_spool_runner_path_contract(tmp_path):
    runner_id = "runner-voice-77"
    expected = DEFAULT_SPOOL_DIR / runner_id / "journal.jsonl"
    assert spool_path_for_runner(runner_id) == expected

    custom_base = tmp_path / "runtime" / "spool"
    custom_expected = custom_base / runner_id / "journal.jsonl"
    assert spool_path_for_runner(runner_id, base_dir=custom_base) == custom_expected

    spool = EventSpool(runner_id=runner_id)
    assert spool.path == expected
    assert spool.runner_id == runner_id


@pytest.mark.asyncio
async def test_admission_drain_threshold_triggers_before_saturation(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    # max_events=10, drain threshold at 2 events (20%)
    async with EventSpool(
        path,
        max_events=10,
        drain_threshold_events=2,
        min_free_bytes=0,
    ) as spool:
        cap0 = await spool.capacity()
        assert cap0.events == 0
        assert cap0.needs_drain is False
        assert cap0.saturated is False

        # Event 1: below drain threshold
        await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("one"),
        )
        cap1 = await spool.capacity()
        assert cap1.events == 1
        assert cap1.needs_drain is False
        assert cap1.saturated is False

        # Event 2: reaches drain threshold
        await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("two"),
        )
        cap2 = await spool.capacity()
        assert cap2.events == 2
        assert cap2.needs_drain is True
        assert cap2.saturated is False

        # Draining pending events clears the needs_drain flag
        publisher = RecordingPublisher()
        drain_result = await spool.drain(publisher=publisher)
        assert drain_result.published == 2
        cap_drained = await spool.capacity()
        assert cap_drained.events == 0
        assert cap_drained.needs_drain is False



@pytest.mark.asyncio
async def test_byte_drain_threshold_clears_after_confirmed_drain(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    async with EventSpool(
        path,
        max_bytes=1_000_000,
        drain_threshold_events=100,
        drain_threshold_bytes=1,
        min_free_bytes=0,
    ) as spool:
        await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("one"),
        )
        assert (await spool.capacity()).needs_drain is True

        await spool.drain(publisher=RecordingPublisher())
        assert (await spool.capacity()).needs_drain is False


@pytest.mark.asyncio
async def test_byte_admission_drain_reopens_after_confirmed_drain(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    body_size = len(
        build_event(
            session_id=session_id,
            sequence=1,
            event_type="transcript.segment",
            payload=_segment("one"),
        ).to_json_bytes()
    )
    async with EventSpool(
        path,
        max_bytes=1_000_000,
        drain_threshold_events=10,
        drain_threshold_bytes=body_size * 2,
        stop_admission_on_drain=True,
        min_free_bytes=0,
    ) as spool:
        first = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("one"),
        )
        with pytest.raises(SpoolCapacityError, match="admission-drain threshold reached"):
            await spool.append_event(
                session_id=session_id,
                event_type="transcript.segment",
                payload=_segment("two"),
            )

        await spool.drain(publisher=RecordingPublisher())
        second = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("two"),
        )
        assert [first.sequence, second.sequence] == [1, 2]

@pytest.mark.asyncio
async def test_stop_admission_on_drain_policy(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_id = uuid.uuid4()
    async with EventSpool(
        path,
        max_events=10,
        drain_threshold_events=2,
        stop_admission_on_drain=True,
        min_free_bytes=0,
    ) as spool:
        await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload=_segment("one"),
        )
        # Next append would reach drain_threshold_events (2), so admission is stopped
        with pytest.raises(SpoolCapacityError, match="admission-drain threshold reached"):
            await spool.append_event(
                session_id=session_id,
                event_type="transcript.segment",
                payload=_segment("two"),
            )


@pytest.mark.asyncio
async def test_sequence_is_session_wide_across_different_event_types(tmp_path):
    path = tmp_path / "journal.jsonl"
    session_a = uuid.uuid4()
    session_b = uuid.uuid4()

    async with EventSpool(path) as spool:
        # Session A: alternating different event types
        rec_a1 = await spool.append_event(
            session_id=session_a,
            event_type="session.lifecycle",
            payload={"state": "started"},
        )
        rec_a2 = await spool.append_event(
            session_id=session_a,
            event_type="transcript.segment",
            payload=_segment("hello"),
        )
        rec_a3 = await spool.append_event(
            session_id=session_a,
            event_type="usage.provider",
            payload={
                "provider": "local",
                "model": "stt",
                "kind": "audio_ms",
                "quantity": 100,
                "unit": "ms",
            },
        )
        rec_a4 = await spool.append_event(
            session_id=session_a,
            event_type="transcript.gap",
            payload={"after_sequence": 1, "before_sequence": 2, "reason": "producer_loss"},
        )
        rec_a5 = await spool.append_event(
            session_id=session_a,
            event_type="transcript.segment",
            payload=_segment("world"),
        )

        # Session B: independent sequence
        rec_b1 = await spool.append_event(
            session_id=session_b,
            event_type="transcript.segment",
            payload=_segment("b-first"),
        )

        # Session A sequence is contiguous 1..5 regardless of event types
        assert [
            rec_a1.sequence,
            rec_a2.sequence,
            rec_a3.sequence,
            rec_a4.sequence,
            rec_a5.sequence,
        ] == [1, 2, 3, 4, 5]
        assert rec_b1.sequence == 1
