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

from typing import TYPE_CHECKING, TypedDict, cast

from lvmopstools.retrier import Retrier

from gort.enums import ErrorCode
from gort.exceptions import (
    GortError,
    TroubleshooterCriticalError,
    TroubleshooterTimeoutError,
)
from gort.exposure import Exposure
from gort.overwatcher import OverwatcherModule
from gort.overwatcher.core import OverwatcherModuleTask
from gort.overwatcher.transparency import TransparencyQuality
from gort.overwatcher.troubleshooter.recipes import AcquisitionFailedRecipe
from gort.tile import Tile
from gort.tools import (
    cancel_task,
    decap,
    ensure_period,
    record_overheads,
    redis_client_sync,
    run_in_executor,
)


if TYPE_CHECKING:
    pass


__all__ = ["ObserverOverwatcher"]


class ObserverRedisDict(TypedDict):
    """Datamodel for the observer data stored in Redis."""

    focused: bool


class CancelObserveLoopError(GortError):
    """Exception that cancel the observing loop."""

    def __init__(self, message: str, shutdown: bool = False):
        self.shutdown = shutdown
        super().__init__(message)


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
        ephemeris = self.overwatcher.ephemeris

        while True:
            if state.dry_run:
                pass

            elif self.module.is_observing or self.module.is_cancelling:
                pass

            elif not state.enabled or state.troubleshooting:
                pass

            elif not ephemeris.ephemeris:
                pass

            elif state.calibrating:
                pass

            elif not state.safe:
                pass

            elif ephemeris.is_night(mode="observer"):
                # Start observing if it's night (after evening twilight) but not too
                # close to the morning twilight.
                try:
                    await self.module.start_observing()
                except Exception as err:
                    await self.notify(
                        "An error occurred while starting the "
                        f"observing loop: {decap(err)}",
                        level="error",
                    )
                    await asyncio.sleep(15)

            await asyncio.sleep(self.interval)


