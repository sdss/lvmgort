#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-08-13
# @Filename: calibrations.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import random

from typing import Sequence

import numpy
from astropy.time import Time

from sdsstools import get_sjd

from gort.enums import ErrorCode, Event
from gort.exceptions import GortError
from gort.tools import decap, get_ephemeris_summary, redis_client_sync

from .base import BaseRecipe


__all__ = ["QuickCals", "BiasSequence", "TwilightFlats", "LongTermCalibrations"]


class ExposureTimeTooLong(GortError):
    """Raised when the exposure time is too long to continue twilight flats."""

    pass


class QuickCals(BaseRecipe):
    """Runs a quick calibration sequence."""

    name = "quick_cals"

    async def recipe(self):
        """Runs the calibration sequence."""

        specs_idle = await self.gort.specs.are_idle()
        if not specs_idle:
            raise RuntimeError("Spectrographs are not idle.")

        await self.gort.cleanup()

        self.gort.log.info("Pointing telescopes to the calibration screen.")
        await self.gort.telescopes.goto_named_position("calibration")

        ########################
        # Arcs
        ########################

        self.gort.log.info("Turning on the HgNe lamp.")
        await self.gort.nps.calib.on("HgNe")

        self.gort.log.info("Turning on the Ne lamp.")
        await self.gort.nps.calib.on("Neon")

        self.gort.log.info("Turning on the Argon lamp.")
        await self.gort.nps.calib.on("Argon")

        self.gort.log.info("Turning on the Xenon lamp.")
        await self.gort.nps.calib.on("Xenon")

        self.gort.log.info("Waiting 180 seconds for the lamps to warm up.")
        await asyncio.sleep(180)

        fiber = random.randint(1, 12)  # select random fibre on std telescope
        fiber_str = f"P1-{fiber}"
        self.gort.log.info(f"Taking {fiber_str} exposure.")
        await self.gort.telescopes.spec.fibsel.move_to_position(fiber_str)

        for exp_time in [10, 50]:
            await self.gort.specs.expose(
                exp_time,
                flavour="arc",
                header={"CALIBFIB": f"P1-{fiber}"},
            )

        self.gort.log.info("Turning off all lamps.")
        await self.gort.nps.calib.all_off()

        ########################
        # Flats
        ########################

        self.gort.log.info("Turning on the Quartz lamp.")
        await self.gort.nps.calib.on("Quartz")

        self.gort.log.info("Waiting 120 seconds for the lamp to warm up.")
        await asyncio.sleep(120)

        exp_quartz = 20
        await self.gort.specs.expose(
            exp_quartz,
            flavour="flat",
            header={"CALIBFIB": f"P1-{fiber}"},
        )

        self.gort.log.info("Turning off the Quartz lamp.")
        await self.gort.nps.calib.all_off()

        self.gort.log.info("Turning on the LDLS lamp.")
        await self.gort.nps.calib.on("LDLS")

        self.gort.log.info("Waiting 300 seconds for the lamp to warm up.")
        await asyncio.sleep(300)

        exp_LDLS = 150

        await self.gort.specs.expose(
            exp_LDLS,
            flavour="flat",
            header={"CALIBFIB": f"P1-{fiber}"},
        )

        self.gort.log.info("Turning off the LDLS lamp.")
        await self.gort.nps.calib.all_off()


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

        specs_idle = await self.gort.specs.are_idle()
        if not specs_idle:
            raise RuntimeError("Spectrographs are not idle.")

        self.gort.log.info("Pointing telescopes to the selfie position.")
        await self.gort.telescopes.goto_named_position("selfie")

        await self.gort.nps.calib.all_off()
        await self.gort.cleanup()

        for _ in range(count):
            await self.gort.specs.expose(flavour="bias")


