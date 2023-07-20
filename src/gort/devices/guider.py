#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-18
# @Filename: nps.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from gort.exceptions import GortError, GortGuiderError
from gort.gort import GortDevice, GortDeviceSet
from gort.maskbits import GuiderStatus


if TYPE_CHECKING:
    from clu import AMQPReply

    from gort.gort import GortClient


__all__ = ["Guider", "GuiderSet"]


class Guider(GortDevice):
    """Class representing a guider."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

        self.separation: float | None = None
        self.status: GuiderStatus = GuiderStatus.IDLE

        self.gort.add_reply_callback(self._status_cb)

    @property
    def ag(self):
        """Gets the :obj:`.AG` device associated with this guider."""

        return self.gort.ags[self.name]

    @property
    def telescope(self):
        """Gets the :obj:`.Telescope` device associated with this guider."""

        return self.gort.telescopes[self.name]

    async def _status_cb(self, reply: AMQPReply):
        """Listens to guider keywords and updates the internal state."""

        if reply.sender == self.actor.name:
            if "status" in reply.body:
                self.status = GuiderStatus(int(reply.body["status"], 16))
            if "measured_pointing" in reply.body:
                self.separation = reply.body["measured_pointing"]["separation"]

    async def wait_until_guiding(
        self,
        guide_tolerance: float | None = None,
        timeout: float | None = None,
    ) -> tuple[bool, GuiderStatus, float | None, bool]:
        """Waits until the guider has converged.

        Parameters
        ----------
        guide_tolerance
            The minimum separation, in arcsec, between the measured and desired
            positions that needs to be reached before returning. If `None`,
            waits until guiding (as opposed to acquisition) begins.
        timeout
            Maximum time, in seconds, to wait before returning. If `None`,
            waits indefinitely. If the timeout is reached it does not
            raise an exception.

        Returns
        -------
        reached
            Whether the desired minimum separation was reached.
        status
            The current :obj:`.GS`.
        separation
            The current separation.
        timedout
            `True` if the acquisition timed out.

        """

        # Initial delay to allow time for the guider to switch to DRIFTING status.
        await asyncio.sleep(1)

        elapsed = 1
        while True:
            has_acquired = (
                self.status is not None
                and self.separation is not None
                and self.status & GuiderStatus.GUIDING
                and not self.status & GuiderStatus.DRIFTING
                and (guide_tolerance is None or self.separation < guide_tolerance)
            )
            if has_acquired:
                return (True, self.status, self.separation, False)

            elapsed += 1
            if timeout is not None and elapsed > timeout:
                return (False, self.status, self.separation, True)

            await asyncio.sleep(1)

    async def expose(self, *args, continuous: bool = False, **kwargs):
        """Exposes this telescope cameras.

        Parameters
        ----------
        args,kwargs
            Arguments to be passed to the guider expose command.
        continuous
            Whether to expose the camera continuously. If `False`
            it takes a single exposure.

        """

        while True:
            await self.actor.commands.expose(
                reply_callback=self.print_reply,
                *args,
                **kwargs,
            )

            if not continuous:
                return

    async def focus(
        self,
        inplace=False,
        guess: float = 40,
        step_size: float = 0.5,
        steps: int = 7,
        exposure_time: float = 5.0,
    ):
        """Focus the telescope."""

        # Send telescopes to zenith.
        if not inplace:
            self.write_to_log("Moving telescope to zenith.")
            await self.gort.telescopes[self.name].goto_named_position(
                "zenith",
                altaz_tracking=True,
            )

        try:
            await self.actor.commands.focus(
                reply_callback=self.print_reply,
                guess=guess,
                step_size=step_size,
                steps=steps,
                exposure_time=exposure_time,
            )
        except GortError as err:
            self.write_to_log(f"Failed focusing with error {err}", level="error")
        finally:
            self.separation = None

    async def guide(
        self,
        ra: float | None = None,
        dec: float | None = None,
        exposure_time: float = 5.0,
        pixel: tuple[float, float] | str | None = None,
        **guide_kwargs,
    ):
        """Starts the guide loop.

        This command blocks until `.stop` is called.

        Parameters
        ----------
        ra,dec
            The coordinates to acquire. If `None`, the current telescope
            coordinates are used.
        exposure_time
            The exposure time of the AG integrations.
        pixel
            The pixel on the master frame on which to guide. Defaults to
            the central pixel. This can also be the name of a known pixel
            position for this telescope, e.g., ``'P1-1'`` for ``spec``.
        guide_kwargs
            Other keyword arguments to pass to ``lvmguider guide``.

        """

        self.separation = None

        if ra is None or dec is None:
            status = await self.telescope.status()
            ra_status = status["ra_j2000_hours"] * 15
            dec_status = status["dec_j2000_degs"]

            ra = ra if ra is not None else ra_status
            dec = dec if dec is not None else dec_status

        config = self.gort.config
        if isinstance(pixel, str):
            if pixel not in config["guiders"]["devices"][self.name]["named_pixels"]:
                raise GortGuiderError(f"Invalid pixel name {pixel!r}.", error_code=610)
            pixel = config["guiders"]["devices"][self.name]["named_pixels"][pixel]

        log_msg = f"Guiding at RA={ra:.6f}, Dec={dec:.6f}"
        if pixel is not None:
            log_msg += f", pixel=({pixel[0]:.1f}, {pixel[1]:.1f})."
        self.write_to_log(log_msg, level="info")

        await self.actor.commands.guide(
            reply_callback=self.print_reply,
            ra=ra,
            dec=dec,
            exposure_time=exposure_time,
            reference_pixel=pixel,
            **guide_kwargs,
        )

    async def stop(self, now: bool = False, wait_until_stopped: bool = True):
        """Stops the guide loop.

        Parameters
        ----------
        now
            Aggressively stops the guide loop.
        wait_until_stopped
            Blocks until the guider is idle.

        """

        self.write_to_log(f"Stopping guider with now={now}.", "info")
        await self.actor.commands.stop(now=now)

        if wait_until_stopped:
            while True:
                if self.status & GuiderStatus.IDLE:
                    self.write_to_log("Guider is idle.")
                    return
                await asyncio.sleep(0.5)

    async def set_pixel(
        self,
        pixel: tuple[float, float] | str | None = None,
    ):
        """Sets the master frame pixel on which to guide.

        Parameters
        ----------
        pixel
            The pixel on the master frame on which to guide. Defaults to
            the central pixel. This can also be the name of a known pixel
            position for this telescope, e.g., ``'P1-1'`` for ``spec``.

        """

        config = self.gort.config
        if isinstance(pixel, str):
            if pixel not in config["guiders"]["devices"][self.name]["named_pixels"]:
                raise GortGuiderError(f"Invalid pixel name {pixel!r}.", error_code=610)
            pixel = config["guiders"]["devices"][self.name]["named_pixels"][pixel]

        if pixel is None:
            await self.actor.commands.reset_pixel()
        else:
            await self.actor.commands.set_pixel(*pixel)


class GuiderSet(GortDeviceSet[Guider]):
    """A set of telescope guiders."""

    __DEVICE_CLASS__ = Guider

    async def expose(self, *args, continuous: bool = False, **kwargs):
        """Exposes all the cameras using the guider.

        Parameters
        ----------
        args,kwargs
            Arguments to be passed to :obj:`.Guider.expose`.
        continuous
            Whether to expose the camera continuously. If `False`
            it takes a single exposure.

        """

        await self.call_device_method(
            Guider.expose,
            *args,
            continuous=continuous,
            **kwargs,
        )

    async def take_darks(self):
        """Takes AG darks."""

        # Move telescopes to park to prevent light, since we don't have shutters.
        # We use goto_named_position to prevent disabling the telescope and having
        # to rehome.
        self.write_to_log("Moving telescopes to park position.", level="info")
        await self.gort.telescopes.goto_named_position("park")

        # Take darks.
        self.write_to_log("Taking darks.", level="info")

        cmds = []
        for ag in self.values():
            cmds.append(
                ag.actor.commands.expose(
                    flavour="dark",
                    reply_callback=ag.print_reply,
                )
            )

        if len(cmds) > 0:
            await asyncio.gather(*cmds)

    async def focus(
        self,
        inplace=False,
        guess: float = 40,
        step_size: float = 0.5,
        steps: int = 7,
        exposure_time: float = 5.0,
    ):
        """Focus all the telescopes."""

        jobs = [
            ag.focus(
                inplace=inplace,
                guess=guess,
                step_size=step_size,
                steps=steps,
                exposure_time=exposure_time,
            )
            for ag in self.values()
        ]
        await asyncio.gather(*jobs)

    async def guide(self, *args, **kwargs):
        """Guide on all telescopes.

        Parameters
        ----------
        args,kwargs
            Arguments to be passed to :obj:`.Guider.guide`.

        """

        await self.call_device_method(Guider.guide, *args, **kwargs)

    async def stop(self, now: bool = False, wait_until_stopped: bool = True):
        """Stops the guide loop on all telescopes.

        Parameters
        ----------
        now
            Aggressively stops the guide loop.
        wait_until_stopped
            Blocks until the guider is idle.

        """

        await self.call_device_method(
            Guider.stop,
            now=now,
            wait_until_stopped=wait_until_stopped,
        )

    async def wait_until_guiding(
        self,
        guide_tolerance: float | None = None,
        timeout: float | None = None,
    ):
        """Waits until the guiders have converged.

        Parameters
        ----------
        guide_tolerance
            The minimum separation, in arcsec, between the measured and desired
            positions that needs to be reached before returning. If `None`,
            waits until guiding (as opposed to acquisition) begins.
        timeout
            Maximum time, in seconds, to wait before returning. If `None`,
            waits indefinitely. If the timeout is reached it does not
            raise an exception.

        Returns
        -------
        status
            A dictionary with the telescope names and a tuple indicating whether
            the desired minimum separation was reached. the current
            :obj:`.GuiderStatus`, and the current separation for that telescope.

        """

        names = list(self)
        results = await asyncio.gather(
            *[
                self[name].wait_until_guiding(
                    guide_tolerance=guide_tolerance,
                    timeout=timeout,
                )
                for name in names
            ]
        )

        return dict(zip(names, results))
