#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-27
# @Filename: calibration.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import enum
import os
import pathlib
import time

from typing import TYPE_CHECKING, Any, Literal, Self, cast

from astropy import time as ap_time
from pydantic import BaseModel, Field, model_validator

from sdsstools import read_yaml_file

from gort.exceptions import OverwatcherError
from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.tools import add_night_log_comment, cancel_task, redis_client_sync


if TYPE_CHECKING:
    from gort.overwatcher.ephemeris import EphemerisModel
    from gort.overwatcher.helpers.notifier import NotificationLevel
    from gort.overwatcher.overwatcher import Overwatcher


__all__ = ["CalibrationsOverwatcher"]


ETC_DIR = pathlib.Path(__file__).parent / "../etc/"

PathType = os.PathLike | str | pathlib.Path
TimeModeType = Literal["secs_after_sunset", "secs_before_sunrise", "jd", "utc"]
CalsDataType = PathType | list["CalibrationModel"] | list[dict[str, Any]]


class CalibrationModel(BaseModel):
    """Calibration model."""

    name: str = Field(
        ...,
        title="The name of the calibration.",
    )
    recipe: str = Field(
        ...,
        title="The recipe to use.",
    )
    min_start_time: float | None = Field(
        default=None,
        title="The minimum start time. The format depends on time_mode.",
    )
    max_start_time: float | None = Field(
        default=None,
        title="The maximum start time. The format depends on time_mode.",
    )
    time_mode: TimeModeType | None = Field(
        default=None,
        title="The time mode for the calibration.",
    )
    after: str | None = Field(
        default=None,
        title="Run after this calibration.",
    )
    required: bool = Field(
        default=True,
        title="Whether the calibration is required. Currently not used.",
    )
    dome: Literal["open", "closed"] | None = Field(
        default=False,
        title="Whether the dome should be open during the calibration. A null value "
        "will keep the dome in the current position.",
    )
    close_dome_after: bool = Field(
        default=False,
        title="Whether the dome should be closed after the calibration.",
    )
    abort_observing: bool = Field(
        default=False,
        title="Whether observing should be immediately aborted to "
        "allow the calibration to start.",
    )
    priority: int = Field(
        default=5,
        title="The priority of the calibration. Currently not used.",
    )
    max_try_time: float = Field(
        default=300,
        title="The maximum time in seconds to attempt the calibration if it fails. "
        "If max_start_time is reached during this period, the calibrations fails.",
    )
    allow_post_observing_recovery: bool = Field(
        default=True,
        title="Whether the calibration can be run after observing has finished "
        "if it initially failed.",
    )

    @model_validator(mode="after")
    def validate_start_time(self) -> Self:
        """Ensure that ``after`` or ``min_start_time`` are defined."""

        if not self.after and self.min_start_time is None:
            raise OverwatcherError("min_start_time or after are required.")

        return self


class CalibrationState(enum.StrEnum):
    """The state of a calibration."""

    WAITING = "waiting"
    RUNNING = "running"
    RETRYING = "retrying"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DONE = "done"


