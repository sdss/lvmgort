#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-18
# @Filename: nps.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import datetime
from functools import partial

from typing import TYPE_CHECKING

import numpy
import pandas
from packaging.version import Version

from gort.exceptions import ErrorCodes, GortError, GortGuiderError
from gort.gort import GortDevice, GortDeviceSet
from gort.maskbits import GuiderStatus
from gort.tools import build_guider_reply_list, cancel_task


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

        self.guide_monitor_task: asyncio.Task | None = None
        self._best_focus: tuple[float, float] = (-999.0, -999.0)

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
            positions that needs to be reached before returning. If :obj:`None`,
            waits until guiding (as opposed to acquisition) begins.
        timeout
            Maximum time, in seconds, to wait before returning. If :obj:`None`,
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
            :obj:`True` if the acquisition timed out.

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
            Whether to expose the camera continuously. If :obj:`False`
            it takes a single exposure.

        """

        while True:
            await self.actor.commands.expose(*args, **kwargs)

            if not continuous:
                return

    async def focus(
        self,
        inplace=False,
        guess: float | None = None,
        step_size: float = 0.5,
        steps: int = 7,
        exposure_time: float = 5.0,
    ):
        """Focus the telescope.

        Parameters
        ----------
        inplace
            If :obj:`True`, focuses the telescope where it is pointing at. Otherwise
            points to zenith.
        guess
            The initial guess for the focuser position. If :obj:`None`, the
            default value from the configuration file is used.
        step_size
            The size, in focuser units, of each step.
        steps
            The total number of step points. Must be an odd number.
        exposure_time
            The exposure time for each step.

        """

        self._best_focus = (-999, -999)

        # Send telescopes to zenith.
        if not inplace:
            self.write_to_log("Moving telescope to zenith.")
            await self.gort.telescopes[self.name].goto_named_position(
                "zenith",
                altaz_tracking=True,
            )

        if guess is None:
            guess = self.gort.config["guiders"]["focus"]["guess"][self.name]
            if isinstance(guess, (list, tuple)):
                sensor_data = await self.gort.telemetry[self.name].status()
                temp = sensor_data["sensor1"]["temperature"]
                guess = guess[0] * temp + guess[1]

        try:
            self.write_to_log(
                f"Focusing telescope {self.name} with initial guess {guess:.1f}.",
                "info",
            )
            await self.actor.commands.focus(
                reply_callback=self._parse_focus,
                guess=guess,
                step_size=step_size,
                steps=steps,
                exposure_time=exposure_time,
            )
        except GortError as err:
            self.write_to_log(f"Failed focusing with error {err}", level="error")
        finally:
            self.separation = None
            self.write_to_log(
                f"Best focus: {self._best_focus[1]} arcsec "
                f"at {self._best_focus[0]} DT",
                "info",
            )

            if self._best_focus[1] < 0.3:
                self.write_to_log("Focus value is invalid.", "error")

        return self._best_focus

    def _parse_focus(self, reply: AMQPReply):
        """Parses replies from the guider command."""

        if not reply.body:
            return

        self.log_replies(reply, skip_debug=False)

        if "best_focus" in reply.body:
            self._best_focus = (
                reply.body["best_focus"]["focus"],
                reply.body["best_focus"]["fwhm"],
            )

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
            The coordinates to acquire. If :obj:`None`, the current telescope
            coordinates are used.
        exposure_time
            The exposure time of the AG integrations.
        pixel
            The pixel on the master frame on which to guide. Defaults to
            the central pixel. This can also be the name of a known pixel
            position for this telescope, e.g., ``'P1-1'`` for ``spec``.
        guide_kwargs
            Other keyword arguments to pass to ``lvmguider guide``. The includes
            the ``pa`` argument that if not provided is assumed to be zero.

        """

        # The PA argument in lvmguider was added in 0.4.0a0.
        if self.version == Version("0.99.0") or self.version < Version("0.4.0a0"):
            guide_kwargs.pop("pa")

        self.separation = None

        if not self.status & GuiderStatus.IDLE:
            raise GortGuiderError(
                "Guider is not IDLE",
                error_code=ErrorCodes.COMMAND_FAILED,
            )

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

        try:
            self.guide_monitor_task = asyncio.create_task(self._monitor_task())
            await self.actor.commands.guide(
                reply_callback=partial(self.log_replies, skip_debug=False),
                ra=ra,
                dec=dec,
                exposure_time=exposure_time,
                reference_pixel=pixel,
                **guide_kwargs,
            )
        except Exception as err:
            # Deal with the guide command being cancelled when we stop it.
            if "This command has been cancelled" not in str(err):
                raise
        finally:
            await cancel_task(self.guide_monitor_task)

    async def _monitor_task(self, timeout: float = 30):
        """Monitors guiding and reports average and last guide metrics."""

        current_data = []

        task = asyncio.create_task(
            build_guider_reply_list(
                self.gort,
                current_data,
                actor=self.actor.name,
            )
        )

        try:
            while True:
                await asyncio.sleep(timeout)

                # Build DF with all the frames.
                df = pandas.DataFrame.from_records(current_data)

                # Group by frameno, keep only non-NaN values.
                df = df.groupby(["frameno", "telescope"], as_index=False).apply(
                    lambda g: g.bfill(axis=0).iloc[0, :]
                )

                # Sort by frameno.
                df = df.sort_values("frameno")

                # Select columns.
                df = df[
                    [
                        "frameno",
                        "time",
                        "n_sources",
                        "focus_position",
                        "fwhm",
                        "telescope",
                        "ra",
                        "dec",
                        "ra_offset",
                        "dec_offset",
                        "separation",
                        "mode",
                    ]
                ]

                # Remove NaN rows.
                df = df.dropna()

                now = datetime.datetime.utcnow()
                time_range = now - pandas.Timedelta(f"{timeout} seconds")
                time_data = df.loc[df.time > time_range, :]

                if (
                    len(time_data) == 0
                    or "fwhm" not in time_data
                    or "separation" not in time_data
                ):
                    continue

                # Calculate and report last.
                last = time_data.tail(1)
                sep_last = round(last.separation.values[0], 3)
                fwhm_last = round(last.fwhm.values[0], 2)
                mode_last = last["mode"].values[0]
                self.write_to_log(
                    f"Last: sep={sep_last} arcsec; fwhm={fwhm_last} arcsec; "
                    f"mode={mode_last!r}",
                    "info",
                )

                # Calculate and report averages.
                sep_avg = round(time_data.separation.mean(), 3)
                fwhm_avg = round(time_data.fwhm.mean(), 2)
                self.write_to_log(
                    f"Average ({timeout} s): sep={sep_avg} arcsec; "
                    f"fwhm={fwhm_avg} arcsec",
                    "info",
                )

        except asyncio.CancelledError:
            await cancel_task(task)
            return

    async def stop(self) -> None:
        """Stops the guide loop.

        Parameters
        ----------
        wait_until_stopped
            Blocks until the guider is idle.

        """

        self.write_to_log("Stopping guider.", "info")

        await self.actor.commands.stop()
        self.status = GuiderStatus.IDLE

    async def set_pixel(self, pixel: tuple[float, float] | str | None = None):
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

    async def apply_corrections(self, enable: bool = True):
        """Enable/disable corrections being applied to the axes."""

        await self.actor.commands.corrections(mode="enable" if enable else "disable")


