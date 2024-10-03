#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: overwatcher.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import dataclasses
import pathlib

from typing import cast

from sdsstools import Configuration
from sdsstools.utils import GatheringTaskGroup

from gort.core import LogNamespace
from gort.exceptions import GortError
from gort.gort import Gort
from gort.overwatcher.core import OverwatcherBaseTask, OverwatcherModule
from gort.overwatcher.notifier import NotifierMixIn


@dataclasses.dataclass
class OverwatcherState:
    """Dataclass with the overwatcher state values."""

    running: bool = False
    enabled: bool = False
    observing: bool = False
    calibrating: bool = False
    night: bool = False
    safe: bool = False
    allow_dome_calibrations: bool = True
    dry_run: bool = False


class OverwatcherTask(OverwatcherBaseTask):
    """Overwatcher task that is aware of the overwatcher instance."""

    def __init__(self, overwatcher: Overwatcher):
        super().__init__()

        self.overwatcher = overwatcher
        self.log = self.overwatcher.log


class OverwatcherMainTask(OverwatcherTask):
    """The main overwatcher task."""

    name = "overwatcher_task"
    keep_alive = True
    restart_on_error = True

    async def task(self):
        """Main overwatcher task."""

        ow = self.overwatcher

        while True:
            await asyncio.sleep(1)

            is_safe = ow.weather.is_safe()
            is_night = ow.ephemeris.is_night()

            ow.state.night = is_night
            ow.state.safe = is_safe
            ow.state.observing = ow.observer.status.observing()
            ow.state.calibrating = ow.calibrations.get_running_calibration() is not None


class OverwatcherPingTask(OverwatcherTask):
    """Emits a ping notification every five minutes."""

    name = "overwatcher_ping"
    keep_alive = True
    restart_on_error = True

    delay: float = 300

    async def task(self):
        """Ping task."""

        while True:
            await asyncio.sleep(self.delay)
            await self.overwatcher.notify("I am alive!", log=False)


class Overwatcher(NotifierMixIn):
    """Monitors the observatory."""

    instance: Overwatcher | None = None

    def __new__(cls, *args, **kwargs):
        if not cls.instance:
            cls.instance = super(Overwatcher, cls).__new__(cls)
        return cls.instance

    def __init__(
        self,
        gort: Gort | None = None,
        verbosity: str = "debug",
        calibrations_file: str | pathlib.Path | None = None,
        dry_run: bool = False,
        **kwargs,
    ):
        from gort.overwatcher import (
            CalibrationsOverwatcher,
            EphemerisOverwatcher,
            EventsOverwatcher,
            ObserverOverwatcher,
            WeatherOverwatcher,
        )

        # Check if the instance already exists, in which case do nothing.
        if hasattr(self, "gort"):
            return

        self.gort = gort or Gort(verbosity=verbosity, **kwargs)
        self.config = cast(Configuration, self.gort.config)
        self.log = LogNamespace(self.gort.log, header=f"({self.__class__.__name__}) ")

        self.state = OverwatcherState()
        self.state.dry_run = dry_run

        self.tasks: list[OverwatcherTask] = [
            OverwatcherMainTask(self),
            OverwatcherPingTask(self),
        ]

        self.ephemeris = EphemerisOverwatcher(self)
        self.calibrations = CalibrationsOverwatcher(self, calibrations_file)
        self.observer = ObserverOverwatcher(self)
        self.weather = WeatherOverwatcher(self)
        self.events = EventsOverwatcher(self)

    async def run(self):
        """Starts the overwatcher tasks."""

        if self.state.running:
            raise GortError("Overwatcher is already running.")

        if not self.gort.is_connected():
            await self.gort.init()

        async with GatheringTaskGroup() as group:
            for module in OverwatcherModule.instances:
                self.log.info(f"Starting overwatcher module {module.name!r}")
                group.create_task(module.run())

        async with GatheringTaskGroup() as group:
            for task in self.tasks:
                group.create_task(task.run())

        self.state.running = True
        await self.notify(
            "Overwatcher is starting.",
            payload={"dry-run": self.state.dry_run},
        )

        if self.state.dry_run:
            self.log.warning("Overatcher is running in dry-mode.")

        return self

    async def emergency_shutdown(self, block: bool = True, reason: str = "undefined"):
        """Shuts down the observatory in case of an emergency."""

        # Check if the dome is already closed, then do nothing.
        dome_open = await self.gort.enclosure.is_open()
        if not dome_open:
            return

        if not reason.endswith("."):
            reason += "."

        await self.notify(
            f"Triggering emergency shutdown. Reason: {reason}",
            level="warning",
        )

        if not self.state.dry_run:
            stop = asyncio.create_task(self.observer.stop_observing(immediate=True))
            shutdown = asyncio.create_task(self.gort.shutdown())
        else:
            self.log.warning("Dry run enabled. Not shutting down.")

        if block:
            await asyncio.gather(stop, shutdown)

    async def cancel(self):
        """Cancels the overwatcher tasks."""

        for module in OverwatcherModule.instances:
            self.gort.log.debug(f"Cancelling overwatcher module {module.name!r}")
            await module.cancel()

        for task in self.tasks:
            await task.cancel()

        self.state.running = False
