#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: overwatcher.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import logging

from gort.exceptions import GortError
from gort.gort import Gort
from gort.overwatcher.core import OverwatcherModule
from gort.tools import cancel_task


class Overwatcher:
    """Monitors the observatory."""

    instance: Overwatcher | None = None

    def __new__(cls):
        if not cls.instance:
            cls.instance = super(Overwatcher, cls).__new__(cls)
        return cls.instance

    def __init__(self, gort: Gort | None = None):
        from gort.overwatcher import (
            CalibrationOverwatcher,
            EphemerisOverwatcher,
            ObserverOverwatcher,
            WeatherOverwatcher,
        )

        # Check if the instance already exists, in which case do nothing.
        if hasattr(self, "gort"):
            return

        self.gort = gort or Gort(verbosity="debug")

        self.tasks: list[asyncio.Task] = []

        self.calibration = CalibrationOverwatcher(self)
        self.ephemeris = EphemerisOverwatcher(self)
        self.observer = ObserverOverwatcher(self)
        self.weather = WeatherOverwatcher(self)

        self.is_running: bool = False

        self.allow_observations: bool = False

    async def run(self):
        """Starts the overwatcher tasks."""

        if self.is_running:
            raise GortError("Overwatcher is already running.")

        if not self.gort.is_connected():
            await self.gort.init()

        for module in OverwatcherModule.instances:
            self.log(f"Starting overwatcher module {module.name!r}")
            await module.run()

        self.tasks.append(asyncio.create_task(self.overwatcher_task()))

        self.is_running = True

        await asyncio.sleep(5)

        return self

    async def cancel(self):
        """Cancels the overwatcher tasks."""

        for module in OverwatcherModule.instances:
            self.gort.log.debug(f"Cancelling overwatcher module {module.name!r}")
            await module.cancel()

        for task in self.tasks:
            await cancel_task(task)

        self.tasks = []
        self.is_running = False

    async def overwatcher_task(self):
        """Main overwatcher task."""

        while True:
            await asyncio.sleep(5)

            if self.weather.can_open() and self.ephemeris.is_night():
                self.allow_observations = True
            else:
                self.allow_observations = False

    def log(self, message: str, level: str = "debug"):
        """Logs a message to the GORT log."""

        level = logging.getLevelName(level.upper())
        assert isinstance(level, int)

        message = f"({self.__class__.__name__}) {message}"

        self.gort.log.log(level, message)

    async def emergency_shutdown(self, block: bool = True):
        """Shuts down the observatory in case of an emergency."""

        stop_task = asyncio.create_task(self.observer.stop_observing(immediate=True))
        shutdown_task = asyncio.create_task(self.gort.shutdown())

        if block:
            await asyncio.gather(stop_task, shutdown_task)
        else:
            self.tasks.append(stop_task)
            self.tasks.append(shutdown_task)
