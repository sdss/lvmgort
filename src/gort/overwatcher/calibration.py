#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-27
# @Filename: calibration.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import Coroutine

from gort.overwatcher import OverwatcherModule


__all__ = ["CalibrationOverwatcher"]


class CalibrationOverwatcher(OverwatcherModule):

    name = "calibration"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def list_task_coros(self) -> list[Coroutine]:
        """Returns a list of coroutines to schedule as tasks."""

        return []

    def time_to_calibrations(self):
        """ "Returns the number of minutes to the next calibration window."""

        eph = self.overwatcher.ephemeris.ephemeris
        if eph is None:
            return None

        time_to_sunset = eph["time_to_sunset"]
        time_to_sunrise = eph["time_to_sunrise"]

        if time_to_sunset < 0:
            return
        else:
            is_sunset = False
