#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: core.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from unittest.mock import Mock
from weakref import WeakSet

from typing import TYPE_CHECKING, ClassVar, Generic, TypeVar

from gort.core import LogNamespace
from gort.exceptions import GortError
from gort.tools import cancel_task


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


OverwatcherModule_T = TypeVar("OverwatcherModule_T", bound="OverwatcherModule")


class OverwatcherBaseTask:
    """A task that runs in an overwatcher module."""

    name: ClassVar[str]
    keep_alive: ClassVar[bool] = True
    restart_on_error: ClassVar[bool] = True

    def __init__(self, log: LogNamespace | None = None):
        self._task_runner: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

        self._log: Mock | LogNamespace = log or Mock()

    async def run(self):
        """Runs the task."""

        self._task_runner = asyncio.create_task(self.task())
        self._log.debug(f"Task {self.name!r} started.")

        self._heartbeat_task = asyncio.create_task(self.monitor_task_heartbeat())

    async def cancel(self):
        """Cancels the task."""

        await cancel_task(self._task_runner)
        await cancel_task(self._heartbeat_task)

        self._task_runner = None
        self._heartbeat_task = None

        self._log.debug(f"Task {self.name!r} was cancelled.")

    async def task(self):
        """The task to run."""

        raise NotImplementedError("The task coroutine has not been implemented.")

    async def monitor_task_heartbeat(self):
        """Monitors the task and restarts it if it fails."""

        while True:
            await asyncio.sleep(1)

            if not self._task_runner:
                continue

            if self._task_runner.cancelled():
                if self.keep_alive:
                    self._log.warning(f"Task {self.name!r} was cancelled. Restarting.")
                    self._task_runner = asyncio.create_task(self.task())
                return

            if self._task_runner.done():
                if exception := self._task_runner.exception():
                    self._log.error(f"Task {self.name!r} failed: {exception!r}")
                    if self.restart_on_error or self.keep_alive:
                        self._log.warning(f"Task {self.name!r} failed. Restarting.")
                        self._task_runner = asyncio.create_task(self.task())
                        continue

                if self.keep_alive:
                    self._log.warning(f"Task {self.name!r} finished. Restarting.")
                    self._task_runner = asyncio.create_task(self.task())
                    continue


class OverwatcherModuleTask(OverwatcherBaseTask, Generic[OverwatcherModule_T]):
    """A task that runs in an overwatcher module."""

    def __init__(self):
        super().__init__()

        self._module: OverwatcherModule_T | None = None

    @property
    def module(self):
        """Returns the module instance."""

        if not self._module:
            raise GortError("Task has not been associated with a module.")

        return self._module

    @property
    def overwatcher(self):
        """Returns the overwatcher instance."""

        return self.module.overwatcher

    @property
    def gort(self):
        """Returns the GORT instance."""

        return self.module.gort

    @property
    def config(self):
        """Returns the configuration dictionary."""

        return self.module.gort.config

    @property
    def log(self):
        """Returns the logger instance."""

        return self.module.log

    @property
    def notify(self):
        """Returns the Overwatcher notifier."""

        return self.module.overwatcher.notify

    async def run(self, module: OverwatcherModule_T):
        """Runs the task."""

        self._module = module

        await super().run()


class OverwatcherModule:
    """A generic overwatcher module."""

    instances = WeakSet()
    name: str = "generic"
    delay: float = 0

    tasks: ClassVar[list[OverwatcherModuleTask]]

    def __new__(cls, *args, **kwargs):
        instance = object.__new__(cls)
        cls.instances.add(instance)

        return instance

    def __init__(self, overwatcher: Overwatcher):
        if not hasattr(self, "tasks"):
            raise RuntimeError(
                f"{self.__class__.__name__} must define a `tasks` attribute."
            )

        self.overwatcher = overwatcher
        self.gort = overwatcher.gort
        self.notify = overwatcher.notify

        self.is_running: bool = False

        self.log = LogNamespace(self.gort.log, header=f"({self.__class__.__name__}) ")
        for task in self.tasks:
            task._log = self.log

    async def run(self):
        """Runs the overwatcher module."""

        if self.is_running:
            raise GortError(f"{self.name!r} overwatcher is already running.")

        await asyncio.sleep(self.delay)

        self.is_running = True

        for ov_task in self.tasks:
            await ov_task.run(self)

        return self

    async def cancel(self):
        """Stops the overwatcher module."""

        for ov_task in self.tasks:
            await ov_task.cancel()

        self.is_running = False
