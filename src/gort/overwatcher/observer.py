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

from gort.enums import ErrorCode
from gort.exceptions import GortError
from gort.exposure import Exposure
from gort.overwatcher import OverwatcherModule
from gort.overwatcher.core import OverwatcherModuleTask
from gort.tile import Tile
from gort.tools import cancel_task, run_in_executor, set_tile_status


if TYPE_CHECKING:
    pass


__all__ = ["ObserverOverwatcher"]


class ObserverMonitorTask(OverwatcherModuleTask["ObserverOverwatcher"]):
    """Monitors the observer status."""

    name = "observer_monitor"
    keep_alive = False
    restart_on_error = True

    async def task(self):
        """Handles whether we should start the observing loop."""

        # These checks will only start the observing loop. If the weather is
        # unsafe or daytime has been reached the main task will handle stopping
        # the loop.

        state = self.overwatcher.state
        notify = self.overwatcher.notify

        while True:
            if state.dry_run:
                pass

            elif self.module.is_observing or self.module.is_cancelling:
                pass

            elif not state.enabled:
                pass

            elif state.safe and state.night:
                try:
                    await self.module.start_observing()
                except Exception as err:
                    await notify(
                        f"An error occurred while starting the observing loop: {err}",
                        level="error",
                    )
                    await asyncio.sleep(15)

            elif state.safe and not state.night:
                ephemeris = self.overwatcher.ephemeris.ephemeris

                if ephemeris:
                    now = time()
                    twilight_time = Time(ephemeris.twilight_end, format="jd").unix

                    time_to_twilight = twilight_time - now

                    # Open the dome 5 minutes before twilight.
                    if time_to_twilight > 0 and time_to_twilight < 300:
                        dome_open = await self.overwatcher.dome.is_opening()
                        if not dome_open:
                            await notify("Opening the dome for observing.")
                            await self.overwatcher.dome.startup()

            await asyncio.sleep(1)


class ObserverOverwatcher(OverwatcherModule):
    name = "observer"
    delay = 5

    tasks = [ObserverMonitorTask()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.observe_loop: asyncio.Task | None = None
        self.next_exposure_completes: float = 0

        self._starting_observations: bool = False
        self._cancelling: bool = False

    @property
    def is_observing(self):
        """Returns whether the observer is currently observing."""

        if self._starting_observations:
            return True

        return self.observe_loop is not None and not self.observe_loop.done()

    @property
    def is_cancelling(self):
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

        if not self.overwatcher.state.safe:
            raise GortError("Cannot safely open the telescope.")

        await self.overwatcher.write_to_slack("Starting observations.")
        self._starting_observations = True

        if not (await self.overwatcher.dome.is_opening()):
            await self.overwatcher.dome.startup()

        self.observe_loop = asyncio.create_task(self.observe_loop_task())
        self._starting_observations = False

    async def stop_observing(
        self,
        immediate: bool = False,
        reason: str = "undefined",
        block: bool = False,
    ):
        """Stops observations."""

        if self.overwatcher.state.dry_run:
            return

        if not self.is_observing:
            return

        self.cancel()

        if not reason.endswith("."):
            reason += "."

        if immediate:
            await self.overwatcher.notify(
                f"Stopping observations immediately. Reason: {reason}"
            )
            self.observe_loop = await cancel_task(self.observe_loop)

        else:
            await self.overwatcher.notify(
                f"Stopping observations after this tile. Reason: {reason}"
            )

        if block and self.observe_loop and not self.observe_loop.done():
            await self.observe_loop
            self.observe_loop = None

    async def observe_loop_task(self):
        """Runs the observing loop."""

        await self.gort.cleanup(readout=True)
        observer = self.gort.observer

        notify = self.overwatcher.notify

        n_tile_positions = 0

        while True:
            exp: Exposure | list[Exposure] | bool = False

            try:
                # We want to avoid re-acquiring the tile between dithers. We call
                # the scheduler here and control the dither position loop ourselves.
                tile: Tile = await run_in_executor(Tile.from_scheduler)

                self.log.info(
                    f"Received tile {tile.tile_id} from scheduler: "
                    f"observing dither positions {tile.dither_positions}."
                )

                # Check if we should refocus.
                focus_info = await self.gort.guiders.sci.get_focus_info()
                focus_age = focus_info["reference_focus"]["age"]

                # Focus when the loop starts or every 1 hour or at the beginning
                # of the loop.
                if n_tile_positions == 0 or focus_age is None or focus_age > 3600.0:
                    await notify("Focusing telescopes.")
                    await self.gort.guiders.focus()

                for dpos in tile.dither_positions:
                    # The exposure will complete in 900 seconds + acquisition + readout
                    self.next_exposure_completes = time() + 90 + 900 + 60

                    result, _ = await observer.observe_tile(
                        tile=tile,
                        dither_position=dpos,
                        keep_guiding=True,
                        skip_slew_when_acquired=True,
                        run_cleanup=False,
                        cleanup_on_interrupt=True,
                        show_progress=False,
                    )

                    n_tile_positions += 1

                    if not result and not self.is_cancelling:
                        raise GortError("The observation ended with error state.")

                    if self.is_cancelling:
                        break

            except asyncio.CancelledError:
                break

            except Exception as err:
                # TODO: this should be moved to the troubleshooting module, but
                # for now handling it here.

                if isinstance(err, GortError):
                    # If the acquisition failed, disable the tile and try again.
                    if err.error_code == ErrorCode.ACQUISITION_FAILED:
                        tile_id: int | None = err.payload.get("tile_id", None)
                        if tile_id is None:
                            await notify(
                                'Cannot disable tile without a "tile_id. '
                                "Continuing observations without disabling tile.",
                                level="error",
                            )
                        else:
                            await set_tile_status(tile_id, enabled=False)
                            await notify(
                                f"tile_id={tile_id} has been disabled. "
                                "Continuing observations.",
                                level="warning",
                            )

                    # If the scheduler cannot find a tile, wait a minute and try again.
                    elif err.error_code == ErrorCode.SCHEDULER_CANNOT_FIND_TILE:
                        await notify(
                            "The scheduler was not able to find a valid tile to "
                            "observe. Waiting 60 seconds before trying again.",
                            level="warning",
                        )
                        await asyncio.sleep(60)
                        continue

                # No specific troubleshooting available. Report the error,
                # do a cleanup and try again.
                await notify(
                    f"An error occurred during the observation: {err} "
                    "Running the cleanup recipe.",
                    level="error",
                )
                await self.gort.cleanup(readout=False)

            finally:
                if self.is_cancelling:
                    self.log.warning("Cancelling observations.")
                    try:
                        if exp and isinstance(exp[0], Exposure):
                            await asyncio.wait_for(exp[0], timeout=80)
                    except Exception:
                        pass

                    break

        await self.gort.cleanup(readout=False)

        await notify("The observing loop has ended.")
