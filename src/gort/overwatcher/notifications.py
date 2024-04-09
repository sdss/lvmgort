#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-04-09
# @Filename: notifications.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import json

from typing import Any

from gort.maskbits import Event, Notification
from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.overwatcher.overwatcher import Overwatcher
from gort.pubsub import Subscriber


class NotificationsMonitor(OverwatcherModuleTask["NotificationsOverwatcher"]):

    name = "notifications_monitor"
    keep_alive = True
    restart_on_error = True

    async def task(self):
        """Runs the task."""

        subscriber = Subscriber(self.config["services.redis.pubsub.notifications"])

        async for message in subscriber.listen():
            if message["type"] != "message":
                continue

            payload = json.loads(message["data"])

            try:
                notification_code: int | None = payload.pop("notification", None)
                notification_type: str = payload.pop("type", None)
                if notification_type == "event":
                    notification = Event(notification_code)
                else:
                    notification = Notification(notification_code)
            except ValueError:
                self.log.error(f"Invalid notification code {notification_code!r}.")
                continue

            self.module.queue.put_nowait((notification, payload))


class ProcessNotification(OverwatcherModuleTask["NotificationsOverwatcher"]):
    """Processes the notification queue."""

    name = "process_notification"
    keep_alive = True
    restart_on_error = True

    async def task(self):
        """Runs the task."""

        while True:
            notification, payload = await self.module.queue.get()

            asyncio.create_task(self.process(notification, payload))

    async def process(
        self,
        notification: Notification,
        payload: dict[str, Any] = {},
    ):
        """Processes a notification"""

        self.log_notification(notification, payload)

    def log_notification(
        self,
        notification: Notification,
        payload: dict[str, Any] = {},
    ):
        """Logs a notification."""

        type_ = "event" if isinstance(notification, Event) else "notification"
        name = notification.name

        self.log.debug(f"Received {type_} {name!r} with payload {payload!r}.")


class NotificationsOverwatcher(OverwatcherModule):
    name = "notifications"

    tasks = [NotificationsMonitor(), ProcessNotification()]

    def __init__(self, overwatcher: Overwatcher):
        super().__init__(overwatcher)

        self.queue = asyncio.Queue()
