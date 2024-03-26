#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: weather.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import polars

from gort.overwatcher.overwatcher import OverwatcherModule


if TYPE_CHECKING:
    pass


__all__ = ["WeatherOverwatcher"]


class WeatherOverwatcher(OverwatcherModule):
    """Monitors weather conditions."""

    name = "weather"

    async def run(self):
        """Starts the weather monitor."""

        print("here")

    @staticmethod
    async def get_weather_report(delta_time=600) -> polars.DataFrame:
        """Returns a weather report."""

        async with httpx.AsyncClient(
            base_url="http://localhost:8085",
            follow_redirects=True,
        ) as client:
            response = await client.get("/weather", params={"delta_time": delta_time})

            if response.status_code != 200:
                raise ValueError("Failed to get weather report.")

        data = response.json()

        df = polars.DataFrame(data)
        df = df.with_columns(ts=polars.col("ts").str.to_datetime(time_unit="ms"))

        return df.sort("ts")
