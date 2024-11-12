#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-11-11
# @Filename: transparency.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import enum
from time import time

from typing import TYPE_CHECKING, TypedDict

import numpy
import polars

from gort.overwatcher.core import OverwatcherModuleTask
from gort.overwatcher.overwatcher import OverwatcherModule
from gort.tools import get_lvmapi_route


if TYPE_CHECKING:
    pass


__all__ = ["TransparencyOverwatcher", "TransparencyStatus"]


class TransparencyStatus(enum.Flag):
    """Flags for transparency status."""

    GOOD = enum.auto()
    POOR = enum.auto()
    BAD = enum.auto()
    IMPROVING = enum.auto()
    WORSENING = enum.auto()
    UNKNOWN = enum.auto()


class TransparencyStatusDict(TypedDict):
    sci: TransparencyStatus
    skye: TransparencyStatus
    skyw: TransparencyStatus
    spec: TransparencyStatus


class TransparencyValuesDict(TypedDict):
    sci: float
    skye: float
    skyw: float
    spec: float


class TransparencyMonitorTask(OverwatcherModuleTask["TransparencyOverwatcher"]):
    """Monitors transparency."""

    name = "transparency_monitor"
    keep_alive = True
    restart_on_error = True

    def __init__(self):
        super().__init__()

        self.last_updated: float = 0
        self.unavailable: bool = False

    async def task(self):
        """Updates the transparency data."""

        n_failures: int = 0

        while True:
            try:
                await self.update_data()
            except Exception as err:
                if not self.unavailable:
                    self.log.error(f"Failed to get transparency data: {err!r}")
                n_failures += 1
            else:
                self.last_updated = time()
                self.unavailable = False
                n_failures = 0
            finally:
                if n_failures >= 5 and not self.unavailable:
                    await self.notify(
                        "Cannot retrieve transparency data. Will continue trying but "
                        "transparency monitoring will be unavailable.",
                        level="error",
                    )

                    self.module.reset()
                    self.unavailable = True

            await asyncio.sleep(30)

    async def update_data(self):
        """Retrieves and evaluates transparency data."""

        # Get transparency data from the API for the last hour.
        data = await get_lvmapi_route("/transparency/")

        self.module.data_start_time = data["start_time"]
        self.module.data_end_time = data["end_time"]

        self.module.data = (
            polars.DataFrame(
                data["data"],
                orient="row",
                schema={
                    "time": polars.String(),
                    "zero_point": polars.Float32(),
                    "telescope": polars.String(),
                },
            )
            .with_columns(
                time=polars.col.time.str.to_datetime(time_zone="UTC", time_unit="ms")
            )
            .sort("time")
        )

        # TODO: actually set the status and values based on some average from the data.


class TransparencyOverwatcher(OverwatcherModule):
    """Monitors alerts."""

    name = "alerts"

    tasks = [TransparencyMonitorTask()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.data_start_time: float = 0
        self.data_end_time: float = 0

        self.state = TransparencyStatusDict
        self.values = TransparencyValuesDict
        self.data: polars.DataFrame

        self.reset()

    def reset(self):
        """Reset values."""

        self.data_start_time: float = 0
        self.data_end_time: float = 0

        self.state = TransparencyStatusDict(
            sci=TransparencyStatus.UNKNOWN,
            skye=TransparencyStatus.UNKNOWN,
            skyw=TransparencyStatus.UNKNOWN,
            spec=TransparencyStatus.UNKNOWN,
        )

        self.values = TransparencyValuesDict(
            sci=numpy.nan,
            skye=numpy.nan,
            skyw=numpy.nan,
            spec=numpy.nan,
        )

        self.data: polars.DataFrame = polars.DataFrame(
            None,
            schema={
                "time": polars.Datetime(time_unit="ms", time_zone="UTC"),
                "zero_point": polars.Float32(),
                "telescope": polars.String(),
            },
        )
