"""History consumer: validate, project, commit, then acknowledge.

The ACK boundary is the whole point of this module:

* a valid event is ACKed only after ``processed_events`` and its projection
  commit;
* a conflicting or invalid event is copied to a declared durable quarantine
  queue with a confirmed publish *before* the source delivery is ACKed;
* if the quarantine transfer cannot be confirmed, the source stays unacknowledged
  and the transfer is retried with backoff;
* a database outage leaves the delivery unacknowledged; there is no in-memory
  fallback and no wallet mutation on this path.

The consumer never imports a wallet model and :class:`HistoryRepository`
installs a flush guard that rejects wallet table mutations on its session.
"""

from __future__ import annotations

import argparse
import asyncio
from asyncio import sleep as _sleep
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Awaitable, Callable, Literal, Protocol
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.engine import make_url

from platform_api.config import (
    PlatformSettings,
    get_settings,
    validate_event_consumer_database_url,
)
from platform_api.db.session import create_engine, get_session_factory
from platform_api.events.permissions import verify_event_consumer_role
from platform_api.events.amqp import (
    AmqpConfig,
    AmqpConsumer,
    AmqpPublisher,
    PublishNotConfirmed,
    declare_topology,
)
from platform_api.events.repository import HistoryRepository
from platform_api.events.schemas import (
    EventEnvelope,
    EventValidationError,
    parse_event_body,
)


logger = logging.getLogger(__name__)


class DestinationTransfer(Protocol):
    async def transfer(self, body: bytes, *, reason: str) -> None: ...


@dataclass(frozen=True)
class QueueTransfer:
    """Copy a body to a durable queue and wait for mandatory routing/confirm."""

    publisher: AmqpPublisher
    queue: str

    async def transfer(self, body: bytes, *, reason: str) -> None:
        # The original bytes are preserved exactly.  The reason is retained in
        # PostgreSQL quarantine/audit state, not wrapped around the body.
        confirmation = await self.publisher.publish(
            body,
            exchange="",
            routing_key=self.queue,
            mandatory=True,
        )
        if not confirmation:
            raise PublishNotConfirmed(
                "quarantine destination publish was not confirmed"
            )


ConsumerStatus = Literal[
    "inserted",
    "duplicate",
    "quarantined",
    "ack_failed",
]


@dataclass(frozen=True)
class ConsumerResult:
    status: ConsumerStatus
    event_id: uuid.UUID | None = None
    reason: str | None = None


AckCallable = Callable[[], Awaitable[None]]


