#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-27
# @Filename: calibration.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from dataclasses import dataclass
from unittest import mock

from typing import TYPE_CHECKING, Any

import jsonschema
import nptyping as npt
import numpy
import polars
from astropy import time

from sdsstools import get_sjd, read_yaml_file

from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.tools import (
    get_ephemeris_summary_sync,
    redis_client,
    run_in_executor,
)


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


__all__ = ["CalibrationsOverwatcher"]


ARRAY_F32 = npt.NDArray[npt.Shape["*, 2"], npt.Float32]


@dataclass(kw_only=True)
class Calibration:
    name: str
    expected_duration: float
    time_mode: str | None = None
    recipe: str | None = None
    script: str | None = None
    min_start_time: float | None = None
    max_start_time: float | None = None
    repeat: str = "never"
    required: bool = False
    open_dome: bool | None = None
    night_mode: bool = False
    priority: float = 5
    min_start_time_schedule: float | None = None
    max_start_time_schedule: float | None = None
    start_time: float | None = None
    end_time: float | None = None
    done: bool = False
    active: bool = False

    def time_to_calibration(self):
        """Returns the number of seconds to the beginning of the observation window."""

        jd_now = time.Time.now().jd

        if self.start_time is None:
            raise ValueError("Calibration does not have a start JD.")

        return (self.start_time - jd_now) * 86400

    async def get_last_taken(self):
        """Gets the last time the observation was taken from Redis."""

        client = redis_client()
        data = await client.hgetall(f"gort:overwatcher:calibrations:{self.name}")

        if data == {}:
            return None

        return time.Time(data.get("last_taken"), format="isot")

    async def mark_done(self):
        """Stores that the calibration was taken to Redis."""

        self.done = True

        client = redis_client()
        await client.hset(
            f"gort:overwatcher:calibrations:{self.name}",
            mapping={"last_taken": time.Time.now().isot},
        )