class Calibration:
    """Keeps track of the state of a calibration."""

    def __init__(
        self,
        schedule: CalibrationSchedule,
        calibration: CalibrationModel | dict[str, Any],
    ):
        self.schedule = schedule
        self.ephemeris = self.schedule.ephemeris

        self.model = (
            calibration
            if isinstance(calibration, CalibrationModel)
            else CalibrationModel(**calibration)
        )

        self.name = self.model.name
        self.start_time, self.max_start_time = self.get_start_time()

        self.state = CalibrationState.WAITING

        self._task_queue: set[asyncio.Task] = set()

    def get_start_time(self) -> tuple[float | None, float | None]:
        """Determines the actual time at which the calibration will start.

        Also returns the latest time at which the calibration can start (which
        could be ``None`` if there is no limit). All times are returned as UNIX
        timestamps.

        """

        cal = self.model

        if cal.min_start_time is None and cal.max_start_time is None:
            return None, None

        sunrise = self.ephemeris.sunrise
        sunset = self.ephemeris.sunset

        min_start_time: float | None = None
        max_start_time: float | None = None

        # Convert everything to JD for now.
        if cal.time_mode == "secs_after_sunset":
            if cal.min_start_time is not None:
                min_start_time = sunset + cal.min_start_time / 86400
            if cal.max_start_time is not None:
                max_start_time = sunset + cal.max_start_time / 86400

        elif cal.time_mode == "secs_before_sunrise":
            if cal.min_start_time is not None:
                min_start_time = sunrise - cal.min_start_time / 86400
            if cal.max_start_time is not None:
                max_start_time = sunrise - cal.max_start_time / 86400

        elif cal.time_mode == "jd":
            if cal.min_start_time is not None:
                min_start_time = cal.min_start_time
            if cal.max_start_time is not None:
                max_start_time = cal.max_start_time

        elif cal.time_mode == "utc":
            # SJD at the time of observation is always the following midnight.
            # In this case we want to refer times from the previous midnight.
            base_date = ap_time.Time(self.ephemeris.SJD - 1, format="mjd")

            if cal.min_start_time is not None:
                min_time_d = ap_time.TimeDelta(cal.min_start_time * 3600, format="sec")
                min_start_time = (base_date + min_time_d).jd

            if cal.max_start_time is not None:
                max_start_time_c = cal.max_start_time
                if cal.min_start_time and max_start_time_c < cal.min_start_time:
                    max_start_time_c += 24
                max_time_d = ap_time.TimeDelta(max_start_time_c * 3600, format="sec")
                max_start_time = (base_date + max_time_d).jd

        # Now convert JD to UNIX ap_time.
        min_start_time_unix: float | None = None
        max_start_time_unix: float | None = None

        if min_start_time is not None:
            min_start_time_unix = ap_time.Time(min_start_time, format="jd").unix

        if max_start_time is not None:
            max_start_time_unix = ap_time.Time(max_start_time, format="jd").unix

        return min_start_time_unix, max_start_time_unix

    def to_dict(self) -> dict[str, Any]:
        """Returns the calibration as a dictionary."""

        return {
            "name": self.name,
            "state": self.state.name.lower(),
        }

    def is_finished(self):
        """Returns ``True`` if the calibration is done or has failed."""

        if self.state in (
            CalibrationState.DONE,
            CalibrationState.FAILED,
            CalibrationState.CANCELLED,
        ):
            return True

    def record_state(
        self,
        state: CalibrationState | None = None,
        fail_reason: str = "unespecified reason",
        add_to_night_log: bool = True,
    ):
        """Records the state of the calibration in Redis."""

        if state is not None:
            self.state = state

        with redis_client_sync() as redis:
            key = f"overwatcher:calibrations:{self.schedule.sjd}"
            redis.json().set(key, f".{self.name}.state", self.state.name.lower())

        # Add a comment to the night log section for issues indicating the calibration
        # has failed and the reason for it.
        if add_to_night_log:
            if not fail_reason.endswith("."):
                fail_reason += "."

            task = asyncio.create_task(
                add_night_log_comment(
                    f"Calibration {self.name} failed. Reason: {fail_reason}"
                    " See log for more details.",
                    category="overwatcher",
                ),
            )
            self._task_queue.add(task)
            task.add_done_callback(self._task_queue.discard)


