#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-08-13
# @Filename: calibration.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import json
import pathlib
import random
from copy import deepcopy

from typing import Any

import jsonschema
from astropy.time import Time
from pyds9 import numpy

from gort.exceptions import ErrorCodes, GortSpecError
from gort.exposure import Exposure
from gort.tools import cancel_task, get_ephemeris_summary, move_mask_interval

from .base import BaseRecipe


__all__ = ["CalibrationRecipe", "QuickCals", "BiasSequence"]


class CalibrationRecipe(BaseRecipe):
    """Runs a calibration sequence."""

    name = "calibration"

    async def recipe(
        self,
        sequence: str | dict = "normal",
        slew_telescopes: bool = True,
        park_after: bool = False,
        show_progress: bool | None = None,
    ):
        """Runs the calibration sequence.

        Parameters
        ----------
        sequence
            The name calibration sequence to execute. It can also be a
            dictionary with the calibration sequence definition that
            follows the :ref:`calibration schema <calibration-schema>`.
        slew_telescopes
            Whether to move the telescopes to point to the FF screen.
        park_after
            Park the telescopes after a successful calibration sequence.
        show_progress
            Displays a progress bar with the elapsed exposure time.

        """

        # TODO: add some checks. Confirm HDs are open, enclosure is closed,
        # specs connected, etc.

        # Calibration sequence configuration. Includes the position where to
        # point the telescopes, NPS to use, and sequences.
        cal_config = self.gort.config["recipes"]["calibration"]

        # Task that will move the fibre selector.
        fibsel_task: asyncio.Task | None = None

        sequence_config: dict[str, Any]
        if isinstance(sequence, dict):
            sequence_config = sequence

        else:
            if sequence not in cal_config["sequences"]:
                raise GortSpecError(
                    f"Unknown sequence {sequence!r}.",
                    error_code=ErrorCodes.INVALID_CALIBRATION_SEQUENCE,
                )
            sequence_config = cal_config["sequences"][sequence]

        # Validate sequence.
        schema_file = pathlib.Path(__file__).parent / "../etc/calibration_schema.json"
        schema = json.loads(open(schema_file).read())
        try:
            jsonschema.validate(sequence_config, schema)
        except jsonschema.ValidationError:
            raise GortSpecError(
                "Calibration sequence does not match schema.",
                error_code=ErrorCodes.INVALID_CALIBRATION_SEQUENCE,
            )

        calib_nps = self.gort.nps[cal_config["lamps_nps"]]

        lamps_config = sequence_config.get("lamps", {})
        has_lamps = len(lamps_config) != 0
        if not has_lamps:
            # No point in slewing if we are only taking bias and darks.
            slew_telescopes = False

        # Turn off all lamps.
        self.gort.log.info("Checking that all lamps are off.")
        await calib_nps.all_off()

        self.gort.log.info(f"Running calibration sequence {sequence!r}.")

        try:
            if "biases" in sequence_config:
                nbias = sequence_config["biases"].get("count", 1)
                self.gort.log.info(f"Taking {nbias} biases.")
                for _ in range(nbias):
                    await self.gort.specs.expose(flavour="bias", object="bias")

            if "darks" in sequence_config:
                ndarks = sequence_config["darks"].get("count", 1)
                exp_times = sequence_config["darks"]["exposure_time"]
                if isinstance(exp_times, (float, int)):
                    exp_times = [exp_times]

                self.gort.log.info(f"Taking {ndarks} x {exp_times} darks.")

                total_darks = len(exp_times) * ndarks
                idark = 1
                for exp_time in exp_times:
                    for _ in range(ndarks):
                        await self.gort.specs.expose(
                            flavour="dark",
                            exposure_time=exp_time,
                            async_readout=(idark == total_darks) and has_lamps,
                            object="dark",
                        )
                        idark += 1

            if slew_telescopes:
                # Move the telescopes to point to the screen.
                self.gort.log.info("Pointing telescopes to FF screen.")
                await self.gort.telescopes.goto_named_position(cal_config["position"])

            for lamp in lamps_config:
                warmup = lamps_config[lamp].get(
                    "warmup",
                    cal_config["defaults"]["warmup"],
                )

                self.gort.log.info(f"Warming up lamp {lamp} ({warmup} s).")
                await calib_nps.on(lamp)
                await asyncio.sleep(warmup)

                exp_times = lamps_config[lamp]["exposure_time"]
                if isinstance(exp_times, (int, float)):
                    exp_times = [exp_times]

                n_exp_times = len(exp_times)
                for ietime, exp_time in enumerate(exp_times):
                    flavour = lamps_config[lamp].get(
                        "flavour",
                        cal_config["defaults"]["flavours"][lamp.lower()],
                    )

                    # Check if we are spinning the fibre selector and,
                    # if so, launch the task.
                    fibsel = lamps_config[lamp].get("fibsel", False)
                    fibsel_def = cal_config["defaults"]["fibsel"]
                    if isinstance(fibsel, dict) or fibsel is True:
                        # If it's True, just use defaults.
                        if fibsel is True:
                            fibsel = {}

                        positions = fibsel.get("positions", fibsel_def["positions"])
                        order_by_steps = True

                        if isinstance(positions, (list, tuple)):
                            positions = list(positions)
                            order_by_steps = False
                            if "initial_position" in fibsel:
                                initial_position = fibsel["initial_position"]
                            else:
                                initial_position = positions[0]
                        else:
                            initial_position = fibsel.get(
                                "initial_position",
                                fibsel_def["initial_position"],
                            )

                        time_per_position = fibsel.get("time_per_position", None)
                        total_time = exp_time if time_per_position is None else None

                        fibsel_device = self.gort.telescopes.spec.fibsel
                        await fibsel_device.move_to_position(initial_position)

                        # Launch the task.
                        fibsel_task = asyncio.create_task(
                            move_mask_interval(
                                self.gort,
                                positions,
                                order_by_steps=order_by_steps,
                                total_time=total_time,
                                time_per_position=time_per_position,
                            )
                        )

                    self.gort.log.info(f"Exposing lamp for {exp_time} s.")
                    await self.gort.specs.expose(
                        flavour=flavour,
                        exposure_time=exp_time,
                        show_progress=show_progress,
                        async_readout=ietime == n_exp_times - 1,
                        object=lamp,
                    )

                    await cancel_task(fibsel_task)

                self.gort.log.info(f"Turning off {lamp}.")
                await calib_nps.off(lamp)

            if park_after:
                await self.gort.telescopes.park()

            if (
                self.gort.specs.last_exposure
                and not self.gort.specs.last_exposure.done()
            ):
                self.gort.log.info("Awaiting last exposure readout.")
                await self.gort.specs.last_exposure

        except Exception:
            self.gort.log.error(
                "Errored while executing sequence. "
                "Turning all the lamps off before raising.",
            )

            # Stop the mask iteration task.
            await cancel_task(fibsel_task)

            raise

        finally:
            # If there are no lamps there is no way we turned them on.
            if has_lamps:
                await calib_nps.all_off()

    def get_calibration_sequence(self, sequence: str):
        """Returns a dictionary with the configuration for a calibration sequence.

        Parameters
        ----------
        sequence
            The name calibration sequence.

        Returns
        -------
        sequence_dict
            The calibration sequence dictionary. This dictionary can be
            altered and then passed to :obj:`.calibrate` to execute the
            modified sequence. The returned dictionary if a deep copy of
            the original sequence; modifying it won't modify the original
            sequence.

        """

        sequences = self.gort.config["recipes"]["calibration"]["sequences"]

        if sequence not in sequences:
            raise ValueError(f"Sequence {sequence!r} not found in configuration file.")

        return deepcopy(sequences[sequence])


