#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: ephemeris.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from time import time

from astropy.time import Time

from sdsstools import get_sjd

from gort.overwatcher import OverwatcherModule
from gort.overwatcher.core import OverwatcherModuleTask
from gort.tools import get_ephemeris_summary


__all__ = ["EphemerisOverwatcher"]


class EphemerisMonitorTask(OverwatcherModuleTask["EphemerisOverwatcher"]):
    """Monitors the ephemeris."""

    name = "ephemeris_monitor"
    keep_alive = True
    restart_on_error = True

    async def task(self):
        """Monitors SJD change and keeps ephemeris updated."""

        while True:
            self.log.debug("Updating LCO ephemeris.")

            new_sjd = get_sjd("LCO")

            if self.module.ephemeris is None or new_sjd != self.sjd:
                self.sjd = new_sjd
                self.overwatcher.calibrations.reset()

                try:
                    self.module.ephemeris = await get_ephemeris_summary(new_sjd)
                except Exception as err:
                    self.log.error(f"Failed getting ephemeris data: {err!r}")
                    await asyncio.sleep(10)
                    continue
                else:
                    self.last_updated = time()

            await asyncio.sleep(600)


class EphemerisOverwatcher(OverwatcherModule):
    """Monitors ephemeris data."""

    name = "ephemeris"

    tasks = [EphemerisMonitorTask()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.sjd = get_sjd("LCO")
        self.ephemeris: dict | None = None
        self.last_updated: float = 0.0

    def is_night(self, require_twilight: bool = True):
        """Determines whether it is nightime."""

        ephemeris = self.ephemeris
        now_jd = Time.now().jd

        if not ephemeris:
            return False

        between_twl = ephemeris["twilight_end"] < now_jd < ephemeris["twilight_start"]

        if ephemeris["is_night"]:
            if require_twilight and between_twl:
                return True
            elif not require_twilight:
                return True

        return False
