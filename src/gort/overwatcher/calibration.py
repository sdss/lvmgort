#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-27
# @Filename: calibration.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import json
import pathlib

from typing import TYPE_CHECKING, Coroutine

import jsonschema
import polars

from sdsstools import read_yaml_file


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


__all__ = ["CalibrationsHandler"]

SCHEMA = {
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
}


class CalibrationsHandler:

    name = "calibration"

    def __init__(
        self,
        overwatcher: Overwatcher,
        calibrations_file: str | pathlib.Path | None = None,
    ):

        self.overwatcher = overwatcher

        self.calibrations_file: str | pathlib.Path | None = calibrations_file
        self.calibrations: polars.DataFrame = polars.DataFrame(None, schema=SCHEMA)

        self.reset()

    def list_task_coros(self) -> list[Coroutine]:
        """Returns a list of coroutines to schedule as tasks."""

        return []

    def reset(self):
        """Resets the list of calibrations for a new SJD."""

        calibrations = self.load_calibrations()

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

        self.calibrations = polars.DataFrame(list(cals_yml), schema=SCHEMA)

    # def generate_schedule(self):
    #     """Generates a schedule of calibrations for the night."""

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
