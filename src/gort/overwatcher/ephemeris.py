#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: ephemeris.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import pathlib
from time import time

from astropy.time import Time
from pydantic import BaseModel

from sdsstools import get_sjd

from gort.overwatcher import OverwatcherModule
from gort.overwatcher.core import OverwatcherModuleTask
from gort.tools import get_ephemeris_summary


__all__ = ["EphemerisOverwatcher"]


class EphemerisModel(BaseModel):
    SJD: int
    request_jd: float
    date: str
    sunset: float
    twilight_end: float
    twilight_start: float
    sunrise: float
    is_night: bool
    is_twilight: bool
    time_to_sunset: float
    time_to_sunrise: float
    moon_illumination: float
    from_file: bool


class EphemerisMonitorTask(OverwatcherModuleTask["EphemerisOverwatcher"]):
    """Monitors the ephemeris."""

    name = "ephemeris_monitor"
    keep_alive = True
    restart_on_error = True

    async def task(self):
        """Monitors SJD change and keeps ephemeris updated."""

        failing: bool = False

        while True:
            sjd = get_sjd("LCO")

            if self.module.ephemeris is None or failing or sjd != self.module.sjd:
                try:
                    # First, roll over the GORT log to the new SJD file.
                    log = self.gort.log
                    if log.fh and log.log_filename:
                        path = pathlib.Path(log.log_filename)
                        if str(sjd) not in path.name:
                            log.fh.flush()
                            log.fh.close()
                            log.removeHandler(log.fh)
                            log.start_file_logger(str(path.parent / f"{sjd}.log"))

                    await self.module.update_ephemeris(sjd)

                except Exception as err:
                    await self.notify(
                        f"Failed getting ephemeris data for {sjd}: {err!r}",
                        level="error",
                    )
                    failing = True

                else:
                    self.log.info(f"New SJD: updating ephemeris for {sjd}.")
                    self.module.sjd = sjd

                    await self.overwatcher.calibrations.reset()

                    failing = False

            await asyncio.sleep(60)


class EphemerisOverwatcher(OverwatcherModule):
    """Monitors ephemeris data."""

    name = "ephemeris"

    tasks = [EphemerisMonitorTask()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.sjd = get_sjd("LCO")
        self.ephemeris: EphemerisModel | None = None
        self.last_updated: float = 0.0

    async def update_ephemeris(self, sjd: int | None = None):
        """Updates the ephemeris data."""

        sjd = sjd or get_sjd("LCO")

        ephemeris_response = await get_ephemeris_summary(sjd)
        self.last_updated = time()

        self.ephemeris = EphemerisModel(**ephemeris_response)

        return self.ephemeris

    def is_night(self):
        """Determines whether it is nightime."""

        ephemeris = self.ephemeris
        now_jd = float(Time.now().jd)

        if not ephemeris:
            self.log.warning("Ephemeris data not available. is_night() returns False.")
            return False

        return float(ephemeris.twilight_end) < now_jd < float(ephemeris.twilight_start)

    def time_to_morning_twilight(self):
        """Returns the time to morning twilight in seconds."""

        ephemeris = self.ephemeris
        if not ephemeris:
            return None

        now = time()
        twilight_time = Time(ephemeris.twilight_start, format="jd").unix

        return round(twilight_time - now, 2)