class CalibrationSchedule:
    """A class to handle how to schedule calibrations.

    Parameters
    ----------
    cals_overwatcher
        The calibrations overwatcher instance that will monitor the schedule.
    cals_data
        The calibration data to use to generate the schedule. Either a YAML file
        with the calibrations to be taken, or a list of ``CalibrationModel`` instances.
    update
        Update the schedule on init.

    """

    def __init__(
        self,
        cals_overwatcher: CalibrationsOverwatcher,
        cals_data: CalsDataType,
        update: bool = True,
    ):
        self.cals_overwatcher = cals_overwatcher
        self.log = self.cals_overwatcher.log

        self.calibrations: list[Calibration] = []

        # These are set by .refresh()
        self.sjd: int
        self.ephemeris: EphemerisModel

        if update:
            self.update_schedule(cals_data=cals_data)

    def update_schedule(
        self,
        cals_data: CalsDataType | None = None,
        reset: bool = False,
    ):
        """Generates a schedule of calibrations for the night."""

        if not self.cals_overwatcher.overwatcher.ephemeris.ephemeris:
            self.log.error("Ephemeris are not available. Not updating schedule.")
            return

        self.ephemeris = self.cals_overwatcher.overwatcher.ephemeris.ephemeris
        self.sjd = self.cals_overwatcher.overwatcher.ephemeris.sjd

        # Update the calibration models.
        if cals_data is not None:
            if isinstance(cals_data, (list, tuple)):
                cals_data = [
                    CalibrationModel(**cal) if isinstance(cal, dict) else cal
                    for cal in cals_data
                ]
            else:
                cals_data = self.read_file(cals_data)

            self.calibrations = [Calibration(self, cal) for cal in cals_data]

        else:
            pass

        if len(self.calibrations) == 0:
            self.log.warning("No calibrations found.")
            return

        self.log.info(f"Updating calibrations schedule for SJD {self.sjd}.")

        # Get the state of the calibrations from Redis. This is important if for
        # example the overwatcher is restarted after some calibrations have already
        # been taken. We use sync Redis because this should be fast and allows
        # update_schedule() to be called by __init__().
        with redis_client_sync() as redis:
            # The key we use to store the information for this SJD.
            key = f"overwatcher:calibrations:{self.sjd}"

            # Check if the key exists. If it does not, it means we haven't done
            # anything yet for this SJD.
            if not redis.exists(key):
                # Purge all existing data.
                data = {cal.name: cal.to_dict() for cal in self.calibrations}
                redis.json().set(key, "$", data)
            else:
                # Get calibration data from Redis and update the object.
                redis_cal_keys = redis.json().objkeys(key, "$")[0]

                for cal in self.calibrations:
                    if not reset and redis_cal_keys and cal.name in redis_cal_keys:
                        state = cast(str, redis.json().get(key, f".{cal.name}.state"))
                        cal.state = CalibrationState(state.lower())
                    else:
                        redis.json().set(key, f".{cal.name}", cal.to_dict())

    def read_file(self, filename: PathType) -> list[CalibrationModel]:
        """Loads calibrations from a file."""

        cal_data: list = read_yaml_file(filename, return_class=list)  # type: ignore
        if not isinstance(cal_data, (list, tuple)):
            raise ValueError("Calibrations file is badly formatted.")

        return [CalibrationModel(**cal) for cal in cal_data]

    async def get_next(self):
        """Returns the next calibration or ``None`` if no calibration is due.

        Takes into account open dome buffer time.

        """

        # First check if any calibration is running. If that's the case,
        # return it immediately.
        for cal in self.calibrations:
            if cal.state == CalibrationState.RUNNING:
                return cal

        overwatcher = self.cals_overwatcher.overwatcher
        open_dome_buffer = overwatcher.config["overwatcher.scheduler.open_dome_buffer"]

        now: float = time.time()
        done_cals: set[str] = set()

        for cal in self.calibrations:
            if cal.name in done_cals or cal.is_finished():
                done_cals.add(cal.name)
                continue

            # If it's too late to start the calibration, skip it.
            if cal.max_start_time is not None and now > cal.max_start_time:
                await overwatcher.notify(
                    f"Skipping calibration {cal.name} as it's too late to start it.",
                    level="error",
                )
                cal.record_state(
                    CalibrationState.FAILED,
                    fail_reason="too late to start the calibration.",
                )
                continue

            if cal.model.after is not None:
                if cal.model.after not in done_cals:
                    continue

            if cal.start_time is None:
                if cal.model.after is not None:
                    return cal

                self.log.warning(
                    f"Calibration {cal.name} has no start time and after is null. "
                    "This should not happen."
                )
                cal.record_state(CalibrationState.FAILED, add_to_night_log=False)
                continue

            # If the calibration start time is too fast in the future even considering
            # that we may need to move the dome, continue. This is mostly to avoid
            # too many calls to gort.enclosure.is_open() which are kind of slow.
            if now < cal.start_time - open_dome_buffer:
                continue

            # If the calibration requires a specific position for the dome,
            # check the current dome position and adjust the start time if needed.
            min_start_time_dome: float = cal.start_time
            if (dome_requested := cal.model.dome) is not None:
                dome_open = await overwatcher.dome.is_opening()
                dome_requested_bool = True if dome_requested == "open" else False

                if dome_requested_bool is not dome_open:
                    min_start_time_dome = cal.start_time - open_dome_buffer

            if now >= min_start_time_dome:
                return cal

        return None


