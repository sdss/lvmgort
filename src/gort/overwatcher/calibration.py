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
import warnings

from typing import TYPE_CHECKING, Any, Literal, Self, cast

from astropy import time as ap_time
from pydantic import BaseModel, Field, model_validator

from sdsstools import read_yaml_file

from gort.exceptions import GortUserWarning, OverwatcherError
from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.tools import redis_client_sync


if TYPE_CHECKING:
    from gort.overwatcher.ephemeris import EphemerisModel
    from gort.overwatcher.notifier import NotificationLevel
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
        None,
        title="The minimum start time. The format depends on time_mode.",
    )
    max_start_time: float | None = Field(
        None,
        title="The maximum start time. The format depends on time_mode.",
    )
    time_mode: TimeModeType | None = Field(
        None,
        title="The time mode for the calibration.",
    )
    after: str | None = Field(
        None,
        title="Run after this calibration. Incompatible with min/max_start_time.",
    )
    required: bool = Field(
        True,
        title="Whether the calibration is required. Currently not used.",
    )
    dome: Literal["open", "closed"] | None = Field(
        False,
        title="Whether the dome should be open during the calibration. A null value "
        "will keep the dome in the current position.",
    )
    close_dome_after: bool = Field(
        False,
        title="Whether the dome should be closed after the calibration.",
    )
    abort_observing: bool = Field(
        False,
        title="Whether observing should be immediately aborted to "
        "allow the calibration to start.",
    )
    priority: int = Field(
        5,
        title="The priority of the calibration. Currently not used.",
    )
    max_try_time: float = Field(
        300,
        title="The maximum time in seconds to attempt the calibration if it fails. "
        "If max_start_time is reached during this period, the calibrations fails.",
    )

    @model_validator(mode="after")
    def validate_start_time(self) -> Self:
        """Validates ``min/max_start_time``, ``time_mode`` and ``after``."""

        if not self.after and not self.min_start_time:
            raise OverwatcherError("min_start_time or after are required.")

        if self.after and self.min_start_time:
            raise OverwatcherError("Cannot specify min_start_time and after.")

        if self.after and self.max_start_time and self.time_mode:
            warnings.warn(
                "Ignoring time_mode when after is specified.",
                GortUserWarning,
            )

        return self


class CalibrationState(enum.StrEnum):
    """The state of a calibration."""

    WAITING = "waiting"
    RUNNING = "running"
    RETRYING = "retrying"
    FAILED = "failed"
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

    def get_start_time(self) -> tuple[float | None, float | None]:
        """Determines the actual time at which the calibration will start.

        Also returns the latest time at which the calibration can start (which
        could be ``None`` if there is no limit). All times are returned as UNIX
        timestamps.

        """

        cal = self.model

        if not cal.min_start_time and not cal.max_start_time:
            return None, None

        sunrise = self.ephemeris.sunrise
        sunset = self.ephemeris.sunset

        min_start_time: float | None = None
        max_start_time: float | None = None

        # Convert everything to JD for now.
        if cal.time_mode == "secs_after_sunset":
            if cal.min_start_time:
                min_start_time = sunset + cal.min_start_time / 86400
            if cal.max_start_time:
                max_start_time = sunset + cal.max_start_time / 86400

        elif cal.time_mode == "secs_before_sunrise":
            if cal.min_start_time:
                min_start_time = sunrise - cal.min_start_time / 86400
            if cal.max_start_time:
                max_start_time = sunrise - cal.max_start_time / 86400

        elif cal.time_mode == "jd":
            if cal.min_start_time:
                min_start_time = cal.min_start_time
            if cal.max_start_time:
                max_start_time = cal.max_start_time

        elif cal.time_mode == "utc":
            # SJD at the time of observation is always the following midnight.
            # In this case we want to refer times from the previous midnight.
            base_date = ap_time.Time(self.ephemeris.SJD - 1, format="mjd")

            if cal.min_start_time:
                min_time_d = ap_time.TimeDelta(cal.min_start_time * 3600, format="sec")
                min_start_time = (base_date + min_time_d).jd

            if cal.max_start_time:
                max_start_time_c = cal.max_start_time
                if cal.min_start_time and max_start_time_c < cal.min_start_time:
                    max_start_time_c += 24
                max_time_d = ap_time.TimeDelta(max_start_time_c * 3600, format="sec")
                max_start_time = (base_date + max_time_d).jd

        # Now convert JD to UNIX ap_time.
        min_start_time_unix: float | None = None
        max_start_time_unix: float | None = None

        if min_start_time:
            min_start_time_unix = ap_time.Time(min_start_time, format="jd").unix

        if max_start_time:
            max_start_time_unix = ap_time.Time(max_start_time, format="jd").unix

        return min_start_time_unix, max_start_time_unix

    def to_dict(self) -> dict[str, Any]:
        """Returns the calibration as a dictionary."""

        return {
            "name": self.name,
            "start_time": self.start_time,
            "max_start_time": self.max_start_time,
            "state": self.state.name.lower(),
        }

    def is_finished(self):
        """Returns ``True`` if the calibration is done or has failed."""

        if self.state in (CalibrationState.DONE, CalibrationState.FAILED):
            return True

    def record_state(self, state: CalibrationState | None = None):
        """Records the state of the calibration in Redis."""

        if state is not None:
            self.state = state

        with redis_client_sync() as redis:
            key = f"overwatcher:calibrations:{self.schedule.sjd}"
            redis.json().set(key, f".{self.name}.state", self.state.name.lower())


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
        clear: bool = False,
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
                clear = True

            if clear:
                # Purge all existing data.
                data = {cal.name: cal.to_dict() for cal in self.calibrations}
                redis.json().set(key, "$", data)
            else:
                # Get calibration data from Redis and update the object.
                redis_cal_keys = redis.json().objkeys(key, "$")[0]

                for cal in self.calibrations:
                    if redis_cal_keys and cal.name in redis_cal_keys:
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

        # First check if any calibration is running. In that case just return None.
        for cal in self.calibrations:
            if cal.state == CalibrationState.RUNNING:
                return None

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
                    f"Skipping calibration {cal.name!r} as it's too late to start it.",
                    level="warning",
                )
                cal.record_state(CalibrationState.FAILED)
                continue

            if cal.model.after is not None:
                if cal.model.after in done_cals:
                    return cal
                continue

            if cal.start_time is None:
                self.log.warning(
                    f"Calibration {cal.name!r} has no start time and after is null. "
                    "This should not happen."
                )
                cal.record_state(CalibrationState.FAILED)
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
                dome_open = await overwatcher.gort.enclosure.is_open()
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
        await self.module.reset()

        while True:
            next_calibration = await self.module.schedule.get_next()

            if next_calibration is not None:
                try:
                    await self.module.run_calibration(next_calibration)
                except Exception as ee:
                    await self.module.overwatcher.notify(
                        f"Error running calibration {next_calibration.name!r}: {ee}",
                        level="error",
                    )
                    next_calibration.record_state(CalibrationState.FAILED)

            await asyncio.sleep(10)


