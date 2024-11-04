#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-10-31
# @Filename: tasks.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import abc
import time

import typing
from typing import TYPE_CHECKING, Literal, TypedDict, cast

from astropy.time import Time

from sdsstools import get_sjd

from gort.tools import redis_client_sync


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher

VALID_TASKS = Literal["pre_observing"]


class TaskStatus(TypedDict):
    name: str
    mjd: float | None
    done: bool


class DailyTasks(dict[Literal[VALID_TASKS], "DailyTaskBase"]):
    def __init__(self, overwatcher: Overwatcher):
        tasks = {
            DailyTaskClass.name: DailyTaskClass(overwatcher)
            for DailyTaskClass in DailyTaskBase.__subclasses__()
            if DailyTaskClass.name in typing.get_args(VALID_TASKS)
        }
        self.tasks = [task.name for task in tasks.values()]
        super().__init__(tasks)

    async def run_all(self):
        """Run all the tasks."""

        for task in self.values():
            await task.run()


class DailyTaskBase(metaclass=abc.ABCMeta):
    """A class to handle a daily task."""

    name: str

    def __init__(self, overwatcher: Overwatcher):
        self.overwatcher = overwatcher

        self._status: TaskStatus = {
            "name": self.name,
            "mjd": get_sjd("LCO"),
            "done": False,
        }

        self.update_status(initial=True)

    def update_status(self, initial: bool = False):
        """Records the status in Redis."""

        current_mjd = get_sjd("LCO")
        key = f"overwatcher:daily_tasks:{self.name}"

        # Get current Redis status.
        with redis_client_sync() as redis:
            redis_status = cast(TaskStatus | None, redis.json().get(key))

        # If the MJD has changed, ignore the Redis status.
        if redis_status is not None and redis_status["mjd"] != current_mjd:
            self._status["done"] = False

        # If we are instantiating the class and the Redis value is valid, use it.
        if initial and redis_status is not None:
            self._status = redis_status
            if redis_status["mjd"] != current_mjd:
                self._status["done"] = False

        # Now ensure the correct MJD and write the status to Redis.
        self._status["mjd"] = current_mjd

        with redis_client_sync() as redis:
            redis.json().set(key, "$", dict(self._status))

    async def run(self):
        """Runs the task."""

        # Check if the MJD has changed. If so, update the status.
        current_mjd = get_sjd("LCO")
        if current_mjd != self._status["mjd"]:
            self.update_status()

        if self.done:
            return

        self.done = await self._run_internal()
        if self.done:
            self.overwatcher.log.debug(f"Task {self.name!r} has been completed.")

    @abc.abstractmethod
    async def _run_internal(self) -> bool:
        """Runs the internal task."""

        raise NotImplementedError

    @property
    def mjd(self):
        """Returns the MJD."""

        return self._status["mjd"]

    @property
    def done(self):
        """Returns True if the task is done."""

        return self._status["done"]

    @done.setter
    def done(self, value: bool):
        """Sets the task as done."""

        assert isinstance(value, bool)

        self._status["done"] = value
        self.update_status()

    def mark_done(self):
        """Marks the task as done."""

        self.done = True


class PreObservingTask(DailyTaskBase):
    """Run the pre-observing tasks."""

    name = "pre_observing"

    async def _run_internal(self) -> bool:
        """Runs the pre-observing tasks."""

        if self.overwatcher.ephemeris.ephemeris is None:
            return False

        # Run this task 30 minutes before sunset.
        now = time.time()
        sunset = Time(self.overwatcher.ephemeris.ephemeris.sunset, format="jd").unix

        if (
            sunset - now < 0
            or sunset - now > 1800
            or sunset - now < 600
            or self.overwatcher.state.calibrating
            or self.overwatcher.state.observing
        ):
            return False

        try:
            self.overwatcher.log.info(f"Running daily task {self.name!r}.")
            await self.overwatcher.gort.execute_recipe("pre-observing")
        except Exception as err:
            self.overwatcher.log.error(f"Error running pre-observing tasks: {err}")

        # Always mark the task complete, even if it failed.
        return True
