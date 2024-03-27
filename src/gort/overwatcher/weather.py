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

from typing import TYPE_CHECKING, Coroutine, cast

import polars

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
    rain_intensity_10: float
    station: str


class WeatherOverwatcher(OverwatcherModule):
    """Monitors weather conditions."""

    name = "weather"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.data: polars.DataFrame | None = None
        self.last_updated: float = 0.0

        self.unavailable: bool = False

        self.state: WeatherState | None = None
        self.risk = WeatherRisk.UNKNOWN

    def list_task_coros(self) -> list[Coroutine]:
        """Returns a list of coroutines to schedule as tasks."""

        return [self.monitor_weather()]

    async def monitor_weather(self):
        """Updates the weather data."""

        n_failures: int = 0

        while True:
            try:
                self.log("Checking weather conditions.")
                await self.update_weather()
                await self.check_weather()
            except Exception as err:
                if self.unavailable is False:
                    self.log(f"Failed to get weather data: {err!r}", "error")
                n_failures += 1
            else:
                self.last_updated = time()
                self.unavailable = False
                n_failures = 0
            finally:
                if self.unavailable is False and n_failures >= 5:
                    self.unavailable = True
                    self.log(
                        "Failed to get weather data 5 times. "
                        "Triggering an emergency shutdown.",
                        "critical",
                    )
                    await self.overwatcher.emergency_shutdown(block=False)

            await asyncio.sleep(60)

    async def update_weather(self):
        """Processes the weather update and determines whether it is safe to observe."""

        self.data = await self.get_weather_report()

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
            rain_intensity_10=cast(float, data_10["rain_intensity"].max()),
            station=self.data["station"][0],
        )

        # Determine the risk level.
        new_risk = WeatherRisk.SAFE

        if self.state.temperature_10 < -10:
            new_risk = WeatherRisk.EXTREME

        if self.state.wind_speed_10 > 50 or self.state.wind_speed_30 > 40:
            new_risk = WeatherRisk.DANGER

        if self.state.rain_intensity_10 > 0:
            new_risk = WeatherRisk.EXTREME

        if self.state.rh_10 > 80:
            new_risk = WeatherRisk.DANGER

        self.risk = new_risk

    async def check_weather(self):
        """Checks the weather state and triggers an emergency shutdown if necessary."""

        ecp_status = await self.gort.enclosure.status()
        dome_labels = ecp_status["dome_status_labels"]
        if "CLOSED" in dome_labels or "MOTOR_CLOSING" in dome_labels:
            return

        if not self.can_open():
            self.log(
                "Unsafe weather conditions. Triggering emergency shutdown.",
                "critical",
            )
            await self.overwatcher.emergency_shutdown(block=False)

    def can_open(self):
        """Determines whether it is possible to open."""

        if self.state in [WeatherRisk.DANGER, WeatherRisk.EXTREME]:
            return False

        return True

    @staticmethod
    async def get_weather_report(delta_time=3600) -> polars.DataFrame:
        """Returns a weather report."""

        data = await get_lvmapi_route("/weather", delta_time=delta_time)

        df = polars.DataFrame(data)
        df = df.with_columns(
            ts=polars.col("ts").str.to_datetime(
                time_unit="ms",
                time_zone="UTC",
            )
        )

        return df.sort("ts")