class CalibrationSchedule:
    """A class to handle how to schedule calibrations."""

    def __init__(
        self,
        calibrations: dict[str, Calibration],
        sjd: int | None = None,
        ephemeris: dict[str, Any] | None = None,
        log: logging.Logger | None = None,
    ):
        self.calibrations = calibrations
        self.sjd = sjd or get_sjd("LCO")
        self.ephemeris = ephemeris or get_ephemeris_summary_sync(sjd)

        self.log = log or mock.Mock(spec=logging.Logger)

    async def update_schedule(self):
        """Generates a schedule of calibrations for the night."""

        sunrise = self.ephemeris["sunrise"]
        sunset = self.ephemeris["sunset"]

        cals = self.calibrations.values()

        # Step 1. Disable calibrations that do not need to be taken tonight.
        # Set the scheduleable start and end time depending on time_mode.

        for cal in cals:
            last_taken = await cal.get_last_taken()
            last_taken_sjd = get_sjd("LCO", last_taken.datetime) if last_taken else None

            cal.done = False
            cal.active = False
            cal.start_time = None
            cal.end_time = None

            if last_taken_sjd == self.sjd:
                # The calibration has already been taken today.
                cal.done = True
                continue

            if last_taken_sjd:
                if (
                    (cal.repeat == "weekly" and self.sjd - last_taken_sjd < 7)
                    or (cal.repeat == "monthly" and self.sjd - last_taken_sjd < 30)
                    or cal.repeat == "never"
                ):
                    # The calibration has been taken recently or should not be repeated.
                    continue

            cal.active = True

            if cal.min_start_time is None and cal.time_mode in [
                "secs_after_sunset",
                "secs_before_sunrise",
                "jd",
                "utc",
            ]:
                self.log.warning(f"Calibration {cal.name} has no min_start_time.")
                cal.max_start_time = None
                continue

            if cal.max_start_time is None and cal.time_mode in ["jd", "utc"]:
                self.log.warning(f"Calibration {cal.name} has no max_start_time.")
                cal.min_start_time = None
                continue

            if cal.min_start_time is None and cal.max_start_time is None:
                # No time constraints.
                continue

            if cal.min_start_time is None or cal.max_start_time is None:
                raise ValueError(
                    f"Calibration {cal.name} must have both min_start_time "
                    "and max_start_time set or none."
                )

            if cal.time_mode == "jd" and cal.repeat != "never":
                self.log.warning(
                    f"Calibration {cal.name} must use "
                    "repeat=never with time_mode=jd."
                )
                cal.repeat = "never"

            if cal.time_mode == "secs_after_sunset":
                cal.min_start_time_schedule = sunset + cal.min_start_time / 86400
                cal.max_start_time_schedule = sunset + cal.max_start_time / 86400

            elif cal.time_mode == "secs_before_sunrise":
                cal.min_start_time_schedule = sunrise - cal.min_start_time / 86400
                cal.max_start_time_schedule = sunrise - cal.max_start_time / 86400

            elif cal.time_mode == "jd":
                cal.min_start_time_schedule = cal.min_start_time
                cal.max_start_time_schedule = cal.max_start_time

            elif cal.time_mode == "utc":
                if cal.max_start_time < cal.min_start_time:
                    cal.max_start_time += 24

                # SJD at the time of observation is always the following midnight.
                # In this case we want to refer times from the previous midnight.
                base_date = time.Time(self.sjd - 1, format="mjd")

                min_time_delta = time.TimeDelta(cal.min_start_time * 3600, format="sec")
                max_time_delta = time.TimeDelta(cal.max_start_time * 3600, format="sec")

                cal.min_start_time_schedule = (base_date + min_time_delta).jd
                cal.max_start_time_schedule = (base_date + max_time_delta).jd

            else:
                self.log.warning(
                    f"Calibration {cal.name} has invalid "
                    f"time_mode={cal.time_mode!r}."
                )
                cal.active = False
                continue

        # Step 2. Schedule calibration by priority order.
        # TODO: Right now this is done using a simple priority scheme. This can
        # probably be generalised to use something like
        # https://www.sciencedirect.com/science/article/pii/0020019094900434
        for cal in sorted(cals, key=lambda x: x.priority, reverse=True):
            try:
                (start_time, end_time) = await run_in_executor(self._schedule_one, cal)
            except ValueError:
                self.log.warning(f"Unable schedule calibration {cal.name!r}.")
            else:
                cal.start_time = start_time
                cal.end_time = end_time

    def get_start_end_times(self) -> ARRAY_F32:
        """Returns a numpy array with scheduled times for each calibration."""

        return numpy.array(
            [
                [cal.start_time, cal.end_time]
                for cal in list(self.calibrations.values())
                if cal.active and not cal.done and cal.start_time and cal.end_time
            ]
        )

    def _schedule_one(self, cal: Calibration):
        """Schedules a single calibration."""

        if cal.done or not cal.active:
            return

        scheduled = self.get_start_end_times()

        if cal.min_start_time_schedule is None and cal.max_start_time_schedule is None:
            if cal.night_mode:
                # Schedule for the entire night.
                cal.min_start_time_schedule = self.ephemeris["sunset"]
                cal.max_start_time_schedule = self.ephemeris["sunrise"]
            else:
                # Schedule from three hours before the sunset.
                cal.min_start_time_schedule = self.ephemeris["sunset"] - 3.0 / 24.0
                cal.max_start_time_schedule = self.ephemeris["sunset"]
        else:
            assert cal.min_start_time_schedule and cal.max_start_time_schedule

        start_time = 0.0
        end_time = 0.0

        delay = 0
        now = time.Time.now().jd
        TIME_STEP = 60  # 1 minute.
        while True:
            start_time = cal.min_start_time_schedule + delay / 86400
            end_time = start_time + cal.expected_duration / 86400

            if start_time < now:
                delay += TIME_STEP
                continue

            if start_time > cal.max_start_time_schedule:
                raise ValueError(f"Could not schedule calibration {cal.name!r}.")

            # Check if the calibration window overlaps with any other calibration.
            if len(scheduled) > 0:
                if (
                    ((start_time >= scheduled[:, 0]) & (start_time <= scheduled[:, 1]))
                    | ((end_time >= scheduled[:, 0]) & (end_time <= scheduled[:, 1]))
                ).any():
                    delay += TIME_STEP  # Blocks of 1 minute.
                    continue

            # The calibration window does not overlap with any other calibration.
            cal.start_time = start_time
            cal.end_time = end_time

            break

        return (start_time, end_time)

    def get_schedule(self):
        """Returns a dataframe with the schedule of calibrations."""

        df = polars.DataFrame(list(self.calibrations.values()))

        return df.select(
            [
                "name",
                "recipe",
                "script",
                "start_time",
                "end_time",
                "expected_duration",
                "min_start_time_schedule",
                "max_start_time_schedule",
                "open_dome",
                "night_mode",
                "priority",
                "done",
                "active",
            ]
        )

    async def get_next(self) -> tuple[Calibration | None, float | None]:
        """Gets the next calibration to observe and the time to it."""

        sch = self.get_schedule().sort("start_time")
        sch = sch.filter(polars.col("active") & ~polars.col("done"))

        now = time.Time.now().jd

        if len(sch) > 0:
            for cal in sch.rows(named=True):
                name = cal["name"]
                if cal["start_time"] < now:
                    max_time = cal["max_start_time_schedule"]
                    if max_time is None or now < cal["max_start_time_schedule"]:
                        self.log.warning(
                            f"Calibration {name!r} is late but can be observed."
                        )
                        return (self.calibrations[name], 0)
                    else:
                        self.log.warning(
                            f"Calibration {name!r} is late and cannot be observed."
                        )
                        self.calibrations[name].active = False
                        continue

                return (self.calibrations[name], (cal["start_time"] - now) * 86400)

        return (None, None)

    @classmethod
    def from_file(cls, filename: str | pathlib.Path | None = None, **kwargs):
        """Loads calibrations from a file."""

        etc_dir = pathlib.Path(__file__).parent / "../etc/"
        default_cals_file = etc_dir / "calibrations.yaml"

        cals_file = filename or default_cals_file

        cals_yml = read_yaml_file(cals_file)
        if "calibrations" not in cals_yml:
            raise ValueError("Calibrations file is badly formatted.")

        cals_data: list[dict] = list(cals_yml["calibrations"])

        json_schema_file = etc_dir / "calibrations_schema.json"
        json_schema = json.loads(open(json_schema_file).read())
        validator = jsonschema.Draft7Validator(json_schema)

        try:
            validator.validate(cals_data)
        except jsonschema.ValidationError:
            raise ValueError("Calibrations file is badly formatted.")

        return cls({cal["name"]: Calibration(**cal) for cal in cals_data}, **kwargs)


