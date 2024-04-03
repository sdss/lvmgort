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
import pathlib

from typing import TYPE_CHECKING, Any

import jsonschema
import polars

from sdsstools import get_sjd, read_yaml_file

from gort.overwatcher.core import OverwatcherModule
from gort.tools import get_redis_client


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


__all__ = ["CalibrationsWatcher"]


SCHEMA = {
    "sjd": polars.Int32(),
    "name": polars.String(),
    "recipe": polars.String(),
    "script": polars.String(),
    "min_start_time": polars.Float32(),
    "max_start_time": polars.Float32(),
    "time_mode": polars.String(),
    "expected_duration": polars.Float32(),
    "required": polars.Boolean(),
    "open_dome": polars.Boolean(),
    "night_mode": polars.Boolean(),
    "priority": polars.Float32(),
    "start_jd": polars.Float32(),
    "end_jd": polars.Float32(),
    "done": polars.Boolean(),
}


class CalibrationsWatcher(OverwatcherModule):

    name = "calibration"
    tasks = []

    def __init__(
        self,
        overwatcher: Overwatcher,
        calibrations_file: str | pathlib.Path | None = None,
    ):

        super().__init__(overwatcher)

        self.calibrations_file: str | pathlib.Path | None = calibrations_file
        self.calibrations: polars.DataFrame = polars.DataFrame(None, schema=SCHEMA)

        self.ephemeris: dict[str, Any] | None = None
        self.sjd: int = get_sjd("LCO")

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

        self.load_calibrations()

    def load_calibrations(self):
        """Loads and validaes the calibrations file."""

        etc_dir = pathlib.Path(__file__).parent / "../etc/"
        default_cals_file = etc_dir / "calibrations.yaml"
        cals_file = self.calibrations_file or default_cals_file

        cals_yml = read_yaml_file(cals_file)
        if "calibrations" in cals_yml:
            cals_yml = cals_yml["calibrations"]

        json_schema_file = etc_dir / "calibrations_schema.json"
        json_schema = json.loads(open(json_schema_file).read())
        validator = jsonschema.Draft7Validator(json_schema)

        try:
            validator.validate(cals_yml)
        except jsonschema.ValidationError:
            raise ValueError("Calibrations file is badly formatted.")

        cals = polars.DataFrame(list(cals_yml), schema=SCHEMA)
        cals = cals.with_columns(sjd=polars.lit(self.sjd, SCHEMA["sjd"]))

        self.calibrations = cals

    def generate_schedule(self):
        """Generates a schedule of calibrations for the night."""

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

    async def get_from_redis(self):
        """Gets the status of the calibrations."""

        redis = get_redis_client()

        data = await redis.hgetall(f"overwatcher:calibrations:status:{self.sjd}")
        if data is None:
            await self.write_to_redis()
            return self.get_from_redis()

        for key, value in data.items():
            data[key] = bool(int(value))
        return data

    async def write_to_redis(self):
        """Writes the status of the calibrations to Redis."""

        redis = get_redis_client()

        data = {}
        for row in self.calibrations.rows(named=True):
            print(row)
            data[row["name"]] = str(int(row["done"] or False))

        await redis.hset(f"overwatcher:calibrations:status:{self.sjd}", mapping=data)