class GuiderSet(GortDeviceSet[Guider]):
    """A set of telescope guiders."""

    __DEVICE_CLASS__ = Guider
    __DEPLOYMENTS__ = ["lvmguider"]

    async def expose(self, *args, continuous: bool = False, **kwargs):
        """Exposes all the cameras using the guider.

        Parameters
        ----------
        args,kwargs
            Arguments to be passed to :obj:`.Guider.expose`.
        continuous
            Whether to expose the camera continuously. If :obj:`False`
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
        for guider in self.values():
            cmds.append(
                guider.actor.commands.expose(
                    flavour="dark",
                    reply_callback=partial(guider.log_replies, skip_debug=False),
                )
            )

        if len(cmds) > 0:
            await asyncio.gather(*cmds)

    async def focus(
        self,
        inplace=False,
        guess: float | dict[str, float] | None = None,
        step_size: float = 0.5,
        steps: int = 7,
        exposure_time: float = 5.0,
    ):
        """Focus all the telescopes.

        Parameters
        ----------
        inplace
            If :obj:`True`, focuses the telescopes where they are pointing at. Otherwise
            points to zenith.
        guess
            The initial guesses for focuser position. If :obj:`None`, the default
            values from the configuration file are used. It can also be a float
            value, which will be used for all telescopes, or a mapping of telescope
            name to guess value. Missing values will default to the configuration
            value.
        step_size
            The size, in focuser units, of each step.
        steps
            The total number of step points. Must be an odd number.
        exposure_time
            The exposure time for each step.

        """

        self.write_to_log("Running focus sequence.", "info")

        if guess is None:
            guess_dict = {}
        elif isinstance(guess, dict):
            guess_dict = guess
        else:
            guess_dict = {guider_name: guess for guider_name in self}

        jobs = [
            self[guider_name].focus(
                inplace=inplace,
                guess=guess_dict.get(guider_name, None),
                step_size=step_size,
                steps=steps,
                exposure_time=exposure_time,
            )
            for guider_name in self
        ]
        results = await asyncio.gather(*jobs)

        best_focus = [f"{name}: {results[itel][1]}" for itel, name in enumerate(self)]

        self.write_to_log("Best focus: " + ", ".join(best_focus), "info")

        fwhms = numpy.array([fwhm for _, fwhm in results])
        if numpy.any(fwhms < 0.3):
            self.write_to_log("One or more focus values are invalid.", "error")

    async def guide(self, *args, **kwargs):
        """Guide on all telescopes.

        Parameters
        ----------
        args,kwargs
            Arguments to be passed to :obj:`.Guider.guide`.

        """

        await self.call_device_method(Guider.guide, *args, **kwargs)

    async def stop(self):
        """Stops the guide loop on all telescopes."""

        await self.call_device_method(Guider.stop)

    async def apply_corrections(self, enable: bool = True):
        """Enable/disable corrections being applied to the axes."""

        await self.call_device_method(Guider.apply_corrections, enable=enable)

    async def wait_until_guiding(
        self,
        names: list[str] | None = None,
        guide_tolerance: float | None = None,
        timeout: float | None = None,
    ):
        """Waits until the guiders have converged.

        Parameters
        ----------
        names
            List of telescopes to wait for convergence.
        guide_tolerance
            The minimum separation, in arcsec, between the measured and desired
            positions that needs to be reached before returning. If :obj:`None`,
            waits until guiding (as opposed to acquisition) begins.
        timeout
            Maximum time, in seconds, to wait before returning. If :obj:`None`,
            waits indefinitely. If the timeout is reached it does not
            raise an exception.

        Returns
        -------
        status
            A dictionary with the telescope names and a tuple indicating whether
            the desired minimum separation was reached. The current
            ``GuiderStatus``, and the current separation for that telescope.

        """

        names = names or list(self)
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
