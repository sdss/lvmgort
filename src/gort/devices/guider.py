#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-18
# @Filename: nps.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import partial

from typing import TYPE_CHECKING

import polars
from packaging.version import Version

from gort import config
from gort.enums import GuiderStatus
from gort.exceptions import ErrorCode, GortError, GortGuiderError
from gort.gort import GortDevice, GortDeviceSet
from gort.tools import GuiderMonitor, cancel_task


if TYPE_CHECKING:
    from clu import AMQPReply

    from gort.core import ActorReply
    from gort.gort import GortClient


__all__ = ["Guider", "GuiderSet"]


class Guider(GortDevice):
    """Class representing a guider."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

        self.separation: float | None = None
        self.status: GuiderStatus = GuiderStatus.IDLE

        self.guider_monitor = GuiderMonitor(self.gort, self.name)
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
        sweep: bool = True,
        guess: float | None = None,
        step_size: float = config["guiders.focus.step_size"],
        steps: int = config["guiders.focus.steps"],
        exposure_time: float = config["guiders.focus.exposure_time"],
    ):
        """Focus the telescope.

        Parameters
        ----------
        inplace
            If :obj:`True`, focuses the telescope where it is pointing at. Otherwise
            points to zenith.
        sweep
            Performs a focus sweep around the initial guess position to find the
            best focus. If :obj:`False`, the focus position is determined based on
            the current bench temperature.
        guess
            The initial guess for the focuser position. If :obj:`None`, the initial
            guess is determined based on the current bench temperature.
        step_size
            The size, in focuser units, of each step.
        steps
            The total number of step points. Must be an odd number.
        exposure_time
            The exposure time for each step.

        """

        reply_callback = partial(self.log_replies, skip_debug=False)

        if sweep is False:
            self.write_to_log("Adjusting focus position.", "info")
            await self.actor.commands.adjust_focus(reply_callback=reply_callback)
            return

        if self.status & GuiderStatus.NON_IDLE:
            self.write_to_log(
                "Guider is not idle. Stopping it before focusing.",
                level="warning",
            )
            await self.stop()

        # Send telescopes to zenith.
        if not inplace:
            self.write_to_log("Moving telescope to zenith.")
            await self.gort.telescopes[self.name].goto_named_position(
                "zenith",
                altaz_tracking=True,
            )

        try:
            self.write_to_log(f"Focusing telescope {self.name}.", "info")

            replies = await self.actor.commands.focus(
                reply_callback=reply_callback,
                guess=guess,
                step_size=step_size,
                steps=steps,
                exposure_time=exposure_time,
            )

            best_focus = replies.get("best_focus")

            if best_focus is None:
                raise GortError("best_focus keyword was not emitted.")
            elif best_focus["focus"] < 0.5 or best_focus["r2"] < 0.5:
                raise GortError(
                    "Estimated focus does not seem to be correct. "
                    "Please repeat the focus sweep."
                )

            focus = best_focus["focus"]
            fwhm = best_focus["fwhm"]
            self.write_to_log(
                f"Best focus: {fwhm} arcsec at {focus} DT",
                "info",
            )

            return focus, fwhm

        except GortError as err:
            self.write_to_log(f"Failed focusing with error: {err}", level="error")

        finally:
            self.separation = None

        return -999, -999

    async def adjust_focus(self):
        """Adjusts the focus position based on the current bench temperature."""

        await self.focus(sweep=False)

    async def guide(
        self,
        ra: float | None = None,
        dec: float | None = None,
        exposure_time: float = 5.0,
        pixel: tuple[float, float] | str | None = None,
        monitor: bool = True,
        output_monitor_data: bool = True,
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
        monitor
            Whether to monitor the guide loop and output the average and last
            guide metrics every 30 seconds.
        output_monitor_data
            Whether to output the monitor data to the log.
        guide_kwargs
            Other keyword arguments to pass to ``lvmguider guide``. The includes
            the ``pa`` argument that if not provided is assumed to be zero.

        """

        monitor_task: asyncio.Task | None = None

        # The PA argument in lvmguider was added in 0.4.0a0.
        if self.version == Version("0.99.0") or self.version < Version("0.4.0a0"):
            guide_kwargs.pop("pa")

        self.separation = None

        if self.status & GuiderStatus.NON_IDLE:
            raise GortGuiderError(
                "Guider is not IDLE",
                error_code=ErrorCode.COMMAND_FAILED,
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
                raise GortGuiderError(
                    f"Invalid pixel name {pixel!r}.",
                    error_code=ErrorCode.INVALID_PIXEL_NAME,
                )
            pixel = config["guiders"]["devices"][self.name]["named_pixels"][pixel]

        log_msg = f"Guiding at RA={ra:.6f}, Dec={dec:.6f}"
        if pixel is not None:
            log_msg += f", pixel=({pixel[0]:.1f}, {pixel[1]:.1f})."
        self.write_to_log(log_msg, level="info")

        try:
            if monitor:
                self.guider_monitor.start_monitoring()
                if output_monitor_data:
                    monitor_task = asyncio.create_task(self._monitor_task())

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
            await cancel_task(monitor_task)

    async def _monitor_task(self, timeout: float = 30):
        """Monitors guiding and reports average and last guide metrics."""

        while True:
            try:
                await asyncio.sleep(timeout)

                # Get updated data
                df = self.guider_monitor.get_dataframe()
                if df is None:
                    continue

                df = df.filter(polars.col.telescope == self.name)

                # Select columns.
                df = df.select(
                    [
                        "frameno",
                        "time",
                        "n_sources",
                        "focus_position",
                        "fwhm",
                        "ra",
                        "dec",
                        "ra_offset",
                        "dec_offset",
                        "separation",
                        "mode",
                    ]
                )

                # Remove NaN rows.
                df = df.drop_nulls()

                now = datetime.now(UTC)
                time_range = now - timedelta(seconds=timeout)
                time_data = df.filter(polars.col.time > time_range).sort("time")

                if (
                    len(time_data) == 0
                    or "fwhm" not in time_data
                    or "separation" not in time_data
                ):
                    continue

                # Calculate and report last.
                last = time_data.tail(1)
                sep_last = round(last["separation"][0], 3)
                fwhm_last = round(last["fwhm"][0], 2)
                mode_last = last["mode"][0]
                self.write_to_log(
                    f"Last: sep={sep_last} arcsec; fwhm={fwhm_last} arcsec; "
                    f"mode={mode_last!r}",
                    "info",
                )

                # Calculate and report averages.
                sep_avg = round(time_data["separation"].mean(), 3)  # type: ignore
                fwhm_avg = round(time_data["fwhm"].mean(), 2)  # type: ignore
                self.write_to_log(
                    f"Average ({timeout} s): sep={sep_avg} arcsec; "
                    f"fwhm={fwhm_avg} arcsec",
                    "info",
                )

            except asyncio.CancelledError:
                return

            except Exception as err:
                self.write_to_log(f"Error in guider monitor: {err}", "warning")

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
        self.guider_monitor.stop_monitoring()

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
                raise GortGuiderError(
                    f"Invalid pixel name {pixel!r}.",
                    error_code=ErrorCode.INVALID_PIXEL_NAME,
                )
            pixel = config["guiders"]["devices"][self.name]["named_pixels"][pixel]

        if pixel is None:
            await self.actor.commands.reset_pixel()
        else:
            await self.actor.commands.set_pixel(*pixel)

    async def apply_corrections(self, enable: bool = True):
        """Enable/disable corrections being applied to the axes."""

        await self.actor.commands.corrections(mode="enable" if enable else "disable")

    async def monitor(
        self,
        ra: float | None = None,
        dec: float | None = None,
        exposure_time: float = 5.0,
        sleep: float = 60,
        monitor: bool = True,
    ):
        """Guides at a given position, sleeping between exposures.

        This is a convenience function mainly to monitor transparency during bad
        weather conditions. The telescope will be slewed to a given position
        (default to zenith) and guide with a low cadence. This results in the
        guider keywords, including transparency and FWHM, being output and the
        plots in Grafana being updated.

        After cancelling the monitoring make sure to stop the guiders with the
        :obj:`.Guider.stop` method.

        Parameter
        ---------
        ra,dec
            The coordinates to acquire. If :obj:`None`, the current zenith
            coordinates are used.
        exposure_time
            The exposure time of the AG integrations.
        sleep
            The time to sleep between exposures (seconds).
        monitor
            Start the guider monitor task. Data collected during the monitoring
            can be access as a :obj:`polars.DataFrame` from
            :obj:`Guider.guider_monitor.get_dataframe() <.GuiderMonitor.get_dataframe>`.

        """

        if ra is None and dec is None:
            await self.telescope.goto_named_position("zenith", altaz_tracking=True)

            # Get approximate RA/Dec. It doesn't really matter, we just want to guide
            # on a field that's close to zenith.
            tel_status = await self.telescope.status()
            ra = tel_status.get("ra_j2000_hours")
            dec = tel_status.get("dec_j2000_degs")

            if ra is None or dec is None:
                raise GortGuiderError("Cannot determine telescope RA/Dec.")
            ra *= 15.0

        elif (ra is None and dec is not None) or (ra is not None and dec is None):
            raise ValueError("Both RA and Dec need to be provided.")

        # Even if we already went to zenith in alt/az we need to go to these
        # coordinates again to make sure the kmirror is set.
        await self.telescope.goto_coordinates(ra, dec)

        await self.guide(
            ra=ra,
            dec=dec,
            exposure_time=exposure_time,
            sleep=sleep,
            monitor=monitor,
            output_monitor_data=False,
        )

    async def get_focus_info(self):
        """Returns the guider focus information."""

        info_reply: ActorReply = await self.actor.commands.focus_info()
        info = info_reply.flatten()

        for key in ["reference_focus", "current_focus"]:
            focus_ts = info[key]["timestamp"]

            if focus_ts is None:
                continue

            time = focus_ts.split("T")[1]
            if not focus_ts.endswith("Z") and "+" not in time and "-" not in time:
                focus_ts += "Z"

            info[key]["timestamp"] = datetime.fromisoformat(focus_ts)

        now = info["current_focus"]["timestamp"]
        ref_ts = info["reference_focus"]["timestamp"]

        if now is not None and ref_ts is not None:
            age = (now - ref_ts).total_seconds()
        else:
            age = None

        info["reference_focus"]["age"] = age

        return info


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
            The initial guesses for focuser position. If :obj:`None`, an estimate
            based on the current bench temperatures is used. It can also be a float
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
                sweep=True,
                guess=guess_dict.get(guider_name, None),
                step_size=step_size,
                steps=steps,
                exposure_time=exposure_time,
            )
            for guider_name in self
        ]
        results = await asyncio.gather(*jobs)

        best_focus: list[str] = []
        error: bool = False
        for itel, name in enumerate(self):
            result = results[itel]
            if result is None:
                continue

            best_focus.append(f"{name}: {result[1]}")

            if any(result) < 0:
                error = True

        self.write_to_log("Best focus: " + ", ".join(best_focus), "info")

        if error:
            self.write_to_log("One or more focus values are invalid.", "error")
            return False

        return True

    async def adjust_focus(self):
        """Adjusts the focus position based on the current bench temperature."""

        await asyncio.gather(*[self[gname].adjust_focus() for gname in self])

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

    async def monitor(self, *args, **kwargs):
        """Guides at a given position, sleeping between exposures.

        See :obj:`.Guider.monitor` for details.

        """

        await self.call_device_method(Guider.monitor, *args, **kwargs)

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