class QuickCals(BaseRecipe):
    """Runs a quick calibration sequence."""

    name = "quick_cals"

    async def recipe(self):
        """Runs the calibration sequence."""

        from gort import Gort

        gort = self.gort
        assert isinstance(gort, Gort)

        await gort.cleanup()

        gort.log.info("Moving telescopes to point to the calibration screen.")
        await gort.telescopes.goto_named_position("calibration")

        ########################
        # Arcs
        ########################

        gort.log.info("Turning on the HgNe lamp.")
        await gort.nps.calib.on("HgNe")

        gort.log.info("Turning on the Ne lamp.")
        await gort.nps.calib.on("Neon")

        gort.log.info("Turning on the Argon lamp.")
        await gort.nps.calib.on("Argon")

        gort.log.info("Turning on the Xenon lamp.")
        await gort.nps.calib.on("Xenon")

        gort.log.info("Waiting 180 seconds for the lamps to warm up.")
        await asyncio.sleep(180)

        fiber = random.randint(1, 12)  # select random fibre on std telescope
        fiber_str = f"P1-{fiber}"
        gort.log.info(f"Taking {fiber_str} exposure.")
        await gort.telescopes.spec.fibsel.move_to_position(fiber_str)

        for exp_time in [10, 50]:
            exp = await gort.specs.expose(
                exp_time,
                flavour="arc",
                header={"CALIBFIB": f"P1-{fiber}"},
            )
            self.log_files(exp)

        gort.log.info("Turning off all lamps.")
        await gort.nps.calib.all_off()

        ########################
        # Flats
        ########################

        gort.log.info("Turning on the Quartz lamp.")
        await gort.nps.calib.on("Quartz")

        gort.log.info("Waiting 120 seconds for the lamp to warm up.")
        await asyncio.sleep(120)

        exp_quartz = 10
        exp = await gort.specs.expose(
            exp_quartz,
            flavour="flat",
            header={"CALIBFIB": f"P1-{fiber}"},
        )
        self.log_files(exp)

        gort.log.info("Turning off the Quartz lamp.")
        await gort.nps.calib.all_off()

        gort.log.info("Turning on the LDLS lamp.")
        await gort.nps.calib.on("LDLS")

        gort.log.info("Waiting 300 seconds for the lamp to warm up.")
        await asyncio.sleep(300)

        exp_LDLS = 150

        exp = await gort.specs.expose(
            exp_LDLS,
            flavour="flat",
            header={"CALIBFIB": f"P1-{fiber}"},
        )
        self.log_files(exp)

        gort.log.info("Turning off the LDLS lamp.")
        await gort.nps.calib.all_off()

    def log_files(self, exposure: Exposure | list[Exposure]):
        """Logs the files from an exposure or list of exposures."""

        assert isinstance(exposure, Exposure)

        self.gort.log.debug(f"Files are: {exposure.get_files()}")


