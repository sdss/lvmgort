#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-08-13
# @Filename: operations.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, ClassVar

from rich.prompt import Confirm

from sdsstools.utils import GatheringTaskGroup

from gort.tools import get_lvmapi_route, overwatcher_is_running

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

SHUTDOWN_MESSAGE = """The shutdown recipe has completed.

Please confirm that the dome is closed and the telescopes
are parked by turning on the dome lights with

await g.enclosure.lights.telescope_bright.on()

and checking webcam LVM-TEL06. Then turn off the lights with

await g.enclosure.lights.telescope_bright.off()

If the dome is not closed, please run

await g.enclosure.close(force=True)

If that does not work, please contact the Du Pont observers.

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
        open_enclosure: bool = True,
        confirm_open: bool = True,
        focus: bool = True,
    ):
        """Runs the startup sequence.

        Parameters
        ----------
        gort
            The `.Gort` instance to use.
        open_enclosure
            Whether to open the enclosure.
        confirm_open
            If :obj:`True`, asks the user to confirm opening the enclosure.
        focus
            Whether to focus after the enclosure has open.

        """

        self.gort.log.warning("Running the startup sequence.")

        await self.gort.telescopes.home(
            home_telescopes=True,
            home_kms=True,
            home_focusers=True,
            home_fibsel=True,
        )

        self.gort.log.info("Turning off all calibration lamps and dome lights.")
        await self.gort.nps.calib.all_off()
        await self.gort.enclosure.lights.dome_all_off()
        await self.gort.enclosure.lights.spectrograph_room.off()

        self.gort.log.info("Reconnecting AG cameras.")
        await self.gort.ags.reconnect()

        self.gort.log.info("Taking AG darks.")
        await self.gort.guiders.take_darks()

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

    async def recipe(
        self,
        park_telescopes: bool = True,
        additional_close: bool = False,
        disable_overwatcher: bool = False,
        retry_without_parking: bool = False,
        show_message: bool = True,
    ):
        """Shutdown the telescope, closes the dome, etc.

        Parameters
        ----------
        park_telescopes
            Park telescopes (and disables axes). Set to :obj:`False` if only
            closing for a brief period of time. If the dome fails to close with
            ``park_telescopes=True``, it will try again without parking the
            telescopes.
        additional_close
            Issues an additional ``close`` command after the dome is closed.
            This is a temporary solution to make sure the dome is closed
            while we investigate the issue with the dome not fully closing
            sometimes.
        disable_overwatcher
            If :obj:`True`, disables the Overwatcher.
        retry_without_parking
            If the dome fails to park the telescopes before closing the dome, retries
            without parking the telescopes.
        show_message
            If :obj:`True`, shows a message with instructions on how to confirm
            the dome is closed.

        """

        self.gort.log.warning("Running the shutdown sequence.")

        async with GatheringTaskGroup() as group:
            self.gort.log.info("Turning off all lamps.")
            group.create_task(self.gort.nps.calib.all_off())

            self.gort.log.info("Making sure guiders are idle.")
            group.create_task(self.gort.guiders.stop())

            self.gort.log.info("Closing the dome.")
            group.create_task(
                self.gort.enclosure.close(retry_without_parking=retry_without_parking)
            )

            if disable_overwatcher:
                self.gort.log.info("Disabling the overwatcher.")
                group.create_task(
                    self.gort.send_command("lvm.overwatcher", "disable --now")
                )

        if park_telescopes:
            self.gort.log.info("Parking telescopes for the night.")
            await self.gort.telescopes.park()

        if additional_close:
            self.gort.log.info("Closing the dome again.")
            await asyncio.sleep(5)
            await self.gort.enclosure.close(force=True)

        if show_message:
            self.gort.log.warning(SHUTDOWN_MESSAGE)


class CleanupRecipe(BaseRecipe):
    """Stops guiders, aborts exposures, and makes sure the system is ready to go."""

    name = "cleanup"

    async def recipe(self, readout: bool = True, turn_lamps_off: bool = True):
        """Runs the cleanup recipe.

        Parameters
        ----------
        readout
            If the spectrographs are idle and with a readout pending,
            reads the spectrographs.
        turn_lamps_off
            If :obj:`True`, turns off the dome lights and calibration lamps.

        """

        self.gort.log.info("Stopping the guiders.")
        await self.gort.guiders.stop()

        if not (await self.gort.specs.are_idle()):
            extra_sleep: float = 0
            cotasks = []

            for spec in self.gort.specs.values():
                status = await spec.status()
                names = status["status_names"]

                if await spec.is_reading():
                    self.gort.log.warning(f"{spec.name} is reading. Waiting.")
                    cotasks.append(self._wait_until_spec_is_idle(spec))
                    extra_sleep = 10
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
                        extra_sleep = 10

            try:
                await asyncio.gather(*cotasks)

                # HACK: lvmscp says the controller is idle before it actually
                # writes the image to disk. If we reset too fast (as we are going
                # to do just after this) that will crash the exposures.
                # I'll fix that in lvmscp (promise) but for now we add a sleep here
                # to allows the images to post-process and write before resetting.
                await asyncio.sleep(extra_sleep)
            except Exception as ee:
                self.gort.log.error(f"Error during cleanup: {ee}")
                self.gort.log.warning("Resetting the spectrographs.")

        await self.gort.specs.reset(full=True)

        if turn_lamps_off:
            self.gort.log.info("Turning off all calibration lamps and dome lights.")
            await self.gort.nps.calib.all_off()
            await self.gort.enclosure.lights.dome_all_off()

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


class PreObservingRecipe(BaseRecipe):
    """Prepares the system for observing."""

    name = "pre-observing"

    async def recipe(self):
        """Runs the pre-observing sequence."""

        # Run a clean-up first in case there are any issues with the specs.
        await self.gort.cleanup(readout=False)

        tasks = []

        tasks.append(
            self.gort.telescopes.home(
                home_fibsel=True,
                home_focusers=True,
                home_kms=True,
                home_telescopes=True,
            )
        )
        tasks.append(self.gort.telescopes.park(disable=False, kmirror=False))
        tasks.append(self.gort.ags.reconnect())
        tasks.append(self.gort.specs.expose(flavour="bias"))

        for task in tasks:
            await task

        # Take a dark for the AG cameras here. This is not the ideal time to do it
        # because there's still light, but we want to be sure we have a dark in case
        # something weird happens and the startup recipe is not run before observing.
        # The telescopes are at park and guider.take_darks() uses that position.
        await self.gort.guiders.take_darks()


class PostObservingRecipe(BaseRecipe):
    """Runs the post-observing tasks.

    These include:

    - Closing the dome.
    - Parking the telescopes.
    - Turning off all lamps.
    - Stopping the guiders.
    - Sending the night log email.

    """

    name = "post-observing"

    email_route: ClassVar[str] = "/logs/night-logs/0/email?only_if_not_sent=1"

    async def recipe(self, send_email: bool = True):
        """Runs the post-observing sequence."""

        tasks = []

        closed = await self.gort.enclosure.is_closed()
        if not closed:
            tasks.append(self.gort.enclosure.close(retry_without_parking=True))

        tasks.append(self.gort.telescopes.park())
        tasks.append(self.gort.nps.calib.all_off())
        tasks.append(self.gort.guiders.stop())

        for task in tasks:
            try:
                await task
            except Exception as ee:
                self.gort.log.error(f"Error running post-observing task: {ee}")

        if send_email:
            self.gort.log.info("Sending night log email.")
            result = await get_lvmapi_route(self.email_route)
            if not result:
                self.gort.log.warning("Night log had already been sent.")

        # Disable the overwatcher.
        if await overwatcher_is_running():
            cmd = await self.gort.send_command("lvm.overwatcher", "disable")
            if cmd.status.did_fail:
                self.gort.log.error("Failed to disable the overwatcher.")
            else:
                self.gort.log.info("Overwatcher has been disabled.")
