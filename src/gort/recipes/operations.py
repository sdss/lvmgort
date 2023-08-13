#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-08-13
# @Filename: operations.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from rich.prompt import Confirm

from .base import BaseRecipe


if TYPE_CHECKING:
    pass


__all__ = ["StartupRecipe", "ShutdownRecipe"]


OPEN_DOME_MESSAGE = """Do not open the dome if you have not checked the following:
* Humidity is below 80%
* Dew point is below the temperature by > 5 degrees (?)
* Wind is below 35 mph
* There is no-one inside the enclosure
* No rain/good conditions confirmed with the Du Pont observers

Du Pont control room:
   (US) +1 626-310-0436
   (Chile) +56 51-2203-609
Slack:
   #lvm-dupont-observing
"""


class StartupRecipe(BaseRecipe):
    """Starts the telescopes, runs the calibration sequence, and opens the enclosure."""

    name = "startup"

    async def recipe(
        self,
        calibration_sequence: str | None | Literal[False] = None,
        open_enclosure: bool = True,
        confirm_open: bool = True,
        focus: bool = True,
    ):
        """Runs the startup sequence.

        Parameters
        ----------
        gort
            The `.Gort` instance to use.
        calibration_sequence
            The name of the calibration sequence to use. If `None`, uses the default
            sequence from the configuration. If `False`, skips the calibration sequence.
        open_enclosure
            Whether to open the enclosure.
        confirm_open
            If `True`, asks the user to confirm opening the enclosure.
        focus
            Whether to focus after the enclosure has open.

        """

        rconfig = self.gort.config["recipes"]["startup"]

        self.gort.log.warning("Running the startup sequence.")

        await self.gort.telescopes.home(
            home_telescopes=True,
            home_kms=True,
            home_focusers=True,
            home_fibsel=True,
        )

        self.gort.log.info("Turning off all lamps.")
        await self.gort.nps.calib.all_off()

        self.gort.log.info("Taking AG darks.")
        await self.gort.guiders.take_darks()

        if calibration_sequence is not False:
            sequence = calibration_sequence or rconfig["calibration_sequence"]
            self.gort.log.info(f"Running calibration sequence {sequence!r}.")
            await self.gort.specs.calibrate(sequence)

        if open_enclosure:
            if confirm_open:
                self.gort.log.warning(OPEN_DOME_MESSAGE)
                if not Confirm.ask(
                    "Open the dome?",
                    default=False,
                    console=self.gort._console,
                ):
                    return

            self.gort.log.info("Opening the dome ...")
            await self.gort.enclosure.open()

        if open_enclosure and focus:
            self.gort.log.info("Focusing telescopes.")
            await self.gort.guiders.focus()

        self.gort.log.info("The startup recipe has completed.")


class ShutdownRecipe(BaseRecipe):
    """Closes the telescope for the night."""

    name = "shutdown"

    async def recipe(self, park_telescopes: bool = True):
        """Shutdown the telescope, closes the dome, etc.

        Parameters
        ----------
        park_telescopes
            Park telescopes (and disables axes). Set to `False` if only
            closing for a brief period of time.

        """

        self.gort.log.warning("Running the shutdown sequence.")

        self.gort.log.info("Turning off all lamps.")
        await self.gort.nps.calib.all_off()

        self.gort.log.info("Making sure guiders are idle.")
        await self.gort.guiders.stop(now=True)

        await self.gort.enclosure.close()

        if park_telescopes:
            self.gort.log.info("Parking telescopes for the night.")
            await self.gort.telescopes.park()
