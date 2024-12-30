#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-27
# @Filename: observer.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from time import time

from typing import TYPE_CHECKING

from astropy.time import Time

from gort.exceptions import GortError, OverwatcherError, TroubleshooterTimeoutError
from gort.exposure import Exposure
from gort.overwatcher import OverwatcherModule
from gort.overwatcher.core import OverwatcherModuleTask
from gort.overwatcher.transparency import TransparencyQuality
from gort.tile import Tile
from gort.tools import cancel_task, decap, run_in_executor


if TYPE_CHECKING:
    pass


__all__ = ["ObserverOverwatcher"]


class ObserverMonitorTask(OverwatcherModuleTask["ObserverOverwatcher"]):
    """Monitors the observer status."""

    name = "observer_monitor"
    keep_alive = False
    restart_on_error = True

    interval: float = 1

    async def task(self):
        """Handles whether we should start the observing loop."""

        # These checks will only start the observing loop. If the weather is
        # unsafe or daytime has been reached the main task will handle stopping
        # the loop.

        state = self.overwatcher.state

        OPEN_DOME_SECS_BEFORE_TWILIGHT = self.module.OPEN_DOME_SECS_BEFORE_TWILIGHT
        STOP_SECS_BEFORE_MORNING = self.module.STOP_SECS_BEFORE_MORNING

        while True:
            ephemeris = self.overwatcher.ephemeris.ephemeris

            if state.dry_run:
                pass

            elif self.module.is_observing or self.module.is_cancelling:
                pass

            elif not state.enabled or state.troubleshooting:
                pass

            elif not ephemeris:
                pass

            elif state.calibrating:
                pass

            elif state.safe and state.night:
                # Not open within STOP_SECS_BEFORE_MORNING minutes of morning twilight.

                now = time()
                morning_twilight = Time(ephemeris.twilight_start, format="jd").unix
                time_to_morning_twilight = morning_twilight - now

                if time_to_morning_twilight < STOP_SECS_BEFORE_MORNING:
                    await asyncio.sleep(self.interval)
                    continue

                try:
                    await self.module.start_observing()
                except Exception as err:
                    await self.notify(
                        "An error occurred while starting the "
                        f"observing loop: {decap(err)}",
                        level="error",
                    )
                    await asyncio.sleep(15)

            elif state.safe and not state.night and not state.calibrating:
                # Open the dome OPEN_DOME_SECS_BEFORE_TWILIGHT before evening twilight.

                now = time()
                evening_twilight = Time(ephemeris.twilight_end, format="jd").unix
                time_to_evening_twilight = evening_twilight - now

                if (
                    time_to_evening_twilight > 0
                    and time_to_evening_twilight < OPEN_DOME_SECS_BEFORE_TWILIGHT
                ):
                    dome_open = await self.overwatcher.dome.is_opening()
                    if not dome_open:
                        await self.notify(
                            "Running the start-up sequence and "
                            "opening the dome for observing."
                        )
                        await self.overwatcher.dome.startup()

            await asyncio.sleep(self.interval)


