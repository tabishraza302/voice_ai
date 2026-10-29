"""AMQP adapters for durable event publication and manual consumption."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
)
from aio_pika.exceptions import DeliveryError, PublishError


DEFAULT_EXCHANGE = "voice.session.events"
DEFAULT_HISTORY_QUEUE = "voice.session.events.history"
DEFAULT_RETRY_QUEUE = "voice.session.events.retry"
DEFAULT_QUARANTINE_QUEUE = "voice.session.events.quarantine"


class AmqpError(RuntimeError):
    """Base class for AMQP adapter failures."""


class AmqpConnectionError(AmqpError):
    """The broker connection could not be established or was lost."""


class PublishNotConfirmed(AmqpError):
    """The broker did not confirm a persistent publish."""


class UnroutableMessage(AmqpError):
    """A mandatory publish was returned because no queue matched."""


@dataclass(frozen=True)
class AmqpConfig:
    url: str
    exchange: str = DEFAULT_EXCHANGE
    history_queue: str = DEFAULT_HISTORY_QUEUE
    retry_queue: str = DEFAULT_RETRY_QUEUE
    quarantine_queue: str = DEFAULT_QUARANTINE_QUEUE
    timeout: float = 3.0

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        history_queue: str = DEFAULT_HISTORY_QUEUE,
        retry_queue: str = DEFAULT_RETRY_QUEUE,
        quarantine_queue: str = DEFAULT_QUARANTINE_QUEUE,
        timeout: float = 3.0,
    ) -> "AmqpConfig":
        if not url:
            raise AmqpConnectionError("AMQP URL is required")
        return cls(
            url=url,
            exchange=exchange,
            history_queue=history_queue,
            retry_queue=retry_queue,
            quarantine_queue=quarantine_queue,
            timeout=timeout,
        )


@dataclass
class AmqpDelivery:
    body: bytes
    delivery_tag: int
    redelivered: bool
    exchange: str
    routing_key: str
    _message: AbstractIncomingMessage
    _settled: bool = False

    async def ack(self) -> None:
        if self._settled:
            return
        self._settled = True
        await self._message.ack()

    async def nack(self, requeue: bool = True) -> None:
        if self._settled:
            return
        self._settled = True
        await self._message.nack(requeue=requeue)


def _delivery(message: AbstractIncomingMessage) -> AmqpDelivery:
    if message.delivery_tag is None:
        raise AmqpError("broker delivery has no delivery tag")
    return AmqpDelivery(
        body=message.body,
        delivery_tag=message.delivery_tag,
        redelivered=message.redelivered,
        exchange=message.exchange,
        routing_key=message.routing_key,
        _message=message,
    )


async def _connect(config: AmqpConfig) -> AbstractConnection:
    try:
        return await aio_pika.connect(config.url, timeout=config.timeout)
    except Exception as exc:
        raise AmqpConnectionError("cannot connect to AMQP broker") from exc


async def _declare_topology(
    channel: AbstractChannel,
    config: AmqpConfig,
) -> None:
    exchange = await channel.declare_exchange(
        config.exchange,
        aio_pika.ExchangeType.TOPIC,
        durable=True,
        timeout=config.timeout,
    )
    history = await channel.declare_queue(
        config.history_queue,
        durable=True,
        timeout=config.timeout,
    )
    await history.bind(exchange, routing_key="#", timeout=config.timeout)
    await channel.declare_queue(
        config.retry_queue,
        durable=True,
        timeout=config.timeout,
    )
    await channel.declare_queue(
        config.quarantine_queue,
        durable=True,
        timeout=config.timeout,
    )


class AmqpPublisher:
    """Publish persistent messages with mandatory routing and confirms."""

    def __init__(self, config: AmqpConfig, *, ensure_exchange: bool = True) -> None:
        self.config = config
        self.ensure_exchange = ensure_exchange
        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None

    async def connect(self) -> None:
        if self._connection is not None:
            return

        connection = await _connect(self.config)
        try:
            channel = await connection.channel(
                publisher_confirms=True,
                on_return_raises=True,
            )
            if self.ensure_exchange:
                exchange = await channel.declare_exchange(
                    self.config.exchange,
                    aio_pika.ExchangeType.TOPIC,
                    durable=True,
                    timeout=self.config.timeout,
                )
            else:
                exchange = await channel.get_exchange(
                    self.config.exchange,
                    ensure=False,
                )
        except Exception:
            await connection.close()
            raise

        self._connection = connection
        self._channel = channel
        self._exchange = exchange

    async def publish(
        self,
        body: bytes,
        *,
        routing_key: str,
        exchange: str | None = None,
        mandatory: bool = True,
    ) -> bool:
        await self.connect()
        assert self._channel is not None

        exchange_name = self.config.exchange if exchange is None else exchange
        if not exchange_name:
            target = self._channel.default_exchange
        elif exchange_name == self.config.exchange and self._exchange is not None:
            target = self._exchange
        else:
            target = await self._channel.get_exchange(
                exchange_name,
                ensure=False,
            )

        message = aio_pika.Message(
            body=body,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        try:
            confirmation = await target.publish(
                message,
                routing_key=routing_key,
                mandatory=mandatory,
                timeout=self.config.timeout,
            )
        except PublishError as exc:
            await self.close()
            raise UnroutableMessage(
                "mandatory publish was returned by the broker"
            ) from exc
        except DeliveryError as exc:
            await self.close()
            raise PublishNotConfirmed(
                "broker negatively acknowledged publish"
            ) from exc
        except Exception:
            await self.close()
            raise

        if confirmation is None:
            await self.close()
            raise PublishNotConfirmed("broker returned no publisher confirmation")
        return True

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        self._channel = None
        self._exchange = None
        if connection is not None:
            await connection.close()

    async def __aenter__(self) -> "AmqpPublisher":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class AmqpConsumer:
    """Consume broker deliveries with manual acknowledgements."""

    def __init__(self, config: AmqpConfig) -> None:
        self.config = config
        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None

    async def connect(self, *, declare_topology: bool = True) -> None:
        if self._connection is not None:
            return

        connection = await _connect(self.config)
        try:
            channel = await connection.channel(publisher_confirms=False)
            if declare_topology:
                await _declare_topology(channel, self.config)
        except Exception:
            await connection.close()
            raise

        self._connection = connection
        self._channel = channel

    async def _get_queue(self, queue: str) -> AbstractQueue:
        await self.connect(declare_topology=False)
        assert self._channel is not None
        return await self._channel.get_queue(queue, ensure=True)

    async def queue_depth(self, queue: str) -> int:
        broker_queue = await self._get_queue(queue)
        result = broker_queue.declaration_result
        if result is None:
            raise AmqpError("passive queue declaration returned no result")
        return result.message_count

    async def purge(self, queue: str) -> int:
        broker_queue = await self._get_queue(queue)
        result = await broker_queue.purge(timeout=self.config.timeout)
        return result.message_count

    async def delete_queue(self, queue: str) -> int:
        broker_queue = await self._get_queue(queue)
        result = await broker_queue.delete(
            if_unused=False,
            if_empty=False,
            timeout=self.config.timeout,
        )
        return result.message_count

    async def get(self, queue: str) -> AmqpDelivery | None:
        broker_queue = await self._get_queue(queue)
        message = await broker_queue.get(
            fail=False,
            timeout=self.config.timeout,
        )
        return None if message is None else _delivery(message)

    async def consume(
        self,
        queue: str,
        handler: Callable[[AmqpDelivery], Awaitable[None]],
        *,
        prefetch: int = 1,
        max_messages: int | None = None,
    ) -> int:
        if max_messages is not None and max_messages <= 0:
            return 0

        await self.connect(declare_topology=False)
        assert self._channel is not None
        channel = self._channel
        await channel.set_qos(
            prefetch_count=prefetch,
            timeout=self.config.timeout,
        )
        broker_queue = await channel.get_queue(queue, ensure=True)

        handled = 0
        async with broker_queue.iterator(no_ack=False) as deliveries:
            async for message in deliveries:
                await handler(_delivery(message))
                handled += 1
                if max_messages is not None and handled >= max_messages:
                    break

        if channel.is_closed:
            raise AmqpConnectionError("AMQP consumer channel closed")
        return handled

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        self._channel = None
        if connection is not None:
            await connection.close()

    async def __aenter__(self) -> "AmqpConsumer":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


async def declare_topology(config: AmqpConfig) -> None:
    """Create the durable exchange and queues used by event ingestion."""
    connection = await _connect(config)
    try:
        channel = await connection.channel(publisher_confirms=False)
        await _declare_topology(channel, config)
    finally:
        await connection.close()


__all__ = [
    "AmqpConfig",
    "AmqpConnectionError",
    "AmqpConsumer",
    "AmqpDelivery",
    "AmqpError",
    "AmqpPublisher",
    "PublishNotConfirmed",
    "UnroutableMessage",
    "DEFAULT_EXCHANGE",
    "DEFAULT_HISTORY_QUEUE",
    "DEFAULT_QUARANTINE_QUEUE",
    "DEFAULT_RETRY_QUEUE",
    "declare_topology",
]
