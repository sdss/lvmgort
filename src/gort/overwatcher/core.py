#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: core.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from weakref import WeakSet

from typing import TYPE_CHECKING

from gort.tools import cancel_task


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


class OverwatcherModule:
    """A generic overwatcher module."""

    instances = WeakSet()
    name: str = "generic"

    def __new__(cls, *args, **kwargs):
        instance = object.__new__(cls)
        cls.instances.add(instance)

        return instance

    def __init__(self, overwatcher: Overwatcher):
        self.overwatcher = overwatcher
        self.gort = overwatcher.gort

        self.tasks: dict[str, asyncio.Task] = {}
        self.is_running: bool = False

    async def run(self):
        """Runs the overwatcher module."""

        self.is_running = True

        return self

    async def cancel(self):
        """Stops the overwatcher module."""

        for _, task in self.tasks.items():
            await cancel_task(task)

        self.tasks = {}

        self.is_running = False
