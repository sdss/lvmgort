#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: core.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import abc
import asyncio
from weakref import WeakSet

from typing import TYPE_CHECKING, Coroutine

from gort.exceptions import GortError
from gort.tools import cancel_task


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


class OverwatcherModule(metaclass=abc.ABCMeta):
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

        self.tasks: list[asyncio.Task] = []
        self.is_running: bool = False

        self.log = self.overwatcher.log

    async def run(self):
        """Runs the overwatcher module."""

        if self.is_running:
            raise GortError(f"{self.name!r} overwatcher is already running.")

        self.is_running = True

        for coro in self.list_task_coros():
            self.tasks.append(asyncio.create_task(coro))

        return self

    async def cancel(self):
        """Stops the overwatcher module."""

        for task in self.tasks:
            await cancel_task(task)

        self.tasks = []
        self.is_running = False

    @abc.abstractmethod
    def list_task_coros(self) -> list[Coroutine]:
        """ "Returns a list of task coroutines that will be schedule on `.run`."""

        return []
