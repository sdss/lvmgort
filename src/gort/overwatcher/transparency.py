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

from typing import TYPE_CHECKING, Literal, Sequence, TypedDict, cast, get_args

import numpy
import polars

from sdsstools.utils import GatheringTaskGroup

from gort.exceptions import GortError
from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.tools import cancel_task, decap, get_lvmapi_route


if TYPE_CHECKING:
    pass


__all__ = ["TransparencyOverwatcher", "TransparencyQuality", "TransparencyQuality"]


class TransparencyQuality(enum.Flag):
    """Flags for transparency status."""

    GOOD = enum.auto()
    POOR = enum.auto()
    BAD = enum.auto()
    UNKNOWN = enum.auto()
    IMPROVING = enum.auto()
    WORSENING = enum.auto()
    FLAT = enum.auto()


Telescopes = Literal["sci", "spec", "skye", "skyw"]


class TransparencyQualityDict(TypedDict):
    sci: TransparencyQuality
    skye: TransparencyQuality
    skyw: TransparencyQuality
    spec: TransparencyQuality


class TransparencyZPDict(TypedDict):
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

        self.unavailable: bool = False

    async def task(self):
        """Updates the transparency data."""

        n_failures: int = 0

        while True:
            try:
                await self.update_data()
            except Exception as err:
                if not self.unavailable:
                    self.log.error(f"Failed to get transparency data: {decap(err)}")
                n_failures += 1
            else:
                self.module.last_updated = time()
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

            await asyncio.sleep(60)

    async def update_data(self):
        """Retrieves and evaluates transparency data."""

        now: float = time()
        lookback: float = 3600

        # Get transparency data from the API for the last hour.
        data = await get_lvmapi_route(
            "/transparency",
            params={"start_time": now - lookback, "end_time": now},
        )

        self.module.data_start_time = data["start_time"]
        self.module.data_end_time = data["end_time"]

        data = (
            polars.DataFrame(
                data["data"],
                orient="row",
                schema={
                    "date": polars.String(),
                    "timestamp": polars.Float64(),
                    "telescope": polars.String(),
                    "zero_point": polars.Float32(),
                },
            )
            .with_columns(
                date=polars.col.date.str.to_datetime(time_zone="UTC", time_unit="ms")
            )
            .sort("telescope", "date")
        )

        # Add a rolling mean.
        data = data.with_columns(
            zero_point_10m=polars.col.zero_point.rolling_mean_by(
                by="date",
                window_size="10m",
            ).over("telescope")
        )

        # Get last 5 and 15 minutes of data.
        data_10 = data.filter(polars.col.timestamp > (now - 300))
        data_15 = data.filter(polars.col.timestamp > (now - 900))

        # Use the last 5 minutes of data to determine the transparency status
        # and value the last 15 minutes to estimate the trend.
        for tel in ["sci", "spec", "skye", "skyw"]:
            data_tel_10 = data_10.filter(polars.col.telescope == tel)
            data_tel_15 = data_15.filter(polars.col.telescope == tel)

            if len(data_tel_10) < 5:
                self.module.quality[tel] = TransparencyQuality.UNKNOWN
                self.module.zero_point[tel] = numpy.nan
                continue

            avg_10 = data_tel_10["zero_point_10m"].median()
            if avg_10 is not None:
                avg_10 = cast(float, avg_10)
                self.module.zero_point[tel] = round(float(avg_10), 2)

                if avg_10 < -22.75:
                    self.module.quality[tel] = TransparencyQuality.GOOD
                elif avg_10 > -22.75 and avg_10 < -22.25:
                    self.module.quality[tel] = TransparencyQuality.POOR
                else:
                    self.module.quality[tel] = TransparencyQuality.BAD

            time_15m = data_tel_15["timestamp"].to_numpy() - data_tel_15["timestamp"][0]
            zp_15m = data_tel_15["zero_point_10m"].to_numpy()
            gradient_15m = (zp_15m[-1] - zp_15m[0]) / (time_15m[-1] - time_15m[0])

            if gradient_15m > 5e-4:
                self.module.quality[tel] |= TransparencyQuality.WORSENING
            elif gradient_15m < -5e-4:
                self.module.quality[tel] |= TransparencyQuality.IMPROVING
            else:
                self.module.quality[tel] |= TransparencyQuality.FLAT

        return data


