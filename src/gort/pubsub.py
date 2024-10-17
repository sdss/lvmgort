#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-08-21
# @Filename: pubsub.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import json
import time
import uuid
import warnings

from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    ClassVar,
    Literal,
    Self,
    overload,
)

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from aio_pika.exceptions import AMQPConnectionError
from pydantic import BaseModel, Field

from gort import config
from gort.enums import Event


if TYPE_CHECKING:
    from aio_pika.abc import (
        AbstractChannel,
        AbstractConnection,
        AbstractExchange,
        AbstractQueue,
        ConsumerTag,
    )


SubCallbackType = Callable[["GortMessage"], Awaitable[None]]
MessageType = Literal["event", "notification", "custom"]


class GortPublishedMessageModel(BaseModel):
    """A model for messages published to the exchange."""

    message_type: MessageType
    event_name: int | str | None
    payload: dict[str, Any] = {}
    timestamp: float = Field(default_factory=time.time)


class GortEventModel(GortPublishedMessageModel):
    """A model for event messages."""

    event_name: str
    message_type: Literal["event"] = "event"


class GortMessage:
    """A model for messages to be published to the exchange."""

    def __init__(self, message: AbstractIncomingMessage):
        self.message = message

        self.body: dict[str, Any] = json.loads(message.body)
        self.payload: dict[str, Any] = self.body.get("payload", {})

        self.message_type: MessageType = self.body.get("message_type", "custom")

        self.event: Event | None = None
        self.event_name: str | None = None

        if self.message_type == "event":
            self.event = Event(self.body["event_name"].upper())
            self.event_name = self.event.name.upper()


def callback_wrapper(func: SubCallbackType):
    """Wraps a callback to receive a ``GortMessage`` instance."""

    async def wrapper(message: AbstractIncomingMessage):
        async with message.process():
            await func(GortMessage(message))

    return wrapper


class BasePubSub:
    """A base class to connect to a RabbitMQ GORT exchange.

    Parameters
    ----------
    connection_string
        The connection string to the RabbitMQ server.
    exchange_name
        The name of the exchange where the messages will be sent.

    """

    def __init__(
        self,
        connection_string: str | None = None,
        exchange_name: str | None = None,
    ):
        psc = config["services.pubsub"]

        self.connection_string = connection_string or psc["connection_string"]
        self.exchange_name = exchange_name or psc["exchange_name"]

        self.connection: AbstractConnection | None = None
        self.channel: AbstractChannel | None = None
        self.exchange: AbstractExchange | None = None

    async def connect(self) -> Self:
        """Connects to the RabbitMQ server and declares the exchange."""

        self.connection = await aio_pika.connect_robust(self.connection_string)

        self.channel = await self.connection.channel()
        await self.channel.set_qos(prefetch_count=1)

        self.exchange = await self.channel.declare_exchange(
            self.exchange_name,
            auto_delete=True,
            type=aio_pika.ExchangeType.FANOUT,
        )

        return self

    async def disconnect(self):
        """Disconnects from the RabbitMQ server."""

        if self.channel and not self.channel.is_closed:
            await self.channel.close()

        if self.connection and not self.connection.is_closed:
            await self.connection.close()

    async def __aenter__(self):
        if (
            not self.connection
            or self.connection.is_closed
            or not self.channel
            or self.channel.is_closed
        ):
            await self.connect()

        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.disconnect()


class GortPublisher(BasePubSub):
    """A class to publish messages to a RabbitMQ GORT exchange. A singleton."""

    _instance: ClassVar[GortPublisher]

    def __new__(cls, *args, **kwargs):
        if not hasattr(cls, "_instance"):
            cls._instance = super(GortPublisher, cls).__new__(cls)
        return cls._instance

    def __init__(
        self,
        connection_string: str | None = None,
        exchange_name: str | None = None,
    ):
        if not hasattr(self, "connection"):
            super().__init__(connection_string, exchange_name)

    async def publish(self, message: dict, routing_key: str | None = None):
        """Publishes a message to the exchange.

        Parameters
        ----------
        message
            The message to publish. Must be a dictionary that will be encoded
            as a JSON string.
        routing_key
            The routing key to use. If not provided, uses the default routing
            key defined in the configuration.

        """

        # Give the event loop a chance to run. This should only matter if the
        # library is being used in IPython, which does run the loop unless there
        # is a command running.
        try:
            await asyncio.sleep(0.1)
        except Exception:
            pass
        finally:
            if not self.channel or not self.exchange or self.channel.is_closed:
                await self.connect()

        assert self.exchange, "exchange not defined."

        for _ in range(3):
            try:
                await self.exchange.publish(
                    aio_pika.Message(body=json.dumps(message).encode()),
                    routing_key=routing_key or config["services.pubsub.routing_key"],
                )
                return
            except AMQPConnectionError:
                await self.connect()
            except Exception as err:
                from gort.exceptions import GortWarning

                warnings.warn(f"Unexpected error in GortPublisher: {err}", GortWarning)
                break


class GortSubscriber(BasePubSub):
    """A class to subscribe to messages from a RabbitMQ GORT exchange."""

    def __init__(
        self,
        connection_string: str | None = None,
        exchange_name: str | None = None,
        callback: SubCallbackType | None = None,
        queue_name: str | None = None,
    ):
        super().__init__(
            connection_string=connection_string,
            exchange_name=exchange_name,
        )

        self.queue_name: str | None = queue_name
        self.queue: AbstractQueue | None = None
        self.callback = callback

        self.consumer_tag: ConsumerTag | None = None

    async def connect(self, queue_name: str | None = None) -> Self:
        """Connects to the exchange, declares a queue, and binds the callback.

        Parameters
        ----------
        queue_name
            The name of the queue to declare. If not provided, a random name
            will be generated (recommended).

        """

        await super().connect()

        assert self.channel, "channel not defined."
        assert self.exchange, "exchange not defined."

        self.queue_name = (
            queue_name
            or self.queue_name
            or f"{self.exchange_name}-{str(uuid.uuid4()).split('-')[-1]}"
        )

        self.queue = await self.channel.declare_queue(
            self.queue_name,
            auto_delete=True,
            exclusive=True,
        )

        await self.queue.bind(self.exchange, routing_key="data")

        if self.callback:
            await self.queue.consume(callback_wrapper(self.callback))

        return self

    async def disconnect(self):
        """Disconnects from the RabbitMQ server."""

        if self.callback and self.queue and self.consumer_tag:
            await self.queue.cancel(self.consumer_tag)

            if self.exchange:
                await self.queue.unbind(self.exchange)

        await super().disconnect()

    @overload
    async def iterator(
        self,
        decode: Literal[True] = True,
    ) -> AsyncGenerator[GortMessage, None]: ...

    @overload
    async def iterator(
        self,
        decode: Literal[False],
    ) -> AsyncGenerator[AbstractIncomingMessage, None]: ...

    async def iterator(
        self,
        decode: bool = True,
    ) -> AsyncGenerator[AbstractIncomingMessage | GortMessage, None]:
        """Iterates over a queue and yields messages."""

        async with self as instance:
            assert instance.queue, "queue not defined."

            async with instance.queue.iterator() as queue_iter:
                async for message in queue_iter:
                    async with message.process():
                        if decode:
                            yield GortMessage(message)
                        else:
                            yield message


async def notify_event(event: Event, payload: dict[str, Any] = {}):
    """Notifies an event to the exchange."""

    message = GortEventModel(event_name=event.name, payload=payload).model_dump()
    await GortPublisher().publish(message)
