#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-04-09
# @Filename: notifications.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import Any

from gort.maskbits import Event, Notification
from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.overwatcher.overwatcher import Overwatcher


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

    tasks = [ProcessNotification()]

    def __init__(self, overwatcher: Overwatcher):
        super().__init__(overwatcher)

        self.queue = asyncio.Queue()
