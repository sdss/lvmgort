#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: overwatcher.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from weakref import WeakSet

from typing import TYPE_CHECKING

from sdsstools import get_sjd

from gort.tools import cancel_task


if TYPE_CHECKING:
    from gort.gort import Gort


class Overwatcher:
    """Monitors the observatory."""

    def __init__(self, gort: Gort):
        from gort.overwatcher.weather import WeatherOverwatcher

        self.gort = gort

        self.sjd = get_sjd("LCO")

        self.tasks: dict[str, asyncio.Task] = {}

        self.weather = WeatherOverwatcher(self.gort)

    async def run(self):
        """Starts the overwatcher tasks."""

        for module in OverwatcherModule.instances:
            self.gort.log.debug(f"Starting overwatcher module {module.name!r}")
            await module.run()

    async def cancel(self):
        """Cancels the overwatcher tasks."""

        for module in OverwatcherModule.instances:
            self.gort.log.debug(f"Cancelling overwatcher module {module.name!r}")
            await module.cancel()

        for task_name, task in self.tasks.items():
            await cancel_task(task)
            del self.tasks[task_name]

    async def emergency_shutdown(self):
        """Shuts down the observatory in case of an emergency."""

        await self.gort.shutdown()


class OverwatcherModule:
    """A generic overwatcher module."""

    instances = WeakSet()
    name: str = "generic"

    def __new__(cls, *args, **kwargs):
        instance = object.__new__(cls)
        cls.instances.add(instance)

        return instance

    def __init__(self, gort: Gort):
        self.gort = gort

        self.tasks: dict[str, asyncio.Task] = {}

    async def run(self):
        """Runs the overwatcher module."""

        return self

    async def cancel(self):
        """Stops the overwatcher module."""

        for task_name, task in self.tasks.items():
            await cancel_task(task)
            del self.tasks[task_name]