class TransparencyOverwatcher(OverwatcherModule):
    """Monitors alerts."""

    name = "alerts"

    tasks = [TransparencyMonitorTask()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.last_updated: float = 0

        self.data_start_time: float = 0
        self.data_end_time: float = 0

        self._monitor_task: asyncio.Task | None = None

        self.reset()

    def reset(self):
        """Reset values."""

        self.data_start_time: float = 0
        self.data_end_time: float = 0

        self.quality = TransparencyQualityDict(
            sci=TransparencyQuality.UNKNOWN,
            skye=TransparencyQuality.UNKNOWN,
            skyw=TransparencyQuality.UNKNOWN,
            spec=TransparencyQuality.UNKNOWN,
        )

        self.zero_point = TransparencyZPDict(
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

    def write_to_log(
        self,
        telescopes: Sequence[Telescopes] | Telescopes = get_args(Telescopes),
    ):
        """Writes the current state to the log."""

        if isinstance(telescopes, str):
            telescopes = [telescopes]

        for tel in telescopes:
            self.log.info(
                f"Transparency for {tel}: quality={self.get_quality_string(tel)}; "
                f"trend={self.get_trend_string(tel)}; zp={self.zero_point[tel]:.2f}"
            )

    def get_quality_string(self, telescope: Telescopes) -> str:
        """Returns the quality as a string."""

        quality_flag = self.quality[telescope]
        quality: str = "UNKNOWN"

        if quality_flag & TransparencyQuality.BAD:
            quality = "BAD"
        elif quality_flag & TransparencyQuality.POOR:
            quality = "POOR"
        elif quality_flag & TransparencyQuality.GOOD:
            quality = "GOOD"

        return quality

    def get_trend_string(self, telescope: Telescopes) -> str:
        """Returns the trend as a string."""

        quality_flag = self.quality[telescope]
        trend: str = "UNKNOWN"

        if quality_flag & TransparencyQuality.IMPROVING:
            trend = "IMPROVING"
        elif quality_flag & TransparencyQuality.WORSENING:
            trend = "WORSENING"
        elif quality_flag & TransparencyQuality.FLAT:
            trend = "FLAT"

        return trend

    def is_monitoring(self):
        """Returns True if the transparency monitor is running."""

        return self._monitor_task is not None and not self._monitor_task.done()

    async def start_monitoring(self):
        """Starts monitoring transparency."""

        if self.is_monitoring():
            return

        self.gort.log.info("Starting transparency monitor.")
        self._monitor_task = asyncio.create_task(self._monitor_transparency())

    async def stop_monitoring(self):
        """Stops monitoring transparency."""

        if self.is_monitoring():
            self.gort.log.info("Stopping transparency monitor.")
            self._monitor_task = await cancel_task(self._monitor_task)
            await self.gort.guiders.stop()

    async def _monitor_transparency(self):
        """Monitors transparency."""

        # Stop guiding and fold all telescopes except sci.
        await self.gort.guiders.stop()
        async with GatheringTaskGroup() as group:
            for tel in ["spec", "skye", "skyw"]:
                group.create_task(
                    self.gort.telescopes[tel].park(
                        disable=False,
                        kmirror=False,
                    )
                )

        # Start monitoring with the sci telescope.
        sci_task = asyncio.create_task(self.gort.guiders["sci"].monitor(sleep=30))

        while True:
            await asyncio.sleep(30)

            if sci_task.done():
                self.gort.log.error("sci guider has stopped monitoring transparency.")
                await self.stop_monitoring()
                raise GortError("sci guider has stopped monitoring transparency.")

            sci_quality = self.quality["sci"]
            sci_zero_point = self.zero_point["sci"]

            if sci_quality & TransparencyQuality.GOOD:
                self.gort.log.info("sci guider has detected good transparency.")
                await cancel_task(sci_task)
                await self.stop_monitoring()
                break

            self.log.info(
                f"sci guider transparency quality: {sci_quality.name} "
                f"(zero_point={sci_zero_point:.2f})."
            )
