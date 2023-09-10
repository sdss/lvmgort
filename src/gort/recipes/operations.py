#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-08-13
# @Filename: operations.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, Literal

from rich.prompt import Confirm

from .base import BaseRecipe


if TYPE_CHECKING:
    from gort.devices.spec import Spectrograph


__all__ = ["StartupRecipe", "ShutdownRecipe", "CleanupRecipe"]


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
            The name of the calibration sequence to use. If :obj:`None`, uses the
            default sequence from the configuration. If :obj:`False`, skips the
            calibration sequence.
        open_enclosure
            Whether to open the enclosure.
        confirm_open
            If :obj:`True`, asks the user to confirm opening the enclosure.
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

        self.gort.log.info("Reconnecting AG cameras.")
        await self.gort.ags.reconnect()

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
            Park telescopes (and disables axes). Set to :obj:`False` if only
            closing for a brief period of time.

        """

        self.gort.log.warning("Running the shutdown sequence.")

        self.gort.log.info("Turning off all lamps.")
        await self.gort.nps.calib.all_off()

        self.gort.log.info("Making sure guiders are idle.")
        await self.gort.guiders.stop()

        await self.gort.enclosure.close()

        if park_telescopes:
            self.gort.log.info("Parking telescopes for the night.")
            await self.gort.telescopes.park()


class CleanupRecipe(BaseRecipe):
    """Stops guiders, aborts exposures, and makes sure the system is ready to go."""

    name = "cleanup"

    async def recipe(self, readout: bool = True, turn_off: bool = True):
        """Runs the cleanup recipe.

        Parameters
        ----------
        readout
            If the spectrographs are idle and with a readout pending,
            reads the spectrographs.
        turn_off
            If :obj:`True`, turns off the lamps.

        """

        self.gort.log.info("Stopping the guiders.")
        await self.gort.guiders.stop()

        if not (await self.gort.specs.are_idle()):
            cotasks = []

            for spec in self.gort.specs.values():
                status = await spec.status()
                names = status["status_names"]

                if await spec.is_reading():
                    self.gort.log.warning(f"{spec.name} is reading. Waiting.")
                    cotasks.append(self._wait_until_spec_is_idle(spec))
                elif await spec.is_exposing():
                    self.gort.log.warning(f"{spec.name} is exposing. Aborting.")
                    cotasks.append(spec.abort())
                elif "IDLE" in names and "READOUT_PENDING" in names:
                    msg = f"{spec.name} has a pending exposure."
                    if readout is False:
                        self.gort.log.warning(f"{msg} Aborting it.")
                        cotasks.append(spec.abort())
                    else:
                        self.gort.log.warning(f"{msg} Reading it.")
                        cotasks.append(spec.actor.commands.read())
                        cotasks.append(self._wait_until_spec_is_idle(spec))

            await asyncio.gather(*cotasks)

        if turn_off:
            self.gort.log.info("Turning all lights off.")
            await self.gort.nps.calib.all_off()

        # Turn off lights in the dome.
        await asyncio.gather(
            self.gort.enclosure.lights.telescope_red.off(),
            self.gort.enclosure.lights.telescope_bright.off(),
        )

    async def _wait_until_spec_is_idle(self, spec: Spectrograph):
        """Waits until an spectrograph is idle."""

        while True:
            if await spec.is_idle():
                return

            await asyncio.sleep(3)
