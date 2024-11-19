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

from gort.overwatcher.calibration import CalibrationState
from gort.tools import add_night_log_comment, redis_client_sync


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher

VALID_TASKS = Literal["pre_observing", "post_observing"]


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

        if not await self._should_run():
            return

        await self.overwatcher.notify(f"Running daily task {self.name}.")
        try:
            self.done = await self._run_internal()
        except Exception as err:
            await self.overwatcher.notify(
                f"Error running daily task {self.name}: {err}",
                level="error",
            )
            self.done = True
            return

        if self.done:
            await self.overwatcher.notify(f"Task {self.name} has been completed.")
        else:
            await self.overwatcher.notify(f"Task {self.name} has failed.")

    @abc.abstractmethod
    async def _should_run(self) -> bool:
        """Returns True if the task should run."""

        raise NotImplementedError

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
    """Run the pre-observing tasks.

    This task is run between 30 and 10 minutes before sunset if no calibration is
    ongoing and will take a bias and make sure the telescopes are connected and homed.

    """

    name = "pre_observing"

    async def _should_run(self) -> bool:
        """Returns True if the task should run."""

        if self.overwatcher.ephemeris.ephemeris is None:
            return False

        specs_idle = await self.overwatcher.gort.specs.are_idle()
        if not specs_idle:
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

        return True

    async def _run_internal(self) -> bool:
        """Runs the pre-observing tasks."""

        try:
            await self.overwatcher.gort.execute_recipe("pre-observing")
        except Exception as err:
            await self.overwatcher.notify(
                f"Error running pre-observing task: {err}",
                level="critical",
            )

        # Always mark the task complete, even if it failed.
        return True


class PostObservingTask(DailyTaskBase):
    """Run the post-observing tasks.

    This task is run 15 minutes after morning twilight. It runs the post-observing
    recipe but does not send the email (that is done by a cronjob for redundancy).

    The recipe checks that the dome is closed, the telescope is parked, guiders
    are off, etc. It also goes over the calibrations and if a calibration is missing
    and has ``allow_post_observing_recovery=true`` it will try to obtain it.

    """

    name = "post_observing"

    async def _should_run(self) -> bool:
        """Returns True if the task should run."""

        if self.overwatcher.ephemeris.ephemeris is None:
            return False

        specs_idle = await self.overwatcher.gort.specs.are_idle()
        if not specs_idle:
            return False

        # Time to twilight. This is negative after the twilight has started.
        time_to_twilight = self.overwatcher.ephemeris.time_to_morning_twilight()

        # Run this task 15 minutes after the morning twilight.
        if (
            time_to_twilight is None
            or time_to_twilight > 0
            or time_to_twilight > -900
            or time_to_twilight < -1800
            or self.overwatcher.state.calibrating
            or self.overwatcher.state.observing
        ):
            return False

        return True

    async def _run_internal(self) -> bool:
        """Runs the post-observing tasks."""

        notify = self.overwatcher.notify

        try:
            dome_closed = await self.overwatcher.dome.is_closing()
            if not dome_closed:
                await notify("Dome was found open. Closing now.")
                await self.overwatcher.dome.close()
        except Exception as err:
            await notify(f"Error closing the dome: {err}", level="critical")

        try:
            await self.overwatcher.gort.execute_recipe(
                "post-observing",
                send_email=False,
            )
        except Exception as err:
            await self.overwatcher.notify(
                f"Error running post-observing task: {err}",
                level="critical",
            )
            return True

        calibrations_attempted: bool = False

        for calibration in self.overwatcher.calibrations.schedule.calibrations:
            name = calibration.name

            # Calibration must not be done (any other state is valid)
            if calibration.state != CalibrationState.DONE:
                # Calibration must allow recovery.
                allows_recovery = calibration.model.allow_post_observing_recovery

                # Calibration must not require moving the dome (model.dome = None)
                # or asks for the dome to be closed and it actually is.
                required_dome = calibration.model.dome
                needs_dome: bool = False
                if required_dome is not None:
                    current_dome = await self.overwatcher.dome.is_closing()
                    if required_dome is True or current_dome != required_dome:
                        needs_dome = True

                # Calibrations must be allowed.
                allow_calibrations = self.overwatcher.state.allow_calibrations

                if not needs_dome and allows_recovery and allow_calibrations:
                    await notify(f"Retrying calibration {calibration.name}.")

                    try:
                        calibrations_attempted = True
                        await self.overwatcher.calibrations.run_calibration(calibration)

                        if not calibration.state == CalibrationState.DONE:
                            await notify(f"Failed to recover calibration {name}.")
                        else:
                            await notify(f"Calibration {name} recovered.")

                            # Automatically add a comment to the night log.
                            await add_night_log_comment(
                                f"Calibration {name} initially failed and was retaken "
                                "after observations had been completed. Review the "
                                "data quality since the exposures were taken after "
                                "sunrise.",
                                category="overwatcher",
                            )

                    except Exception as err:
                        await notify(f"Error recovering calibration {name}: {err}")

        # If we have tried a calibration we may have rehomed the telescopes and
        # left them not parked. Make sure they are really parked.
        if calibrations_attempted:
            self.overwatcher.log.info("Parking telescopes after post-observing cals.")
            await self.overwatcher.gort.telescopes.park()

        # Always mark the task complete, even if it failed.
        return True