class CalibrationsMonitor(OverwatcherModuleTask["CalibrationsOverwatcher"]):
    """Monitors the calibrations schedule."""

    name = "calibrations_monitor"
    keep_alive = True
    restart_on_error = True

    async def task(self):
        """Runs the calibration monitor."""

        # Small delay to make sure the ephemeris have been update and we can
        # then update the schedule.
        await asyncio.sleep(5)

        notify = self.overwatcher.notify

        while True:
            next_calibration = await self.module.schedule.get_next()
            allow_calibrations = self.module.overwatcher.state.allow_calibrations

            if next_calibration is not None and allow_calibrations:
                name = next_calibration.name
                try:
                    self.module._calibration_task = asyncio.create_task(
                        self.module.run_calibration(next_calibration)
                    )
                    await self.module._calibration_task
                except asyncio.CancelledError:
                    await notify(
                        f"Calibration {name} has been cancelled.",
                        level="warning",
                    )
                    next_calibration.record_state(
                        CalibrationState.CANCELLED,
                        fail_reason="calibration cancelled by Overwatcher or user.",
                    )
                except Exception as ee:
                    await notify(
                        f"Error running calibration {name}: {ee}",
                        level="error",
                    )
                    next_calibration.record_state(
                        CalibrationState.FAILED,
                        fail_reason=str(ee),
                    )
                finally:
                    if next_calibration.is_finished():
                        dome_closed = await self.module.overwatcher.dome.is_closing()
                        if next_calibration.model.close_dome_after and not dome_closed:
                            await notify(f"Closing the dome after calibration {name}.")
                            await self.overwatcher.dome.close()

            await asyncio.sleep(10)


