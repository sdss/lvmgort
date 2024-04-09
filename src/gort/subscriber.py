#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-04-09
# @Filename: subscriber.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import json

from typing import Any

from gort import config
from gort.maskbits import Notification
from gort.overwatcher.commands import OverwatcherCommand
from gort.tools import redis_client


__all__ = ["publish", "notify", "command", "Subscriber"]


async def publish(channel: str, message: dict[str, Any]) -> None:
    """Publishes a message to a channel.

    Parameters
    ----------
    channel
        The channel to publish the message to.
    message
        The message to publish. Must be a dictionary that can be serialised
        using JSON.

    """

    client = redis_client()
    await client.publish(channel, json.dumps(message))


async def notify(
    notification: Notification,
    payload: dict[str, Any] = {},
    channel: str | None = None,
) -> None:
    """Notifies a subscriber of an event.

    Parameters
    ----------
    notification
        The notification type.
    payload
        The payload to send to the subscriber.
    channel
        The channel to which to publish the notification. Default to
        ``redis.pubsub.notifications``.

    """

    await publish(
        channel or config["redis.pubsub.notifications"],
        {"notification": notification.value, **payload},
    )


async def command(
    command: OverwatcherCommand,
    payload: dict[str, Any] = {},
    channel: str | None = None,
) -> None:
    """Commands the overwatcher.

    Parameters
    ----------
    notification
        The notification type.
    payload
        The payload to send to the subscriber.
    channel
        The channel to which to publish the notification. Default to
        ``redis.pubsub.commands``.

    """

    await publish(
        channel or config["redis.pubsub.commands"],
        {"command": command.value, **payload},
    )


class Subscriber:
    """A class that subscribes to one or multiple pubsub channels."""

    def __init__(self, channels: str | list[str]) -> None:
        self.channels = [channels] if isinstance(channels, str) else list(channels)

        self.client = redis_client()
        self.pubsub = self.client.pubsub()

        self.subscribed: bool = False

    async def subscribe(self):
        """Subscribes to the channels."""

        await self.pubsub.subscribe(*self.channels)
        self.subscribed = True

    async def listen(self):
        """Listens for messages and asynchronously yields them."""

        async for message in self.pubsub.listen():
            yield message
