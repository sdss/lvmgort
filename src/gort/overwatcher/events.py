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

from gort.enums import ErrorCode, Event
from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.pubsub import GortMessage, GortSubscriber
from gort.tools import add_night_log_comment, insert_to_database


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

        elif event == Event.DOME_OPENING:
            await self.overwatcher.notify("The dome is opening ...")

        elif event == Event.DOME_OPEN:
            await self.overwatcher.notify("The dome is now open.")

        elif event == Event.DOME_CLOSING:
            await self.overwatcher.notify("The dome is closing ...")

        elif event == Event.DOME_CLOSED:
            await self.overwatcher.notify("The dome is now closed.")

        elif event == Event.EMERGENCY_SHUTDOWN:
            await self.overwatcher.notify("An emergency shutdown was triggered.")

        elif event == Event.ERROR:
            await self.handle_error_event(payload)

    def write_to_db(self, event: Event, payload: dict):
        """Writes the event to the database."""

        dt = datetime.now(tz=UTC)

        insert_to_database(
            self.gort.config["services.database.tables.events"],
            [{"date": dt, "event": event.name.upper(), "payload": json.dumps(payload)}],
        )

    async def handle_error_event(self, error_payload: dict):
        """Handles an error event."""

        error = error_payload.get("error", "unespecified error")

        code = error_payload.get("error_code", ErrorCode.UNCATEGORISED_ERROR)
        error_code = ErrorCode(code)

        error_message = f"Error {error_code.value} ({error_code.name}): {error}"
        if not error_message.endswith("."):
            error_message += "."

        is_observing = self.gort.observer.is_running()
        tile = self.gort.observer._tile
        if is_observing and tile and tile.tile_id:
            await self.notify(
                f"An error event was reported while observing tile {tile.tile_id}. "
                f"{error_message}"
            )
            await add_night_log_comment(
                f"Tile {tile.tile_id} - Error reported. {error_message} "
                "Data quality may have been affected.",
                category="overwatcher",
            )

        else:
            await self.notify(f"An error event was reported. {error_message}")


class EventsOverwatcher(OverwatcherModule):
    name = "events"

    tasks = [MonitorEvents()]

    def __init__(self, overwatcher: Overwatcher):
        super().__init__(overwatcher)

        self.queue = asyncio.Queue()
