#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: weather.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import enum
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import time

from typing import TYPE_CHECKING, cast

import polars

from gort.overwatcher.core import OverwatcherModuleTask
from gort.overwatcher.overwatcher import OverwatcherModule
from gort.tools import get_lvmapi_route


if TYPE_CHECKING:
    pass


__all__ = ["WeatherOverwatcher"]


class WeatherRisk(enum.Enum):
    """An enumeration of weather risk levels."""

    SAFE = enum.auto()
    CAUTION = enum.auto()
    DANGER = enum.auto()
    EXTREME = enum.auto()
    UNKNOWN = enum.auto()


@dataclass
class WeatherState:
    """A dataclass to store the weather state."""

    temperature_10: float
    wind_speed_10: float
    wind_speed_30: float
    wind_speed_10: float
    wind_speed_30: float
    gust_10: float
    rh_10: float
    rh_30: float
    lvm_rain_sensor_alarm: bool
    rain_intensity_10: float
    station: str


class WeatherMonitorTask(OverwatcherModuleTask["WeatherOverwatcher"]):
    """Monitors the weather state."""

    name = "weather_monitor"
    keep_alive = True
    restart_on_error = True

    async def task(self):
        """Updates the weather data."""

        n_failures: int = 0

        while True:
            try:
                await self.update_weather()
                await self.module.check_weather()
            except Exception as err:
                if self.unavailable is False:
                    self.log.error(f"Failed to get weather data: {err!r}")
                n_failures += 1
            else:
                self.last_updated = time()
                self.unavailable = False
                n_failures = 0
            finally:
                if self.unavailable is False and n_failures >= 5:
                    self.unavailable = True
                    self.log.critical(
                        "Failed to get weather data 5 times. "
                        "Triggering an emergency shutdown.",
                    )
                    asyncio.create_task(
                        self.overwatcher.shutdown(
                            reason="weather data unavailable",
                            park=False,
                        )
                    )

            await asyncio.sleep(60)

    async def update_weather(self):
        """Processes the weather update and determines whether it is safe to observe."""

        self.data = await self.module.get_weather_report()
        is_raining = await self.module.is_raining()

        if self.data is None or len(self.data) == 0:
            raise ValueError("No weather data available.")

        # Check that the data is recent.
        now = datetime.now(timezone.utc)
        last_point = cast(datetime, self.data["ts"].max())
        last_point_delay = (now - last_point).total_seconds()

        if last_point_delay > 300:
            raise ValueError("Weather data is too old.")

        # Data for the last 10 and 30 minutes.
        now_10 = now - timedelta(minutes=10)
        now_30 = now - timedelta(minutes=30)
        data_10 = self.data.filter(self.data["ts"] > now_10)
        data_30 = self.data.filter(self.data["ts"] > now_30)

        avg_10 = data_10.select(polars.col(polars.Float64)).mean()
        avg_30 = data_30.select(polars.col(polars.Float64)).mean()

        self.state = WeatherState(
            temperature_10=avg_10["temperature"][0],
            wind_speed_10=avg_10["wind_speed_avg"][0],
            wind_speed_30=avg_30["wind_speed_avg"][0],
            gust_10=cast(float, data_10["wind_speed_max"].max()),
            rh_10=avg_10["relative_humidity"][0],
            rh_30=avg_30["relative_humidity"][0],
            lvm_rain_sensor_alarm=is_raining,
            rain_intensity_10=cast(float, data_10["rain_intensity"].max()),
            station=self.data["station"][0],
        )

        # Determine the risk level.
        new_risk = WeatherRisk.SAFE

        if self.state.temperature_10 < -10:
            new_risk = WeatherRisk.EXTREME

        if self.state.wind_speed_10 > 50 or self.state.wind_speed_30 > 40:
            new_risk = WeatherRisk.DANGER

        if self.state.lvm_rain_sensor_alarm:
            new_risk = WeatherRisk.EXTREME

        if self.state.rh_10 > 80:
            new_risk = WeatherRisk.DANGER

        self.module.risk = new_risk


class WeatherOverwatcher(OverwatcherModule):
    """Monitors weather conditions."""

    name = "weather"

    tasks = [WeatherMonitorTask()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.data: polars.DataFrame | None = None
        self.last_updated: float = 0.0

        self.unavailable: bool = False

        self.state: WeatherState | None = None
        self.risk = WeatherRisk.UNKNOWN

    async def check_weather(self):
        """Checks the weather state and triggers an emergency shutdown if necessary."""

        if await self.overwatcher.dome.is_closing():
            return

        if not self.is_safe():
            await self.overwatcher.shutdown(
                reason="unsafe weather conditions",
                park=True,
            )

    def is_safe(self):
        """Determines whether it is safe to open."""

        if self.risk in [WeatherRisk.DANGER, WeatherRisk.EXTREME, WeatherRisk.UNKNOWN]:
            return False

        return True

    @staticmethod
    async def get_weather_report(delta_time=3600) -> polars.DataFrame:
        """Returns a weather report."""

        data = await get_lvmapi_route("/weather/report", delta_time=delta_time)

        df = polars.DataFrame(data)
        df = df.with_columns(
            ts=polars.col("ts").str.to_datetime(
                time_unit="ms",
                time_zone="UTC",
            )
        )

        return df.sort("ts")

    @staticmethod
    async def is_raining():
        """Determines whether it is raining according to the LVM rain sensor."""

        data = await get_lvmapi_route("/enclosure")

        return "RAIN_SENSOR_ALARM" in data["safety_status"]["labels"]