class ObserverOverwatcher(OverwatcherModule):
    name = "observer"
    delay = 5

    tasks = [ObserverMonitorTask()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.observe_loop: asyncio.Task | None = None
        self.next_exposure_completes: float = 0

        self.focusing: bool = False
        self.mjd_focus: bool = False  # Have we focus at least once this MJD

        self._starting_observations: bool = False
        self._cancelling: bool = False
        self._schedule_shutdown: bool = False

        self.force_focus: bool = False  # Force focus before the next tile

    async def reset(self):
        """Resets the observer module."""

        # Check if we have already focused this MJD.
        if (sjd := self.overwatcher.ephemeris.sjd) is not None:
            with redis_client_sync() as redis:
                observer_data = redis.json().get(f"overwatcher:observer:{sjd}")
                observer_data = cast(ObserverRedisDict | None, observer_data)

                if observer_data is None:
                    self.mjd_focus = False
                    redis.json().set(
                        f"overwatcher:observer:{sjd}",
                        "$",
                        {"focused": False},
                    )
                else:
                    self.mjd_focus = observer_data.get("focused", False)

        else:
            self.log.warning("Cannot get SJD from ephemeris. Assuming focus not done.")
            self.mjd_focus = False

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

        await self.overwatcher.troubleshooter.reset(clear_all_tracking=True)

        if not (await self.overwatcher.dome.is_opening()):
            try:
                self._starting_observations = True
                await self.notify("Running the start-up sequence and opening the dome.")
                await self.overwatcher.startup()
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
        reason: str | None = None,
        block: bool = False,
    ):
        """Stops observations."""

        if self.overwatcher.state.dry_run:
            return

        if not self.is_observing:
            return

        # Prepare the notification message.
        msg = "Stopping observations "
        if immediate:
            msg += "immediately."
        else:
            msg += "after the current tile completes."

        if reason is not None:
            msg += f" Reason: {decap(reason)}"

        await self.overwatcher.notify(ensure_period(msg), level="info")

        # Cancel the observing loop.
        self.cancel()

        # If immediate, cancel the task now and cleanup.
        if immediate:
            if self._starting_observations:
                # Just let the startup finish. observe_loop_task() will immediately
                # return because we have set _is_cancelling=True.
                await self.notify("Waiting for the start-up sequence to finish.")
                if block:
                    while True:
                        if not self._starting_observations:
                            break
                        await asyncio.sleep(1)
                return

            try:
                await cancel_task(self.observe_loop)
            except Exception as err:
                self.log.error(f"Error while cancelling observing loop: {decap(err)}")
            finally:
                self.observe_loop = None

            # The guiders may have been left running or the spectrograph may still
            # be exposing. Clean up to avoid issues.
            await self.gort.cleanup(readout=False)

        # If block, wait for the observing loop to finish.
        if block and self.observe_loop and not self.observe_loop.done():
            await self.observe_loop
            self.observe_loop = None

        # We are not troubleshooting any more if we have stopped the loop, but if we
        # cancelled the task the troubleshooter event may still be cleared.
        await self.overwatcher.troubleshooter.reset()

    async def get_next_tile(
        self,
        wait: bool = True,
        max_wait: float | None = None,
    ) -> Tile:
        """Gets the next tile from the scheduler."""

        t0 = time()
        last_notification: float | None = None
        notification_interval: float = 600

        while True:
            try:
                tile = await run_in_executor(Tile.from_scheduler)

            except GortError as err:
                # If the error is not related to not being able to find a valid
                # tile, just raise the error and let the observe loop handle it.
                if err.error_code != ErrorCode.SCHEDULER_CANNOT_FIND_TILE or not wait:
                    raise

                self.log.warning("The scheduler cannot find a valid tile to observe.")

                t1 = time()
                if max_wait is not None and (t1 - t0) > max_wait:
                    raise CancelObserveLoopError(
                        "The scheduler cannot find a valid tile and the wait "
                        "time has been exhausted. Cancelling the observe loop "
                        "and closing the dome.",
                        shutdown=True,
                    )

                if last_notification is None:
                    await self.notify(
                        "The scheduler was unable to find a valid tile to observe. "
                        "Will continue trying to get a new tile."
                    )
                    last_notification = t1
                elif t1 - last_notification > notification_interval:
                    await self.notify(
                        "Still unable to find a valid tile to observe. "
                        "Will continue trying to get a new tile."
                    )
                    last_notification = t1

                self.log.info("Waiting 60s before trying to find a new tile.")
                await asyncio.sleep(60)

                # Record an overhead for the time we spent waiting.
                try:
                    now = time()
                    record_overheads(
                        [
                            {
                                "observer_id": None,
                                "tile_id": None,
                                "dither_position": None,
                                "stage": None,
                                "start_time": now - 60,
                                "end_time": now,
                                "duration": 60,
                            }
                        ]
                    )
                except Exception as err:
                    self.log.error(f"Failed to record overheads: {err}")

            else:
                return tile

    async def observe_loop_task(self):
        """Runs the observing loop."""

        # Immediately return if we are cancelling. This can happen if we disable
        # the overwatcher during startup.
        if self.is_cancelling:
            return

        # Check that we have not started too early (this happens when we open the dome
        # a bit early to avoid wasting time). If so, wait until we are properly in
        # the observing window.
        if not self.overwatcher.ephemeris.is_night():
            time_to_twilight = self.overwatcher.ephemeris.time_to_evening_twilight()
            if time_to_twilight and time_to_twilight > 0:
                await self.notify(
                    f"Waiting until evening twilight to start observing "
                    f"({time_to_twilight:.0f} seconds)."
                )
                await asyncio.sleep(time_to_twilight)

        await self.gort.cleanup(readout=True)
        observer = self.gort.observer

        n_tile_positions = 0
        self._schedule_shutdown = False

        while True:
            try:
                # Wait in case the troubleshooter is doing something.
                await self.overwatcher.troubleshooter.wait_until_ready(300)

                # We want to avoid re-acquiring the tile between dithers. We call
                # the scheduler here and control the dither position loop ourselves.
                tile: Tile = await self.get_next_tile()

                self.log.info(
                    f"Received tile {tile.tile_id} from scheduler: "
                    f"observing dither positions {tile.dither_positions}."
                )

                if self.is_cancelling:
                    break

                if await self.should_focus(force=self.force_focus):
                    await self.focus_sweep()

                for dpos in tile.dither_positions:
                    exp: Exposure | bool = False

                    await self.overwatcher.troubleshooter.wait_until_ready(300)

                    if not self.overwatcher.ephemeris.is_night(mode="observer"):
                        await self.notify(
                            "Twilight will be reached before the next exposure "
                            "completes. Stopping observations now."
                        )
                        self.cancel()
                        self._schedule_shutdown = True
                        break

                    # The exposure will complete in 900 seconds + acquisition + readout
                    self.next_exposure_completes = time() + 90 + 900 + 60

                    if not (await self.pre_observe_checks()):
                        raise CancelObserveLoopError("Pre-observe checks failed.")

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

                    # Clear counts of errors that are reset
                    # when a tile is successfully observed.
                    await self.overwatcher.troubleshooter.reset()

                    if not result and not self.is_cancelling:
                        raise GortError("The observation ended with error state.")

                    if result and len(exps) > 0:
                        exp = exps[0]

                    try:
                        post_exposure_status = await self.post_exposure(exp)
                    except Exception as err:
                        await self.notify(
                            f"Failed to run post-exposure routine: {decap(err)}",
                            level="error",
                        )
                        post_exposure_status = False

                    if self.is_cancelling or not post_exposure_status:
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

            except CancelObserveLoopError as err:
                await self.notify(
                    f"Cancelling observations: {decap(err)}",
                    level="error",
                )

                if err.shutdown:
                    self._schedule_shutdown = True

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

        if self._schedule_shutdown:
            # Do not set shutdown_pending until here to prevent the
            # main overwatcher task cancelling the last exposure and
            # various recursion problems.
            self.overwatcher.state.shutdown_pending = True

    async def should_focus(self, force: bool = False, is_error: bool = False) -> bool:
        """Determines whether we should perform a focus sweep."""

        if force:
            return True

        focus_config = self.overwatcher.config["overwatcher.observer.focus"]

        focus_every = focus_config["every"]
        require_mjd_sweep = focus_config["require_mjd_sweep"]
        on_error = focus_config["on_error"]

        if is_error and on_error:
            return True

        if focus_every <= 0:
            # Do not run a focus sweep ever, except maybe once per MJD.
            if require_mjd_sweep:
                # Check if we have already done an initial focus.
                return not self.mjd_focus
            else:
                return False

        # Check if it's been long enough to run another focus sweep.
        focus_info = await self.gort.guiders.sci.get_focus_info()
        focus_age = focus_info["reference_focus"]["age"]

        if self.mjd_focus is False or focus_age is None or focus_age > focus_every:
            return True

        return False

    async def focus_sweep(self):
        """Performs a focus sweep."""

        # Retry focusing up to 2 times with a 10 second delay and a 150 second timeout.
        focus_retrier = Retrier(max_attempts=2, delay=10, timeout=150)

        try:
            self.focusing = True
            await self.notify("Focusing telescopes.")
            await focus_retrier(self.gort.guiders.focus)()
        except Exception as err:
            await self.notify(
                f"Failed twice while focusing the telescopes: {decap(err)}",
                level="error",
            )
            return False
        finally:
            self.force_focus = False
            self.focusing = False

        # Indicate that we have performed a focus sweep this MJD.
        if not self.mjd_focus:
            try:
                self.mjd_focus = True
                with redis_client_sync() as redis:
                    if (sjd := self.overwatcher.ephemeris.sjd) is not None:
                        redis.json().set(
                            f"overwatcher:observer:{sjd}",
                            "$.focused",
                            True,
                        )
            except Exception as err:
                self.log.error(f"Failed to update Redis with MJD focus: {decap(err)}")

        return True

    async def pre_observe_checks(self) -> float:
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

        # Use the code in the troubleshooter to check if all AG cameras are connected.
        acq_failed_recipe = AcquisitionFailedRecipe(self.overwatcher.troubleshooter)

        self.log.debug("Checking AG cameras.")
        ag_pings = await acq_failed_recipe.ping_ag_cameras()
        cameras_alive = await acq_failed_recipe.all_cameras_alive()

        if not all(ag_pings.values()) or not cameras_alive:
            self.log.error("Not all AG cameras ping. Running troubleshooting recipe.")
            try:
                await acq_failed_recipe._handle_disconnected_cameras()
            except TroubleshooterCriticalError as err:
                await self.notify(
                    f"Critical error while checking AG cameras: {decap(err)}",
                    level="error",
                )
                return False

        return True

    async def post_exposure(self, exp: Exposure | bool):
        """Runs post-exposure checks."""

        if self._cancelling:
            return False

        if exp is False:
            raise GortError("No exposure was returned.")

        # Output transparency data for the last exposure.
        transparency = self.overwatcher.transparency
        transparency.write_to_log(["sci"])

        # TODO: for now if the transparency is bad we close and disable the overwatcher.
        # Since we are inside the observer loop we cannot just call shutdown() or
        # we'll get a recursion error, so we cancel the loop and schedule the shutdown.
        if transparency.quality["sci"] & TransparencyQuality.BAD:
            self.cancel()
            await self.notify(
                "Transparency is bad. Stopping observations and closing the "
                "dome. Resume observations manually when the transparency is deemed "
                "good.",
                level="warning",
            )
            self._schedule_shutdown = True

            return False

        return True

        # TODO: monitor transparency and resume observing automatically.
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
