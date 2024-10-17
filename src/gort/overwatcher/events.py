#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-04-09
# @Filename: events.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from typing import TYPE_CHECKING

from gort.enums import Event
from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.pubsub import GortMessage, GortSubscriber
from gort.tools import insert_to_database


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


class MonitorEvents(OverwatcherModuleTask["EventsOverwatcher"]):
    """Processes the notification queue."""

    name = "monitor_events"
    keep_alive = True
    restart_on_error = True

    _running_tasks: list[asyncio.Task] = []

    async def task(self):
        """Runs the task."""

        async for message in GortSubscriber().iterator(decode=True):
            # Clean done tasks.
            self._running_tasks = [t for t in self._running_tasks if not t.done()]

            task = asyncio.create_task(self.process(message))
            self._running_tasks.append(task)

    async def process(self, message: GortMessage):
        """Processes a notification"""

        message_type = message.message_type

        if message_type != "event":
            return

        event = Event(message.event or Event.UNCATEGORISED)
        event_name = event.name
        payload = message.payload

        try:
            self.write_to_db(event, payload)
        except Exception as ee:
            self.log.error(f"Failed to write event {event_name} to the database: {ee}")

        if event == Event.OBSERVER_NEW_TILE:
            tile_id = payload.get("tile_id", None)
            dither_position = payload.get("dither_position", 0)
            if tile_id is not None:
                await self.overwatcher.notify(
                    f"Observing tile {tile_id} on dither "
                    f"position #{dither_position}."
                )

    def write_to_db(self, event: Event, payload: dict):
        """Writes the event to the database."""

        dt = datetime.now(tz=UTC)

        insert_to_database(
            self.gort.config["services.database.tables.events"],
            [{"date": dt, "event": event.name.upper(), "payload": json.dumps(payload)}],
        )


class EventsOverwatcher(OverwatcherModule):
    name = "events"

    tasks = [MonitorEvents()]

    def __init__(self, overwatcher: Overwatcher):
        super().__init__(overwatcher)

        self.queue = asyncio.Queue()