class CalibrationsMonitor(OverwatcherModuleTask["CalibrationsOverwatcher"]):
    """Monitors the calibrations schedule."""

    name = "calibrations_monitor"
    keep_alive = True
    restart_on_error = True

    async def task(self):
        """Runs the calibration monitor."""

        open_dome_buffer = self.config["overwatcher"]["scheduler"]["open_dome_buffer"]

        # Small delay to make sure the schedule has been updated.
        await asyncio.sleep(10)

        last_update: float = 0

        while True:
            if last_update > 3600:
                await self.module.schedule.update_schedule()
                last_update = 0

            next_cal, time_to_cal = await self.module.schedule.get_next()
            if next_cal is not None:
                if (open_dome := next_cal.open_dome) is not None:
                    is_open = await self.gort.enclosure.is_open()
                    if (open_dome and not is_open) or (not open_dome and is_open):
                        time_to_cal -= open_dome_buffer

            if next_cal is not None and time_to_cal is not None and time_to_cal < 60:
                asyncio.create_task(self.module.run_calibration(next_cal))

            await asyncio.sleep(60)
            last_update += 60


class CalibrationsOverwatcher(OverwatcherModule):
    name = "calibration"

    tasks = []

    def __init__(
        self,
        overwatcher: Overwatcher,
        calibrations_file: str | pathlib.Path | None = None,
    ):
        super().__init__(overwatcher)

        self.calibrations_file: str | pathlib.Path | None = calibrations_file
        self.schedule = CalibrationSchedule.from_file(self.calibrations_file)

        self.ephemeris: dict[str, Any] | None = None
        self.sjd: int = get_sjd("LCO")

        self.calibration_runnning: bool = False

    async def reset(self):
        """Resets the list of calibrations for a new SJD.

        This method is usually called by the ephemeris overwatcher when a new SJD
        is detected.

        """

        while self.overwatcher.ephemeris.ephemeris is None:
            self.log.error("Ephemeris is not available. Cannot reset calibrations.")
            await asyncio.sleep(5)

        self.sjd = self.overwatcher.ephemeris.sjd
        self.ephemeris = self.overwatcher.ephemeris.ephemeris

        self.schedule = CalibrationSchedule.from_file(
            self.calibrations_file,
            sjd=self.sjd,
            ephemeris=self.ephemeris,
        )
        await self.schedule.update_schedule()

    async def run_calibration(self, calibration: Calibration):
        """Runs a calibration."""

        self.calibration_runnning = True

        self.log.info(f"Running calibration {calibration.name!r}.")