class ObserverOverwatcher(OverwatcherModule):
    name = "observer"
    delay = 5

    tasks = [ObserverMonitorTask()]

    OPEN_DOME_SECS_BEFORE_TWILIGHT: float = 300
    STOP_SECS_BEFORE_MORNING: float = 600

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.observe_loop: asyncio.Task | None = None
        self.next_exposure_completes: float = 0

        self.focusing: bool = False
        self._starting_observations: bool = False
        self._cancelling: bool = False

        self.force_focus: bool = False  # Force focus before the next tile

    @property
    def is_observing(self) -> bool:
        """Returns whether the observer is currently observing."""

        if self._starting_observations:
            return True

        return self.observe_loop is not None and not self.observe_loop.done()

    @property
    def is_cancelling(self) -> bool:
        """Returns whether the observer is currently cancelling."""

        if not self.is_observing:
            self._cancelling = False
            return False

        return self._cancelling

    def cancel(self):
        """Requests the cancellation of the observing loop."""

        if self.is_observing and not self.is_cancelling:
            self._cancelling = True
            self.gort.observer.cancelling = True

    async def start_observing(self):
        """Starts observations."""

        if self.overwatcher.state.dry_run:
            return

        if self.is_observing or self.is_cancelling:
            return

        if self.overwatcher.state.calibrating:
            raise GortError("Cannot start observing while calibrating.")

        if not self.overwatcher.state.safe:
            raise GortError("Cannot safely open the telescope.")

        await self.notify("Starting observations.")

        if not (await self.overwatcher.dome.is_opening()):
            try:
                self._starting_observations = True
                await self.notify("Running the start-up sequence and opening the dome.")
                await self.overwatcher.dome.startup()
            except Exception:
                # If the dome failed to open that will disable the
                # Overwatcher and prevent the startup to run again.
                self.log.error("Startup routine failed.")
                return
            finally:
                self._starting_observations = False

        self.observe_loop = asyncio.create_task(self.observe_loop_task())
        await asyncio.sleep(1)

    async def stop_observing(
        self,
        immediate: bool = False,
        reason: str = "undefined",
        block: bool = False,
    ):
        """Stops observations."""

        notify = self.overwatcher.notify

        if self.overwatcher.state.dry_run:
            return

        if not self.is_observing:
            return

        self.cancel()

        if not reason.endswith("."):
            reason += "."

        if immediate:
            await notify(f"Stopping observations immediately. Reason: {decap(reason)}")
            self.observe_loop = await cancel_task(self.observe_loop)

            # The guiders may have been left running or the spectrograph may still
            # be exposing. Clean up to avoid issues.
            await self.gort.cleanup(readout=False)

        else:
            await notify(
                "Stopping observations after this tile completes. "
                f"Reason: {decap(reason)}"
            )

        if block and self.observe_loop and not self.observe_loop.done():
            await self.observe_loop
            self.observe_loop = None

    async def observe_loop_task(self):
        """Runs the observing loop."""

        await self.gort.cleanup(readout=True)
        observer = self.gort.observer

        n_tile_positions = 0

        while True:
            try:
                # Wait in case the troubleshooter is doing something.
                await self.overwatcher.troubleshooter.wait_until_ready(300)

                # We want to avoid re-acquiring the tile between dithers. We call
                # the scheduler here and control the dither position loop ourselves.
                tile: Tile = await run_in_executor(Tile.from_scheduler)

                self.log.info(
                    f"Received tile {tile.tile_id} from scheduler: "
                    f"observing dither positions {tile.dither_positions}."
                )

                await self.check_focus(force=n_tile_positions == 0 or self.force_focus)

                for dpos in tile.dither_positions:
                    exp: Exposure | bool = False

                    await self.overwatcher.troubleshooter.wait_until_ready(300)

                    if not self.check_twilight():
                        await self.notify(
                            "Morning twilight will be reached before the next "
                            "exposure ends. Finishing the observing loop and "
                            "closing now."
                        )

                        self.cancel()
                        await self.overwatcher.dome.shutdown(retry=True, park=True)

                        break

                    # The exposure will complete in 900 seconds + acquisition + readout
                    self.next_exposure_completes = time() + 90 + 900 + 60

                    if not (await self.pre_observe_checks()):
                        raise OverwatcherError("Pre-observe checks failed.")

                    result, exps = await observer.observe_tile(
                        tile=tile,
                        dither_position=dpos,
                        async_readout=True,
                        keep_guiding=True,
                        skip_slew_when_acquired=True,
                        run_cleanup=False,
                        cleanup_on_interrupt=True,
                        show_progress=False,
                    )

                    n_tile_positions += 1

                    if not result and not self.is_cancelling:
                        raise GortError("The observation ended with error state.")

                    if result and len(exps) > 0:
                        exp = exps[0]

                    try:
                        await self.post_exposure(exp)
                    except Exception as err:
                        await self.notify(
                            f"Failed to run post-exposure routine: {decap(err)}",
                            level="error",
                        )

                    if self.is_cancelling:
                        break

            except asyncio.CancelledError:
                break

            except TroubleshooterTimeoutError:
                await self.notify(
                    "The troubleshooter timed out after 300 seconds. "
                    "Cancelling observations.",
                    level="critical",
                )
                break

            except Exception as err:
                if await self.overwatcher.troubleshooter.handle(err):
                    if self.is_cancelling:
                        break
                    if self.focusing:
                        # Force a new focus if the error occurred while focusing.
                        self.force_focus = True

                    continue

            finally:
                self.exposure_completes = 0

                if self.is_cancelling:
                    try:
                        if exp is not False and not exp.done():
                            self.log.warning("Waiting for exposure to read.")
                            await asyncio.wait_for(exp, timeout=80)
                    except Exception:
                        self.log.error("Failed reading last exposure.")

                    break

        await self.gort.cleanup(readout=False)
        await self.notify("The observing loop has ended.")

    def check_twilight(self) -> bool:
        """Checks if we are close to the morning twilight and cancels observations."""

        ephemeris = self.overwatcher.ephemeris
        time_to_twilight = ephemeris.time_to_morning_twilight()

        if time_to_twilight is None:
            self.log.warning("Failed to get time to morning twilight.")
            return True

        if time_to_twilight > self.STOP_SECS_BEFORE_MORNING:
            return True

        return False

    async def check_focus(self, force: bool = False):
        """Checks if it's time to focus the telescope."""

        should_focus: bool = False

        if not force:
            # Check if we should refocus.
            focus_info = await self.gort.guiders.sci.get_focus_info()
            focus_age = focus_info["reference_focus"]["age"]

            if focus_age is None or focus_age > 2 * 3600:  # Focus sweeps every 2 hours
                should_focus = True

        else:
            should_focus = True

        # Focus when the loop starts or every 1 hour or at the beginning
        # of the loop.
        if should_focus:
            try:
                self.focusing = True
                await self.notify("Focusing telescopes.")
                await self.gort.guiders.focus()
            except Exception as err:
                await self.notify(
                    f"Failed while focusing the telescopes: {decap(err)}",
                    level="error",
                )
                raise
            else:
                self.focusing = False
                self.force_focus = False

    async def pre_observe_checks(self):
        """Runs pre-observe checks."""

        if await self.gort.specs.are_errored():
            exp = self.gort.specs.last_exposure
            if exp and not exp.done():
                self.log.warning(
                    "Spectrographs are idle but an exposure is ongoing. "
                    "Waiting for it to finish before resetting."
                )
                try:
                    await asyncio.wait_for(exp, timeout=100)
                except asyncio.TimeoutError:
                    self.log.error(
                        "Timed out waiting for exposure to finish. "
                        "Resetting spectrographs."
                    )

            await self.gort.specs.reset()

        return True

    async def post_exposure(self, exp: Exposure | bool):
        """Runs post-exposure checks."""

        if self._cancelling:
            return

        if exp is False:
            raise GortError("No exposure was returned.")

        # Output transparency data for the last exposure.
        transparency = self.overwatcher.transparency
        transparency.write_to_log(["sci"])

        # TODO: disable actions based on transparency quality for now.
        return

        if transparency.quality["sci"] & TransparencyQuality.BAD:
            await self.notify(
                "Transparency is bad. Stopping observations and starting "
                "the transparency monitor.",
            )

            # If we reach twilight this will cause the overwatcher
            # to immediately stop observations.
            self.exposure_completes = 0

            try:
                await asyncio.wait_for(
                    transparency.start_monitoring(),
                    timeout=3600,
                )

            except asyncio.TimeoutError:
                await self.notify("Transparency monitor timed out.", level="warning")
                await self.overwatcher.shutdown(
                    reason="Transparency has been bad for over one hour.",
                    disable_overwatcher=True,
                )

            else:
                # The transparency monitor has ended. There are two possible reasons:

                # - Something stopped the observing loop and with it the monitor.
                #   Do nothing and return. The main task will handle the rest.
                if self._cancelling:
                    return

                # - The transparency is good and the monitor has ended.
                if transparency.quality["sci"] & TransparencyQuality.GOOD:
                    await self.notify("Transparency is good. Resuming observations.")
                    return

                else:
                    await self.notify(
                        "Transparency is still bad but the monitor stopped. "
                        "Triggering shutdown.",
                    )
                    await self.overwatcher.shutdown(
                        reason="Transparency monitor failed.",
                        disable_overwatcher=True,
                    )