class HistoryConsumer:
    """Consume one delivery at a time; commit before ACK."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        quarantine_transfer: DestinationTransfer,
        retry_transfer: DestinationTransfer | None = None,
        repository: HistoryRepository | None = None,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self.session_factory = session_factory
        self.quarantine_transfer = quarantine_transfer
        self.retry_transfer = retry_transfer
        self.repository = repository or HistoryRepository()
        self.retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    async def handle(
        self,
        body: bytes | str,
        *,
        ack: AckCallable,
    ) -> ConsumerResult:
        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        try:
            envelope = parse_event_body(raw)
        except EventValidationError as exc:
            return await self._quarantine_invalid(raw, str(exc), ack=ack)

        retry_attempt = 0
        while True:
            try:
                async with self.session_factory() as session:
                    async with session.begin():
                        result = await self.repository.ingest(
                            session,
                            envelope,
                            received_at=datetime.now(timezone.utc),
                        )
                break
            except Exception as exc:
                delay = self._retry_delay(retry_attempt)
                retry_attempt += 1
                logger.warning(
                    "history projection unavailable; retaining delivery and "
                    "retrying in %.1fs: %s",
                    delay,
                    exc,
                )
                await _sleep(delay)

        if result.status == "quarantine":
            await self._transfer_until_confirmed(
                self.quarantine_transfer,
                raw,
                reason=result.reason or "CONFLICT",
                destination_name="quarantine",
            )
            try:
                await ack()
            except Exception as exc:
                logger.warning("ACK failed after quarantine transfer: %s", exc)
                return ConsumerResult("ack_failed", envelope.event_id, str(exc))
            return ConsumerResult(
                "quarantined", envelope.event_id, result.reason
            )

        # inserted or duplicate: the transaction committed above.
        try:
            await ack()
        except Exception as exc:
            # The commit is durable; the broker will redeliver and the event-id
            # dedupe turns the replay into a no-op.  Never roll back or ACK
            # twice.
            logger.warning("ACK failed after durable commit: %s", exc)
            return ConsumerResult("ack_failed", envelope.event_id, str(exc))
        return ConsumerResult(result.status, envelope.event_id, result.reason)

    def _retry_delay(self, attempt: int) -> float:
        base = max(self.retry_backoff_seconds, 0.1)
        return min(base * (2 ** min(attempt, 5)), 30.0)

    async def _transfer_until_confirmed(
        self,
        destination: DestinationTransfer,
        body: bytes,
        *,
        reason: str,
        destination_name: str,
    ) -> None:
        attempt = 0
        while True:
            try:
                await destination.transfer(body, reason=reason)
                return
            except Exception as exc:
                delay = self._retry_delay(attempt)
                attempt += 1
                logger.warning(
                    "%s transfer unconfirmed; retaining source and retrying in "
                    "%.1fs: %s",
                    destination_name,
                    delay,
                    exc,
                )
                await _sleep(delay)

    async def _quarantine_invalid(
        self,
        body: bytes,
        reason: str,
        *,
        ack: AckCallable,
    ) -> ConsumerResult:
        # Transfer first: a durable queue copy is the source of truth for an
        # unparseable event.  The PostgreSQL quarantine row is best-effort
        # observability, so a DB outage cannot lose the only copy.
        await self._transfer_until_confirmed(
            self.quarantine_transfer,
            body,
            reason="INVALID_EVENT",
            destination_name="invalid-event quarantine",
        )

        try:
            async with self.session_factory() as session:
                async with session.begin():
                    await self.repository.record_invalid_body(
                        session, body, "INVALID_EVENT"
                    )
        except Exception as exc:
            logger.warning("could not record invalid-event quarantine row: %s", exc)

        try:
            await ack()
        except Exception as exc:
            logger.warning("ACK failed after invalid-event quarantine: %s", exc)
            return ConsumerResult("ack_failed", None, str(exc))
        return ConsumerResult("quarantined", None, reason[:256])

    async def transfer_to_retry(
        self, body: bytes, *, reason: str
    ) -> None:
        if self.retry_transfer is None:
            raise RuntimeError("retry transfer destination is not configured")
        await self.retry_transfer.transfer(body, reason=reason)



class RetryWorker:
    """Move a durable retry-queue message back to history after its delay.

    The message stays unacknowledged while the delay is observed.  A crash
    before the confirmed republish leaves it on the retry queue, so the retry
    queue is never a TTL/dead-letter hop that owns the only copy.
    """

    def __init__(
        self,
        *,
        amqp_consumer: AmqpConsumer,
        history_publisher: AmqpPublisher,
        retry_queue: str,
        history_exchange: str,
        delay_seconds: float,
        failure_backoff_seconds: float = 1.0,
    ) -> None:
        self.amqp_consumer = amqp_consumer
        self.history_publisher = history_publisher
        self.retry_queue = retry_queue
        self.history_exchange = history_exchange
        self.delay_seconds = max(0.0, delay_seconds)
        self.failure_backoff_seconds = max(0.0, failure_backoff_seconds)

    async def run_once(self) -> bool:
        delivery = await self.amqp_consumer.get(self.retry_queue)
        if delivery is None:
            return False
        if self.delay_seconds:
            await _sleep(self.delay_seconds)
        try:
            envelope = parse_event_body(delivery.body)
        except EventValidationError:
            # Keep the unparseable retry body on the retry queue; an operator
            # must decide where it belongs.  Do not invent an identity.
            await delivery.nack(requeue=True)
            return False

        attempt = 0
        while True:
            try:
                confirmation = await self.history_publisher.publish(
                    delivery.body,
                    exchange=self.history_exchange,
                    routing_key=f"session.{envelope.session_id}",
                    mandatory=True,
                )
                if not confirmation:
                    raise PublishNotConfirmed(
                        "history destination publish was not confirmed"
                    )
                break
            except Exception as exc:
                delay = min(
                    max(self.failure_backoff_seconds, 0.1)
                    * (2 ** min(attempt, 5)),
                    30.0,
                )
                attempt += 1
                logger.warning(
                    "retry destination publish unconfirmed; retaining delivery "
                    "and retrying in %.1fs: %s",
                    delay,
                    exc,
                )
                await _sleep(delay)

        await delivery.ack()
        return True


async def run_consumer(
    settings: PlatformSettings,
    *,
    once: bool = False,
    limit: int | None = None,
    queue: str | None = None,
) -> int:
    if not settings.amqp_url:
        raise RuntimeError("PLATFORM_AMQP_URL is required for the history consumer")
    try:
        consumer_url = validate_event_consumer_database_url(
            settings.database_url, settings.event_consumer_database_url
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    expected_username = make_url(consumer_url).username
    if not expected_username:
        raise RuntimeError(
            "PLATFORM_EVENT_CONSUMER_DATABASE_URL must name a restricted database role."
        )
    config = AmqpConfig.from_url(
        settings.amqp_url,
        exchange=settings.event_exchange,
        history_queue=settings.event_history_queue,
        retry_queue=settings.event_retry_queue,
        quarantine_queue=settings.event_quarantine_queue,
    )
    engine = create_engine(settings, database_url=consumer_url)
    platform_engine = create_engine(settings)
    publisher = None
    amqp_consumer = None
    retry_task: asyncio.Task[None] | None = None
    try:
        await verify_event_consumer_role(
            engine,
            platform_engine=platform_engine,
            expected_username=expected_username,
            expected_database_url=settings.database_url,
        )
        await declare_topology(config)
        session_factory = get_session_factory(engine)
        publisher = AmqpPublisher(config)
        amqp_consumer = AmqpConsumer(config)
        consumer = HistoryConsumer(
            session_factory,
            quarantine_transfer=QueueTransfer(publisher, config.quarantine_queue),
            retry_transfer=QueueTransfer(publisher, config.retry_queue),
        )

        async def on_delivery(delivery) -> None:
            result = await consumer.handle(delivery.body, ack=delivery.ack)
            logger.info(
                "history delivery status=%s event_id=%s reason=%s",
                result.status,
                result.event_id,
                result.reason,
            )

        async def run_retry_queue() -> None:
            while True:
                retry_consumer = AmqpConsumer(config)
                retry_publisher = AmqpPublisher(config)
                try:
                    retry_worker = RetryWorker(
                        amqp_consumer=retry_consumer,
                        history_publisher=retry_publisher,
                        retry_queue=config.retry_queue,
                        history_exchange=config.exchange,
                        delay_seconds=settings.event_retry_delay_seconds,
                    )
                    while True:
                        if not await retry_worker.run_once():
                            await _sleep(1.0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "retry worker disconnected; reconnecting in 1s: %s", exc
                    )
                finally:
                    await retry_consumer.close()
                    await retry_publisher.close()
                await _sleep(1.0)

        max_messages = 1 if once else limit
        if not once and limit is None and queue is None:
            retry_task = asyncio.create_task(run_retry_queue())
        return await amqp_consumer.consume(
            queue or config.history_queue,
            on_delivery,
            prefetch=1,
            max_messages=max_messages,
        )
    finally:
        try:
            if retry_task is not None:
                retry_task.cancel()
                try:
                    await retry_task
                except asyncio.CancelledError:
                    pass
        finally:
            try:
                if amqp_consumer is not None:
                    await amqp_consumer.close()
            finally:
                try:
                    if publisher is not None:
                        await publisher.close()
                finally:
                    try:
                        await engine.dispose()
                    finally:
                        await platform_engine.dispose()

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Consume durable voice session events into PostgreSQL history"
    )
    parser.add_argument("--once", action="store_true", help="consume one delivery and exit")
    parser.add_argument("--limit", type=int, default=None, help="stop after N deliveries")
    parser.add_argument("--queue", default=None, help="override the history queue name")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        settings = get_settings()
        handled = asyncio.run(
            run_consumer(
                settings,
                once=args.once,
                limit=args.limit,
                queue=args.queue,
            )
        )
    except Exception:
        logger.exception("history consumer failed")
        return 1
    logger.info("history consumer stopped handled=%d", handled)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())


__all__ = [
    "ConsumerResult",
    "DestinationTransfer",
    "HistoryConsumer",
    "QueueTransfer",
    "RetryWorker",
    "run_consumer",
    "main",
]