class TwilightFlats(BaseRecipe):
    """Takes a sequence of twilight flats."""

    name = "twilight_flats"

    async def recipe(
        self,
        wait: bool = True,
        start_fibre: int | None = None,
        secondary: bool = False,
    ):
        """Takes a sequence of twilight flats.

        Based on K. Kreckel's code.

        """

        config = self.gort.config["recipes"][self.name]

        # Exposure time model
        popt: numpy.ndarray = numpy.array(config["popt"])

        # Start sunset flats two minutes after sunset.
        # Positive numbers means "into" the twilight.
        sunset_start: float = config.get("sunset_start", 2)

        # Start sunrise flats 15 minutes before sunrise
        sunrise_start: float = config.get("sunrise_start", 15)

        # Fudge factor for the exposure time, in minutes.
        fudge_factor: float = config.get("fudge_factor", 0)

        # Minimum exposure time for normal flats.
        min_exp_time: float = config.get("min_exp_time", 1)

        # Maximum exposure time for normal flats.
        max_exp_time: float = config.get("max_exp_time", 300)

        # Maximum exposure time for extra flats.
        max_exp_time_extra: float = config.get("max_exp_time_extra", 100)

        # Whether to clip the exposure time to the maximum.
        clip_to_max_exposure_time: bool = config.get("clip_to_max_exposure_time", True)

        await self.gort.cleanup()

        if not (await self.gort.enclosure.is_open()):
            raise RuntimeError("Dome must be open to take twilight flats.")

        specs_idle = await self.gort.specs.are_idle()
        if not specs_idle:
            raise RuntimeError("Spectrographs are not idle.")

        eph = await get_ephemeris_summary()

        is_sunset: bool = False
        is_sunrise: bool = False

        if abs(eph["time_to_sunset"]) < abs(eph["time_to_sunrise"]):
            is_sunset = True
            riseset = Time(eph["sunset"], format="jd", scale="utc")
            alt = 40.0
            az = 270.0
        else:
            is_sunrise = True
            riseset = Time(eph["sunrise"], format="jd", scale="utc")
            alt = 40.0
            az = 90.0

        self.gort.log.info("Moving telescopes to point to the twilight sky.")
        await self.gort.telescopes.goto_coordinates_all(
            alt=alt,
            az=az,
            altaz_tracking=False,
        )

        n_fibre = start_fibre or random.randint(1, 12)
        await self.goto_fibre_position(n_fibre, secondary=secondary)

        n_observed: int = 0
        total_fibres: int = 12
        all_done: bool = False

        while True:
            # Calculate the number of minutes into the twilight. Positive values
            # mean minutes into daytime (before sunset or after sunrise).
            now = Time.now()
            time_diff_sun = (now - riseset).sec / 60.0  # Minutes
            if is_sunset:
                time_diff_sun = -time_diff_sun

            # Calculate exposure time.
            aa, bb, cc = popt
            exp_time = aa * numpy.exp(-(time_diff_sun + fudge_factor) / bb) + cc

            if is_sunset:
                time_to_flat_twilighs = sunset_start + time_diff_sun
            else:
                time_to_flat_twilighs = -(sunrise_start + time_diff_sun)

            if time_to_flat_twilighs > 0:
                if wait:
                    self.gort.log.info(
                        "Waiting for twilight. Time to twilight flats: "
                        f"{time_to_flat_twilighs:.1f} minutes."
                    )
                    await asyncio.sleep(time_to_flat_twilighs * 60)
                    continue
                else:
                    raise RuntimeError("Too early to take twilight flats.")

            if not all_done and exp_time < max_exp_time:
                # We allow negative times, which will be rounded to 1 s
                pass
            elif is_sunrise and exp_time > 400 and wait:
                self.gort.log.info(f"Exposure time is too long ({exp_time:.1f} s).")
                self.gort.log.info("Waiting 10 seconds ...")
                await asyncio.sleep(10)
                continue
            elif is_sunset and not all_done and exp_time > max_exp_time:
                if clip_to_max_exposure_time:
                    self.gort.log.info(
                        f"Exposure time is too long ({exp_time:.1f} s). "
                        f"Clipping to {max_exp_time} seconds."
                    )
                    exp_time = max_exp_time
                else:
                    # We have not yet taken all 12 fibres but the
                    # exposure time  is just too long.
                    raise ExposureTimeTooLong(
                        f"Exposure time is now too long to continue taking flats. "
                        f"Took flats for {n_observed}/{total_fibres} fibres."
                    )
            elif is_sunset and all_done and exp_time > max_exp_time_extra:
                # We have taken all 12 fibres and some extra ones, but the
                # exposure time is now too long. Exit.
                self.gort.log.debug("Exposure time is too long to take more flats.")
                break
            elif is_sunset and all_done and exp_time <= max_exp_time_extra:
                # Continue taking exposures for extra flats.
                pass
            else:
                raise RuntimeError("Too early/late to take twilight flats.")

            # Round to the nearest second.
            exp_time = numpy.round(exp_time, 1)

            # Require a minimum exposure time.
            if exp_time < min_exp_time:
                await asyncio.sleep(2)
                continue

            if exp_time < 1:
                exp_time = 1.0

            fibre_str = await self.goto_fibre_position(n_fibre, secondary=secondary)
            self.gort.log.info(f"Exposing {fibre_str} with exp_time={exp_time:.2f}.")

            try:
                await self.gort.specs.expose(
                    float(exp_time),
                    flavour="flat",
                    header={"CALIBFIB": fibre_str},
                )
            except Exception as err:
                self.gort.log.error(
                    "Error taking twilight flat exposure. Will ignore since we "
                    f"are on a schedule here. Error is: {decap(err)}"
                )
                await self.gort.notify_event(
                    Event.ERROR,
                    payload={
                        "error": str(err),
                        "error_code": ErrorCode.CALIBRATION_ERROR.value,
                        "ignore": True,
                    },
                )

            n_observed += 1
            if n_observed == total_fibres:
                all_done = True

                if (all_done and is_sunrise) or exp_time >= max_exp_time_extra:
                    break

                # During sunset, after taking all 12 fibres we continue taking
                # flats until the exposure time reaches MAX_EXP_TIME_EXTRA seconds.
                self.gort.log.info(
                    f"All fibres observed. Taking extra flats until the "
                    f"exposure time reaches {max_exp_time_extra} seconds."
                )

            n_fibre = n_fibre + 1
            if n_fibre > 12:
                n_fibre -= 12

    async def goto_fibre_position(self, n_fibre: int, secondary: bool = False):
        """Moves the mask to a fibre position."""

        fibre_str = f"P1-{n_fibre}"
        if secondary:
            fibre_str = f"P2-{n_fibre}"
        await self.gort.telescopes.spec.fibsel.move_to_position(fibre_str)

        return fibre_str


