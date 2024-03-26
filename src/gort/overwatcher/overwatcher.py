#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: overwatcher.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

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
        from gort.overwatcher import EphemerisOverwatcher, WeatherOverwatcher

        # Check if the instance already exists, in which case do nothing.
        if hasattr(self, "gort"):
            return

        self.gort = gort or Gort(verbosity="debug")

        self.tasks: dict[str, asyncio.Task] = {}

        self.ephemeris = EphemerisOverwatcher(self)
        self.weather = WeatherOverwatcher(self)

        self.is_running: bool = False

    async def run(self):
        """Starts the overwatcher tasks."""

        if self.is_running:
            raise GortError("Overwatcher is already running.")

        if not self.gort.is_connected():
            await self.gort.init()

        for module in OverwatcherModule.instances:
            self.gort.log.debug(f"Starting overwatcher module {module.name!r}")
            await module.run()

        self.is_running = True

        return self

    async def cancel(self):
        """Cancels the overwatcher tasks."""

        for module in OverwatcherModule.instances:
            self.gort.log.debug(f"Cancelling overwatcher module {module.name!r}")
            await module.cancel()

        for _, task in self.tasks.items():
            await cancel_task(task)

        self.tasks = {}

        self.is_running = False

    async def emergency_shutdown(self):
        """Shuts down the observatory in case of an emergency."""

        await self.gort.shutdown()