class BiasSequence(BaseRecipe):
    """Takes a sequence of bias frames."""

    name = "bias_sequence"

    async def recipe(self, count: int = 7):
        """Takes a sequence of bias frames.

        Parameters
        ----------
        count
            The number of bias frames to take.

        """

        from gort import Gort

        gort = self.gort
        assert isinstance(gort, Gort)

        gort.log.info("Moving telescopes to point to the selfie position.")
        await gort.telescopes.goto_named_position("selfie")

        await gort.nps.calib.all_off()

        for _ in range(count):
            await gort.specs.expose(flavour="bias")


class TwilightFlats(BaseRecipe):
    """Takes a sequence of twilight flats."""

    name = "twilight_flats"

    async def recipe(self, wait: bool = False, secondary: bool = False):
        """Takes a sequence of twilight flats."""

        from gort import Gort

        gort = self.gort
        assert isinstance(gort, Gort)

        enclosure_status = await gort.enclosure.status()
        if "OPEN" not in enclosure_status["dome_status_labels"]:
            raise RuntimeError("Dome must be open to take twilight flats.")

        has_slewed: bool = False

        eph = await get_ephemeris_summary()

        if abs(eph["time_to_sunset"]) < abs(eph["time_to_sunrise"]):
            is_sunset = True
            riseset = Time(eph["sunset"], format="jd")
            alt = 40.0
            az = 270.0
        else:
            is_sunset = False
            riseset = Time(eph["sunrise"], format="jd")
            alt = 40.0
            az = 90.0

        fudge_factor = 2
        popt = numpy.array([1.09723745, 3.55598039, -1.86597751])

        n_fibre = random.randint(1, 12)
        n_observed = 0

        while True:
            now = Time.now()

            if is_sunset:
                time_diff = (riseset - now).sec / 60.0  # Minutes
            else:
                time_diff = (now - riseset).sec / 60.0

            time_diff += fudge_factor

            if time_diff > 5:
                if wait:
                    await asyncio.sleep(60)
                    continue
            elif time_diff < -40:
                raise RuntimeError("Too late to take twilight flats.")

            exp_time = popt[0] * numpy.exp(-1.0 * time_diff / popt[1]) + popt[2]
            exp_time = numpy.ceil(exp_time)

            if exp_time < 1:
                exp_time = 1.0
            if exp_time > 300:
                raise RuntimeError("Too early/late in twilight.")

            if not has_slewed:
                gort.log.info("Moving telescopes to point to the twilight sky.")
                await gort.telescopes.goto_coordinates_all(
                    alt=alt,
                    az=az,
                    altaz_tracking=False,
                )

            fibre_str = f"P1-{n_fibre}"
            if secondary:
                fibre_str = f"P2-{n_fibre}"
            await gort.telescopes.spec.fibsel.move_to_position(fibre_str)

            gort.log.info(f"Taking {fibre_str} exposure with exp_time={exp_time:.2f}.")
            await gort.specs.expose(
                exp_time,
                flavour="flat",
                header={"CALIBFIB": fibre_str},
            )

            n_observed += 1
            if n_observed == 12:
                break

            n_fibre = n_fibre + 1
            if n_fibre > 12:
                n_fibre -= 12