class CalibrationsOverwatcher(OverwatcherModule):
    """Calibrations overwatcher module."""

    name = "calibration"

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

        self.schedule.update_schedule(self.cals_file)

    def is_calibration_running(self):
        """Returns ``True`` if a calibration is currently running."""

        for cal in self.schedule.calibrations:
            if cal.state == CalibrationState.RUNNING:
                return True

        return False

    async def run_calibration(self, calibration: Calibration):
        """Runs a calibration."""

        notify = self.overwatcher.notify

        name = calibration.name
        max_start_time = calibration.max_start_time
        dome = calibration.model.dome
        close_dome_after = calibration.model.close_dome_after
        recipe = calibration.model.recipe

        if name in self._ignore_cals:
            return

        if self.overwatcher.state.dry_run:
            self.log.warning(f"Dry-run mode. Not running calibration {name!r}.")
            self._ignore_cals.add(name)
            return

        if self.is_calibration_running():
            await self._fail_calibration(
                calibration,
                f"Cannot run {name!r}. A calibration is already running!",
                level="warning",
            )
            return

        if not self.overwatcher.state.allow_dome_calibrations:
            await self._fail_calibration(
                calibration,
                f"Cannot run {name!r}. Dome calibrations are disabled.",
                level="error",
            )

            return

        if name not in self._failing_cals:
            await notify(f"Running calibration {name!r}.")
        calibration.record_state(CalibrationState.RUNNING)

        if self.overwatcher.state.observing:
            immediate = calibration.model.abort_observing

            await notify(f"Aborting observations to run calibration {name}.")
            await self.overwatcher.observer.stop_observing(
                reason="Scheduled calibration",
                immediate=immediate,
                block=True,
            )

        now = time.time()
        if max_start_time is not None and now > max_start_time:
            await notify(
                f"Skipping calibration {name!r} as it's too late to start it.",
                level="warning",
            )
            calibration.record_state(CalibrationState.FAILED)
            return

        if dome is not None:
            dome_new = True if dome == "open" else False
            dome_current = await self.overwatcher.gort.enclosure.is_open()
            needs_dome_change = dome_new != dome_current

            if needs_dome_change and not self.overwatcher.state.enabled:
                await self._fail_calibration(
                    calibration,
                    f"Cannot move dome for {name!r}. Overwatcher is disabled.",
                    level="error",
                )
                return

            if dome == "open" and not dome_current:
                await notify("Opening the dome for calibration.")
                await self.overwatcher.gort.enclosure.open()
            elif dome == "closed" and dome_current:
                await notify("Closing the dome for calibration.")
                await self.overwatcher.gort.enclosure.close()

        await notify(f"Running recipe {recipe!r} for calibration {name!r}.")
        await self.overwatcher.gort.execute_recipe(recipe)

        if close_dome_after:
            await notify(f"Closing the dome after calibration {name!r}.")
            await self.overwatcher.gort.enclosure.close()

        await notify(f"Calibration {name!r} is done.")

        if name in self._failing_cals:
            self._failing_cals.pop(name)

        calibration.record_state(CalibrationState.DONE)

    async def _fail_calibration(
        self,
        calibration: Calibration,
        message: str,
        level: NotificationLevel = "error",
        repeat_notifications: bool = False,
    ):
        """Decides whether to fail a calibration or continue trying."""

        name = calibration.name
        max_try_time = calibration.model.max_try_time

        if max_try_time <= 0:
            await self.overwatcher.notify(message, level=level)
            calibration.record_state(CalibrationState.FAILED)
            return

        calibration.record_state(CalibrationState.RETRYING)

        if name not in self._failing_cals:
            await self.overwatcher.notify(message, level=level)
            self._failing_cals[name] = time.time()
            return

        if time.time() - self._failing_cals[name] > max_try_time:
            await self.overwatcher.notify(
                f"Maximum try time reached for calibration {name!r}. "
                "Failing the calibration now.",
                level=level,
            )
            calibration.record_state(CalibrationState.FAILED)
            self._failing_cals.pop(name)
            return

        if repeat_notifications:
            await self.overwatcher.notify(message, level=level)

        return