class LongTermCalibrations(BaseRecipe):
    """Runs the long-term calibration sequence."""

    name = "long_term_calibrations"

    async def recipe(self, biases: bool | None = None):
        """Runs the calibration sequence.

        Parameters
        ----------
        biases
            If :obj:`True`, takes a sequence of bias frames. If :obj:`None`, checks
            if the Overwatcher has taken the biases today and won't retake them if so.

        """

        config = self.gort.config["recipes"][self.name]

        n_biases: int = config.get("n_biases", 7)
        quartz_exp_time: float = config.get("quartz_exp_time", 20)
        ldls_exp_time: float = config.get("ldls_exp_time", 150)

        arc_exp_times: Sequence[float] = config.get("arc_exp_times", [10, 50])
        if not isinstance(arc_exp_times, Sequence):
            arc_exp_times = [arc_exp_times]

        if biases is None:
            biases = self.check_biases()

        if biases:
            self.gort.log.info(f"Taking {n_biases} bias frames.")
            await self.gort.execute_recipe("bias_sequence", count=n_biases)

        self.gort.log.info("Moving telescopes to point to the calibration screen.")
        await self.gort.telescopes.goto_named_position("calibration")

        # Quart flats

        self.gort.log.info("Turning on the Quartz lamp.")
        await self.gort.nps.calib.on("Quartz")

        self.gort.log.info("Waiting 120 seconds for the lamp to warm up.")
        await asyncio.sleep(120)

        for fibre in range(12):
            fibre_str = f"P1-{fibre + 1}"
            self.gort.log.info(f"Taking fibre {fibre_str} exposure.")

            await self.gort.telescopes.spec.fibsel.move_to_position(fibre_str)
            await self.gort.specs.expose(
                quartz_exp_time,
                flavour="flat",
                header={"CALIBFIB": fibre_str},
            )

        self.gort.log.debug("Turning off the Quartz lamp.")
        await self.gort.nps.calib.all_off()

        # LDLS flats

        self.gort.log.info("Turning on the LDLS lamp.")
        await self.gort.nps.calib.on("LDLS")

        self.gort.log.info("Waiting 300 seconds for the lamp to warm up.")
        await asyncio.sleep(300)

        for fibre in range(12):
            fibre_str = f"P1-{fibre + 1}"
            self.gort.log.info(f"Taking fibre {fibre_str} exposure.")

            await self.gort.telescopes.spec.fibsel.move_to_position(fibre_str)
            await self.gort.specs.expose(
                ldls_exp_time,
                flavour="flat",
                header={"CALIBFIB": fibre_str},
            )

        self.gort.log.debug("Turning off the LDLS lamp.")
        await self.gort.nps.calib.all_off()

        # Arcs
        self.gort.log.info("Turning on arc lamps.")

        await self.gort.nps.calib.on("HgNe")
        await self.gort.nps.calib.on("Neon")
        await self.gort.nps.calib.on("Argon")
        await self.gort.nps.calib.on("Xenon")

        self.gort.log.info("Waiting 180 seconds for the lamps to warm up.")
        await asyncio.sleep(180)

        for fibre in range(12):
            fibre_str = f"P1-{fibre + 1}"
            self.gort.log.info(f"Taking fibre {fibre_str} exposures.")

            await self.gort.telescopes.spec.fibsel.move_to_position(fibre_str)
            for arc_exp_time in arc_exp_times:
                await self.gort.specs.expose(
                    arc_exp_time,
                    flavour="arc",
                    header={"CALIBFIB": fibre_str},
                )

        self.gort.log.debug("Turning off all lamps.")
        await self.gort.nps.calib.all_off()

        await self.gort.telescopes.park(disable=False)

    def check_biases(self) -> bool:
        """Checks whether the biases have been taken tonight."""

        sjd = get_sjd("LCO")

        with redis_client_sync() as redis:
            try:
                key = f"overwatcher:calibrations:{sjd}"
                bias_data = redis.json().get(key, ".bias_sequence")
            except Exception:
                return True

            if bias_data is None or "state" not in bias_data:
                return True

            return bias_data["state"] == "done"  # type: ignore
