#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-08-13
# @Filename: calibration.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import random

from typing import ClassVar

import numpy
from astropy.time import Time

from gort.enums import ErrorCode, Event
from gort.tools import get_ephemeris_summary

from .base import BaseRecipe


__all__ = ["QuickCals", "BiasSequence", "TwilightFlats"]


class QuickCals(BaseRecipe):
    """Runs a quick calibration sequence."""

    name = "quick_cals"

    async def recipe(self):
        """Runs the calibration sequence."""

        from gort import Gort

        gort = self.gort
        assert isinstance(gort, Gort)

        specs_idle = await self.gort.specs.are_idle()
        if not specs_idle:
            raise RuntimeError("Spectrographs are not idle.")

        await gort.cleanup()

        gort.log.info("Pointing telescopes to the calibration screen.")
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
            await gort.specs.expose(
                exp_time,
                flavour="arc",
                header={"CALIBFIB": f"P1-{fiber}"},
            )

        gort.log.info("Turning off all lamps.")
        await gort.nps.calib.all_off()

        ########################
        # Flats
        ########################

        gort.log.info("Turning on the Quartz lamp.")
        await gort.nps.calib.on("Quartz")

        gort.log.info("Waiting 120 seconds for the lamp to warm up.")
        await asyncio.sleep(120)

        exp_quartz = 20
        await gort.specs.expose(
            exp_quartz,
            flavour="flat",
            header={"CALIBFIB": f"P1-{fiber}"},
        )

        gort.log.info("Turning off the Quartz lamp.")
        await gort.nps.calib.all_off()

        gort.log.info("Turning on the LDLS lamp.")
        await gort.nps.calib.on("LDLS")

        gort.log.info("Waiting 300 seconds for the lamp to warm up.")
        await asyncio.sleep(300)

        exp_LDLS = 150

        await gort.specs.expose(
            exp_LDLS,
            flavour="flat",
            header={"CALIBFIB": f"P1-{fiber}"},
        )

        gort.log.info("Turning off the LDLS lamp.")
        await gort.nps.calib.all_off()


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

        specs_idle = await self.gort.specs.are_idle()
        if not specs_idle:
            raise RuntimeError("Spectrographs are not idle.")

        gort.log.info("Pointing telescopes to the selfie position.")
        await gort.telescopes.goto_named_position("selfie")

        await gort.nps.calib.all_off()
        await gort.cleanup()

        for _ in range(count):
            await gort.specs.expose(flavour="bias")


class TwilightFlats(BaseRecipe):
    """Takes a sequence of twilight flats."""

    name = "twilight_flats"

    # Tweak factor for the exposure time.
    FUDGE_FACTOR: ClassVar[int] = 1

    # Exposure time model
    POPT: ClassVar[numpy.ndarray] = numpy.array([1.09723745, 3.55598039, -1.86597751])

    # Start sunset flats three minute before sunset.
    # Positive numbers means "into" the twilight.
    SUNSET_START: ClassVar[float] = -3

    # Start sunrise flats 15 minutes before sunrise
    SUNRISE_START: ClassVar[float] = 15

    # Maximum exposure time for normal flats.
    MAX_EXP_TIME: ClassVar[float] = 300

    # Maximum exposure time for extra flats.
    MAX_EXP_TIME_EXTRA: ClassVar[float] = 100

    async def recipe(
        self,
        wait: bool = True,
        start_fibre: int | None = None,
        secondary: bool = False,
    ):
        """Takes a sequence of twilight flats.

        Based on K. Kreckel's code.

        """

        from gort import Gort

        gort = self.gort
        assert isinstance(gort, Gort)

        await gort.cleanup()

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

        gort.log.info("Moving telescopes to point to the twilight sky.")
        await gort.telescopes.goto_coordinates_all(
            alt=alt,
            az=az,
            altaz_tracking=False,
        )

        n_fibre = start_fibre or random.randint(1, 12)
        await self.goto_fibre_position(n_fibre, secondary=secondary)

        n_observed: int = 0
        all_done: bool = False

        while True:
            # Calculate the number of minutes into the twilight. Positive values
            # mean minutes into daytime (before sunset or after sunrise).
            now = Time.now()
            time_diff_sun = (now - riseset).sec / 60.0  # Minutes
            if is_sunset:
                time_diff_sun = -time_diff_sun

            time_diff_sun += self.FUDGE_FACTOR

            # Calculate exposure time.
            aa, bb, cc = self.POPT
            exp_time = aa * numpy.exp(-time_diff_sun / bb) + cc

            if is_sunset:
                time_to_flat_twilighs = self.SUNSET_START + time_diff_sun
            else:
                time_to_flat_twilighs = -(self.SUNRISE_START + time_diff_sun)

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

            if not all_done and exp_time < self.MAX_EXP_TIME:
                # We allow negative times, which will be rounded to 1 s
                pass
            elif is_sunrise and exp_time > 400 and wait:
                self.gort.log.info(f"Exposure time is too long ({exp_time:.1f} s).")
                self.gort.log.info("Waiting 10 seconds ...")
                await asyncio.sleep(10)
                continue
            elif is_sunset and not all_done and exp_time > self.MAX_EXP_TIME:
                # We have not yet taken all 12 fibres but the
                # exposure time  is just too long.
                raise RuntimeError("Exposure time is now too long.")
            elif is_sunset and all_done and exp_time > self.MAX_EXP_TIME_EXTRA:
                # We have taken all 12 fibres and some extra ones, but the
                # exposure time is now too long. Exit.
                gort.log.debug("Exposure time is too long to take more extra flats.")
                break
            elif is_sunset and all_done and exp_time <= self.MAX_EXP_TIME_EXTRA:
                # Continue taking exposures for extra flats.
                pass
            else:
                raise RuntimeError("Too early/late to take twilight flats.")

            # Round to the nearest second.
            exp_time = numpy.ceil(exp_time)
            if exp_time < 1:
                exp_time = 1.0

            fibre_str = await self.goto_fibre_position(n_fibre, secondary=secondary)
            gort.log.info(f"Taking {fibre_str} exposure with exp_time={exp_time:.2f}.")

            try:
                await gort.specs.expose(
                    exp_time,
                    flavour="flat",
                    header={"CALIBFIB": fibre_str},
                )
            except Exception as err:
                gort.log.error(
                    "Error taking twilight flat exposure. Will ignore since we "
                    f"are on a schedule here. Error is: {err}"
                )
                await gort.notify_event(
                    Event.ERROR,
                    payload={
                        "error": str(err),
                        "error_code": ErrorCode.CALIBRATION_ERROR.value,
                        "ignore": True,
                    },
                )

            n_observed += 1
            if n_observed == 12:
                all_done = True

                if (all_done and is_sunrise) or exp_time >= self.MAX_EXP_TIME_EXTRA:
                    break

                # During sunset, after taking all 12 fibres we continue taking
                # flats until the exposure time reaches MAX_EXP_TIME_EXTRA seconds.
                gort.log.info(
                    f"All fibres observed. Taking extra flats until the "
                    f"exposure time reaches {self.MAX_EXP_TIME_EXTRA} seconds."
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
