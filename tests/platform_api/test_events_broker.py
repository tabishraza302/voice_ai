"""End-to-end integration tests using disposable RabbitMQ broker and PostgreSQL (Task 8).

Follows Task 8 specification:
- Real RabbitMQ broker container running rabbitmq:4.1.3-alpine with memory limit 256m.
- Publisher confirms with mandatory routing.
- End-to-end spool -> RabbitMQ -> HistoryConsumer -> PostgreSQL.
- Broker redelivery deduplication (crash after commit, before ACK).
- Conflicting payload transferred to durable quarantine queue with confirm before source ACK.
- Broker outage recovery: spool retains events across restart, resumes publishing on recovery.
- Broker tests cleanly skip when absent and FAIL when VERIFY_REQUIRED=1.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import uuid
import aio_pika

import pytest
from sqlalchemy import select

from bot.runtime.outbox import EventSpool, PublishNotConfirmed
from platform_api.events.amqp import (
    AmqpConfig,
    AmqpConsumer,
    AmqpPublisher,
    declare_topology,
    UnroutableMessage,
)
from platform_api.events.consumer import (
    ConsumerResult,
    HistoryConsumer,
    QueueTransfer,
)
from platform_api.events.models import (
    EventQuarantine,
    ProcessedEvent,
    TranscriptSegment,
)
from platform_api.events.repository import HistoryRepository
from platform_api.events.schemas import build_event
from tests.support.broker import RabbitMqBroker
from tests.support.postgres import FreshPostgres


def _segment_envelope(
    session_id: uuid.UUID,
    sequence: int,
    text_content: str = "hello",
    event_id: uuid.UUID | None = None,
):
    return build_event(
        session_id=session_id,
        sequence=sequence,
        event_type="transcript.segment",
        payload={
            "speaker": "user",
            "final_text": text_content,
            "start_ms": 0,
            "end_ms": 100,
        },
        event_id=event_id,
    )


def _isolated_config(broker: RabbitMqBroker) -> AmqpConfig:
    run_id = uuid.uuid4().hex
    prefix = f"voice-ai-test-{run_id}"
    return AmqpConfig(
        url=broker.url,
        exchange=f"{prefix}.events",
        history_queue=f"{prefix}.history",
        retry_queue=f"{prefix}.retry",
        quarantine_queue=f"{prefix}.quarantine",
    )


async def _delete_test_topology(config: AmqpConfig) -> None:
    connection = await aio_pika.connect(config.url, timeout=config.timeout)
    try:
        channel = await connection.channel(publisher_confirms=False)
        for queue_name in (
            config.history_queue,
            config.retry_queue,
            config.quarantine_queue,
        ):
            queue = await channel.declare_queue(
                queue_name,
                durable=True,
                timeout=config.timeout,
            )
            await queue.delete(
                if_unused=False,
                if_empty=False,
                timeout=config.timeout,
            )
        exchange = await channel.declare_exchange(
            config.exchange,
            aio_pika.ExchangeType.TOPIC,
            durable=True,
            timeout=config.timeout,
        )
        await exchange.delete(if_unused=False, timeout=config.timeout)
    finally:
        await connection.close()


@pytest.fixture
async def event_amqp_config(rabbitmq_broker: RabbitMqBroker):
    """Use and clean a unique exchange and queues without touching defaults."""
    config = _isolated_config(rabbitmq_broker)
    try:
        yield config
    finally:
        await _delete_test_topology(config)


@pytest.mark.broker
@pytest.mark.asyncio
async def test_broker_topology_and_confirmed_publish(
    event_amqp_config: AmqpConfig,
) -> None:
    """Topology declaration and publisher confirms against real RabbitMQ instance."""
    config = event_amqp_config
    await declare_topology(config)

    publisher = AmqpPublisher(config)
    await publisher.connect()
    try:
        session_id = uuid.uuid4()
        envelope = _segment_envelope(session_id, 1, "test message")
        routing_key = f"session.{session_id}"

        # Mandatory publish with confirmed routing
        confirmed = await publisher.publish(
            envelope.to_json_bytes(),
            routing_key=routing_key,
            mandatory=True,
        )
        assert confirmed is True
    finally:
        await publisher.close()


@pytest.mark.broker
@pytest.mark.asyncio
async def test_mandatory_publish_rejects_unroutable_message(
    event_amqp_config: AmqpConfig,
) -> None:
    config = event_amqp_config
    await declare_topology(config)
    publisher = AmqpPublisher(config)
    try:
        with pytest.raises(UnroutableMessage):
            await publisher.publish(
                b"must not be lost",
                exchange="",
                routing_key=f"unbound-{uuid.uuid4()}",
                mandatory=True,
            )
    finally:
        await publisher.close()


@pytest.mark.broker
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_spool_to_broker_to_consumer_end_to_end(
    event_amqp_config: AmqpConfig,
    initialized_pg: FreshPostgres,
    tmp_path,
) -> None:
    """Spool -> publisher confirms -> RabbitMQ -> HistoryConsumer -> PostgreSQL commit -> ACK."""
    config = event_amqp_config
    await declare_topology(config)

    session_id = uuid.uuid4()
    spool_path = tmp_path / "journal.jsonl"

    # 1. Producer writes events into durable spool
    async with EventSpool(spool_path) as spool:
        rec1 = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload={"speaker": "user", "final_text": "spooled text", "start_ms": 0, "end_ms": 50},
        )
        assert await spool.pending_count() == 1

        # 2. Spool publishes to RabbitMQ via AmqpPublisher
        publisher = AmqpPublisher(config)
        await publisher.connect()
        try:
            pub_res = await spool.publish_pending(publisher=publisher)
            assert pub_res.published == 1
            assert pub_res.remaining == 0
            assert await spool.pending_count() == 0
        finally:
            await publisher.close()

    # The broker-confirmed message must outlive the producer's local copy.
    spool_path.unlink()

    # 3. HistoryConsumer consumes from RabbitMQ history queue
    transfer_publisher = AmqpPublisher(config)
    await transfer_publisher.connect()
    quarantine_xfer = QueueTransfer(
        transfer_publisher, queue=config.quarantine_queue
    )

    consumer = HistoryConsumer(
        session_factory=initialized_pg.sessions,
        quarantine_transfer=quarantine_xfer,
        repository=HistoryRepository(),
    )

    amqp_consumer = AmqpConsumer(config)
    await amqp_consumer.connect()
    try:
        delivery = await asyncio.wait_for(
            amqp_consumer.get(config.history_queue), timeout=10.0
        )
        assert delivery is not None

        result = await consumer.handle(
            delivery.body,
            ack=delivery.ack,
        )
        assert result.status == "inserted"
        assert result.event_id == rec1.event_id
    finally:
        await amqp_consumer.close()
        await transfer_publisher.close()

    # 4. Verify PostgreSQL has committed projection
    async with initialized_pg.sessions() as session:
        stored = await session.get(ProcessedEvent, rec1.event_id)
        assert stored is not None
        assert stored.session_id == session_id
        segment = await session.scalar(
            select(TranscriptSegment).where(TranscriptSegment.event_id == rec1.event_id)
        )
        assert segment is not None
        assert segment.final_text == "spooled text"


@pytest.mark.broker
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_consumer_redelivery_deduplication_after_unacked_commit(
    event_amqp_config: AmqpConfig,
    initialized_pg: FreshPostgres,
) -> None:
    """A broker redelivery after commit but before ACK is a duplicate no-op."""
    config = event_amqp_config
    await declare_topology(config)

    session_id = uuid.uuid4()
    envelope = _segment_envelope(session_id, 1, "dedupe text")
    publisher = AmqpPublisher(config)
    confirmed = await publisher.publish(
        envelope.to_json_bytes(),
        routing_key=f"session.{session_id}",
        mandatory=True,
    )
    assert confirmed is True
    await publisher.close()

    transfer_publisher = AmqpPublisher(config)
    quarantine_xfer = QueueTransfer(
        transfer_publisher, queue=config.quarantine_queue
    )
    consumer = HistoryConsumer(
        session_factory=initialized_pg.sessions,
        quarantine_transfer=quarantine_xfer,
    )

    first_consumer = AmqpConsumer(config)
    await first_consumer.connect()
    try:
        first_delivery = await asyncio.wait_for(
            first_consumer.get(config.history_queue), timeout=10.0
        )
        assert first_delivery is not None

        class SimulatedAckFailure(RuntimeError):
            pass

        async def failing_ack() -> None:
            raise SimulatedAckFailure("connection lost before ACK")

        first_result = await consumer.handle(
            first_delivery.body,
            ack=failing_ack,
        )
        assert first_result.status == "ack_failed"
    finally:
        # Closing the channel returns the unacked delivery to the broker.
        await first_consumer.close()

    async with initialized_pg.sessions() as session:
        stored = await session.get(ProcessedEvent, envelope.event_id)
        assert stored is not None

    replay_consumer = AmqpConsumer(config)
    await replay_consumer.connect()
    try:
        redelivery = await asyncio.wait_for(
            replay_consumer.get(config.history_queue), timeout=10.0
        )
        assert redelivery is not None
        assert redelivery.redelivered is True
        assert redelivery.body == envelope.to_json_bytes()

        result = await consumer.handle(redelivery.body, ack=redelivery.ack)
        assert result.status == "duplicate"
    finally:
        await replay_consumer.close()
        await transfer_publisher.close()

    async with initialized_pg.sessions() as session:
        segments = (
            await session.scalars(
                select(TranscriptSegment).where(
                    TranscriptSegment.event_id == envelope.event_id
                )
            )
        ).all()
        assert len(segments) == 1


@pytest.mark.broker
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_conflicting_payload_transfers_to_quarantine_queue(
    event_amqp_config: AmqpConfig,
    initialized_pg: FreshPostgres,
) -> None:
    """Same identity with different payload_hash is quarantined to durable AMQP queue before source ACK."""
    config = event_amqp_config
    await declare_topology(config)

    session_id = uuid.uuid4()
    event_id = uuid.uuid4()

    # 1. Ingest original valid event
    original = _segment_envelope(session_id, 1, "original truth", event_id=event_id)
    repo = HistoryRepository()
    async with initialized_pg.sessions() as session:
        async with session.begin():
            r1 = await repo.consume_event(session, original.to_json())
            assert r1 == "inserted"

    # 2. Wire conflicting event with same event_id but different payload
    conflict = _segment_envelope(session_id, 1, "conflicting forgery", event_id=event_id)

    transfer_publisher = AmqpPublisher(config)
    await transfer_publisher.connect()
    quarantine_xfer = QueueTransfer(
        transfer_publisher, queue=config.quarantine_queue
    )
    consumer = HistoryConsumer(
        session_factory=initialized_pg.sessions,
        quarantine_transfer=quarantine_xfer,
        repository=repo,
    )

    source_acked = False

    async def ack_source():
        nonlocal source_acked
        source_acked = True


    # Handle the conflicting message
    res = await consumer.handle(
        conflict.to_json_bytes(),
        ack=ack_source,
    )
    assert res.status == "quarantined"
    assert source_acked is True

    # 3. Verify quarantine AMQP queue received the conflicting message
    q_consumer = AmqpConsumer(config)
    await q_consumer.connect()
    try:
        q_delivery = await asyncio.wait_for(
            q_consumer.get(config.quarantine_queue), timeout=5.0
        )
        assert q_delivery is not None
        await q_delivery.ack()
        q_data = json.loads(q_delivery.body.decode("utf-8"))
        assert q_data["event_id"] == str(event_id)
        assert q_data["payload"]["final_text"] == "conflicting forgery"
    finally:
        await q_consumer.close()
        await transfer_publisher.close()

    # 4. Verify PostgreSQL original record is intact and quarantine record exists
    async with initialized_pg.sessions() as session:
        segment = await session.scalar(
            select(TranscriptSegment).where(TranscriptSegment.event_id == event_id)
        )
        assert segment is not None
        assert segment.final_text == "original truth"

        q_row = await session.scalar(
            select(EventQuarantine).where(EventQuarantine.event_id == event_id)
        )
        assert q_row is not None
        assert q_row.reason == "EVENT_ID_CONFLICT"


@pytest.mark.broker
@pytest.mark.asyncio
async def test_broker_outage_retains_spool_and_recovers(
    rabbitmq_broker: RabbitMqBroker,
    event_amqp_config: AmqpConfig,
    tmp_path,
) -> None:
    """When RabbitMQ stops, publisher fails, spool retains events across restart, and recovers upon restore."""

    if not rabbitmq_broker.can_restart:
        pytest.skip("broker outage injection requires a test-owned container")
    config = event_amqp_config
    await declare_topology(config)

    session_id = uuid.uuid4()
    spool_path = tmp_path / "journal.jsonl"

    # Write event to spool
    async with EventSpool(spool_path) as spool:
        rec = await spool.append_event(
            session_id=session_id,
            event_type="transcript.segment",
            payload={"speaker": "user", "final_text": "survives outage", "start_ms": 0, "end_ms": 100},
        )
        assert await spool.pending_count() == 1

    # Temporarily stop broker
    rabbitmq_broker.stop()
    try:
        publisher = AmqpPublisher(config)
        # Attempt to publish while broker is down fails
        async with EventSpool(spool_path) as spool:
            pub_res = await spool.publish_pending(publisher=publisher)
            assert pub_res.published == 0
            assert pub_res.blocked is True
            assert await spool.pending_count() == 1
    finally:
        # Restore broker
        rabbitmq_broker.start()

    # Re-declare topology after broker restart
    await declare_topology(config)

    # Publisher reconnects and resumes publishing; spool removes confirmed event
    publisher2 = AmqpPublisher(config)
    await publisher2.connect()
    try:
        async with EventSpool(spool_path) as reopened:
            assert await reopened.pending_count() == 1
            result = await reopened.publish_pending(publisher=publisher2)
            assert result.published == 1
            assert result.remaining == 0
            assert await reopened.pending_count() == 0
    finally:
        await publisher2.close()
