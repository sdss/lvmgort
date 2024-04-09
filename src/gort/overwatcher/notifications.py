#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-04-09
# @Filename: notifications.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.subscriber import Subscriber


class NotificationsMonitor(OverwatcherModuleTask["NotificationsOverwatcher"]):

    name = "notifications_monitor"
    keep_alive = True
    restart_on_error = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.subscriber = Subscriber(self.config["redis.pubsub.notifications"])

    async def task(self):
        """Runs the task."""

        async for message in self.subscriber.listen():
            self.log.debug(f"Received message: {message}")
            # Do something with the message.


class NotificationsOverwatcher(OverwatcherModule):
    name = "notifications"

    tasks = [NotificationsMonitor()]