class CalibrationsOverwatcher(OverwatcherModule):
    """Calibrations overwatcher module."""

    name = "calibration"
    delay = 3

    tasks = [CalibrationsMonitor()]

    def __init__(
        self,
        overwatcher: Overwatcher,
        cals_file: str | pathlib.Path | None = None,
    ):
        super().__init__(overwatcher)

        self._default_cals_file = ETC_DIR / "calibrations.yaml"
        self.cals_file = pathlib.Path(cals_file or self._default_cals_file)

        self.schedule = CalibrationSchedule(self, self.cals_file, update=False)

        self._calibration_task: asyncio.Task | None = None

        self._failing_cals: dict[str, float] = {}
        self._ignore_cals: set[str] = set()

    async def reset(self, cals_file: str | pathlib.Path | None = None):
        """Resets the list of calibrations for a new SJD.

        This method is usually called by the ephemeris overwatcher when a new SJD
        is detected.

        """

        while self.overwatcher.ephemeris.ephemeris is None:
            self.log.error("Ephemeris is not available. Cannot reset calibrations.")
            await asyncio.sleep(5)

        if cals_file is not None:
            self.cals_file = cals_file

        self._failing_cals = {}
        self._ignore_cals = set()

        try:
            self.schedule.update_schedule(self.cals_file)
        except Exception as ee:
            self.log.error(f"Error updating calibrations schedule: {ee!r}")

    def get_running_calibration(self):
        """Returns the calibration currently running."""

        for cal in self.schedule.calibrations:
            if cal.state == CalibrationState.RUNNING:
                return cal

    async def run_calibration(self, calibration: Calibration):
        """Runs a calibration."""

        notify = self.overwatcher.notify

        name = calibration.name
        max_start_time = calibration.max_start_time
        dome = calibration.model.dome
        recipe = calibration.model.recipe

        if name in self._ignore_cals:
            return

        if self.overwatcher.state.dry_run:
            self.log.warning(f"Dry-run mode. Not running calibration {name}.")
            self._ignore_cals.add(name)
            return

        running_cal = self.get_running_calibration()
        if running_cal is not None:
            await self._fail_calibration(
                calibration,
                f"Cannot run {name}. A calibration is already running!",
                level="warning",
            )
            return

        if calibration.state == CalibrationState.RUNNING:
            self.log.warning(
                f"Calibration {name} is recorded as running but it is not. "
                "Setting its status to WAITING."
            )
            calibration.record_state(CalibrationState.WAITING)

        if not self.overwatcher.state.allow_calibrations:
            await self._fail_calibration(
                calibration,
                f"Cannot run {name}. Calibrations are disabled.",
                level="error",
            )
            return

        if name not in self._failing_cals:
            await notify(f"Running calibration {name}.")

        calibration.record_state(CalibrationState.RUNNING)

        now = time.time()
        if max_start_time is not None and now > max_start_time:
            await notify(
                f"Skipping calibration {name} as it's too late to start it.",
                level="warning",
            )
            calibration.record_state(
                CalibrationState.FAILED,
                fail_reason="too late to start the calibration.",
            )
            return

        if self.overwatcher.state.observing:
            immediate = calibration.model.abort_observing

            await notify(f"Aborting observations to run calibration {name}.")
            await self.overwatcher.observer.stop_observing(
                reason="Scheduled calibration",
                immediate=immediate,
                block=True,
            )

        if dome is not None:
            dome_new = True if dome == "open" else False
            dome_current = await self.overwatcher.dome.is_opening()
            needs_dome_change = dome_new != dome_current

            if needs_dome_change:
                if await self.overwatcher.dome.is_moving():
                    try:
                        await notify("The dome is moving. Waiting for it to stop.")
                        await asyncio.wait_for(
                            self.overwatcher.dome.wait_until_idle(),
                            timeout=180,
                        )
                    except asyncio.TimeoutError:
                        await self._fail_calibration(
                            calibration,
                            f"Cannot move dome for calibration {name}. "
                            "Dome is still moving after 3 minutes.",
                            level="error",
                            fail_now=True,
                        )
                        return

                if not self.overwatcher.state.safe:
                    # Fail immediately if the weather is not safe. There is little
                    # chance that conditions will improve in the next few minutes.
                    await self._fail_calibration(
                        calibration,
                        f"Cannot move dome for {name}. Weather is not safe.",
                        level="error",
                        fail_now=True,
                    )
                    return

                if not self.overwatcher.state.enabled:
                    await self._fail_calibration(
                        calibration,
                        f"Cannot move dome for {name}. Overwatcher is disabled.",
                        level="error",
                    )
                    return

                if dome == "open" and not self.overwatcher.state.safe:
                    await self._fail_calibration(
                        calibration,
                        f"Cannot move dome for {name}. Weather is not safe.",
                        level="warning",
                    )
                    return

                if dome == "open" and not dome_current:
                    await notify(f"Opening the dome for calibration {name}.")
                    await self.overwatcher.dome.open()
                elif dome == "closed" and dome_current:
                    await notify(f"Closing the dome for calibration {name}.")
                    await self.overwatcher.dome.close()

        await notify(f"Running recipe {recipe!r} for calibration {name}.")
        await self.overwatcher.gort.execute_recipe(recipe)

        await notify(f"Calibration {name} is done.")

        if name in self._failing_cals:
            self._failing_cals.pop(name)

        calibration.record_state(CalibrationState.DONE)

    async def cancel(self):
        """Cancel the running calibration."""

        notify = self.overwatcher.notify

        running_calibration = self.get_running_calibration()

        if (
            self._calibration_task is not None
            and not self._calibration_task.done()
            and running_calibration
        ):
            name = running_calibration.name

            await notify(f"Cancelling calibration {name}.", level="warning")
            self._calibration_task = await cancel_task(self._calibration_task)

            # Ensure we close the dome. This is allowed even
            # if the overwatcher is disabled.
            if running_calibration.model.close_dome_after:
                await notify(f"Closing the dome after calibration {name}.")
                await self.overwatcher.dome.close()

    async def _fail_calibration(
        self,
        calibration: Calibration,
        message: str,
        level: NotificationLevel = "error",
        repeat_notifications: bool = False,
        fail_now: bool = False,
    ):
        """Decides whether to fail a calibration or continue trying."""

        name = calibration.name
        max_try_time = calibration.model.max_try_time

        if max_try_time <= 0 or fail_now:
            await self.overwatcher.notify(message, level=level)
            calibration.record_state(CalibrationState.FAILED, fail_reason=message)
            return

        calibration.record_state(CalibrationState.RETRYING)

        if name not in self._failing_cals:
            await self.overwatcher.notify(message, level=level)
            self._failing_cals[name] = time.time()
            return

        if time.time() - self._failing_cals[name] > max_try_time:
            await self.overwatcher.notify(
                f"Maximum try time reached for calibration {name}. "
                "Failing the calibration now.",
                level=level,
            )
            calibration.record_state(
                CalibrationState.FAILED,
                fail_reason="maximum time reached trying to run the calibration. "
                f"Oringinal error: {message}",
            )
            self._failing_cals.pop(name)
            return

        if repeat_notifications:
            await self.overwatcher.notify(message, level=level)

        return
