#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-27
# @Filename: observer.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import enum
from time import time

from typing import TYPE_CHECKING

from gort.exceptions import GortError
from gort.exposure import Exposure
from gort.overwatcher import OverwatcherModule
from gort.overwatcher.core import OverwatcherModuleTask
from gort.tools import cancel_task


if TYPE_CHECKING:
    pass


__all__ = ["ObserverOverwatcher"]


class ObserverStatus(enum.Flag):
    """An enumeration of observer statuses."""

    OBSERVING = 1 << 0
    CANCELLING = 1 << 1
    NIGHT = 1 << 2
    ENABLED = 1 << 3
    WEATHER_SAFE = 1 << 4

    def observing(self) -> bool:
        return bool(self & ObserverStatus.OBSERVING)

    def enabled(self) -> bool:
        return bool(self & ObserverStatus.ENABLED)

    def cancelling(self) -> bool:
        return bool(self & ObserverStatus.CANCELLING)

    def can_observe(self) -> bool:
        return bool(
            (self & self.NIGHT) and (self & self.ENABLED) and (self & self.WEATHER_SAFE)
        )


class ObserverMonitorTask(OverwatcherModuleTask["ObserverOverwatcher"]):
    """Monitors the observer status."""

    name = "observer_monitor"
    keep_alive = False
    restart_on_error = True

    @property
    def status(self):
        return self.module.status

    @status.setter
    def status(self, value: ObserverStatus):
        self.module.status = value

    async def task(self):
        """Handles whether we should start the observing loop."""

        while True:
            # Update the status of the observer.
            await self.update_status()

            # Now check if we should start/stop the observing loop. Note that these
            # will always call start/stop_observing, but those methods return quickly
            # if there's nothing to do.

            if self.overwatcher.state.dry_run:
                pass

            elif not (self.status & ObserverStatus.WEATHER_SAFE):
                await self.module.stop_observing(
                    immediate=True,
                    reason="weather not safe",
                )

            elif not (self.status & ObserverStatus.NIGHT):
                await self.module.stop_observing(
                    immediate=True,
                    reason="not night time",
                )

            elif not (self.status & ObserverStatus.ENABLED):
                await self.module.stop_observing(
                    reason="observing switch has been manually disabled",
                )

            elif self.status.can_observe() and not self.status.observing():
                await self.module.start_observing()

            await asyncio.sleep(1)

    async def update_status(self):
        """Updates the observer status."""

        new_status = ObserverStatus(0)

        # Set new status. Do not do any actions here, we'll evaluate actions to
        # take once the new status has been set.
        if (
            self.status.observing()
            and self.module.observe_loop
            and not self.module.observe_loop.done()
        ):
            new_status |= ObserverStatus.OBSERVING

        if new_status.observing() and self.status.cancelling():
            new_status |= ObserverStatus.CANCELLING

        if self.overwatcher.ephemeris.is_night():
            new_status |= ObserverStatus.NIGHT

        if self.overwatcher.state.enabled:
            new_status |= ObserverStatus.ENABLED

        if self.overwatcher.weather.is_safe():
            new_status |= ObserverStatus.WEATHER_SAFE

        # Warn about change in enabled status.
        if self.status.enabled() != new_status.enabled():
            if new_status.enabled():
                await self.overwatcher.notify("Overwatcher is now ENABLED.")
            else:
                await self.overwatcher.notify("Overwatcher is now DISABLED")

        self.status = new_status


class ObserverOverwatcher(OverwatcherModule):
    name = "observer"
    delay = 5

    tasks = [ObserverMonitorTask()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.status = ObserverStatus(0)
        self.observe_loop: asyncio.Task | None = None

    async def start_observing(self):
        """Starts observations."""

        if self.overwatcher.state.dry_run:
            return

        if self.status.observing():
            return

        weather = self.overwatcher.weather
        if not weather.is_running or not weather.is_safe():
            raise GortError("Cannot safely open the telescope.")

        await self.overwatcher.write_to_slack("Starting observations.")

        if not (await self.gort.enclosure.is_open()):
            # TODO: this should be an overwatcher function with exception wrapping.
            self.log.info("Opening the dome.")
            await self.gort.startup(confirm_open=False, focus=False)

        self.observe_loop = asyncio.create_task(self.observe_loop_task())
        self.status |= ObserverStatus.OBSERVING

    async def stop_observing(
        self,
        immediate: bool = False,
        reason: str = "undefined",
        block: bool = False,
    ):
        """Stops observations."""

        if self.overwatcher.state.dry_run:
            return

        if not self.status.observing():
            return

        if immediate:
            await self.overwatcher.notify(
                f"Stopping observations immediately. Reason: {reason}."
            )
            self.observe_loop = await cancel_task(self.observe_loop)

        elif not self.status.cancelling():
            await self.overwatcher.notify(
                f"Stopping observations after this tile: {reason}"
            )
            self.status |= ObserverStatus.CANCELLING
            self.gort.observer.cancelling = True

        if block and self.observe_loop and not self.observe_loop.done():
            await self.observe_loop

    async def observe_loop_task(self):
        """Runs the observing loop."""

        await self.gort.cleanup(readout=True)

        while True:
            # TODO: add some checks here.

            focus_info = await self.gort.guiders.sci.get_focus_info()
            focus_age = focus_info["reference_focus"]["age"]

            # Focus when the loop starts or every 1 hour.
            if focus_age is None or focus_age > 3600.0:
                await self.overwatcher.notify("Focusing telescope.")
                await self.gort.guiders.focus()

            exp: Exposure | list[Exposure] | bool = False
            try:
                # The exposure will complete in 900 seconds + acquisition + readout
                self.next_exposure_completes = time() + 90 + 900 + 60
                result, exp = await self.gort.observe_tile(
                    run_cleanup=False,
                    cleanup_on_interrupt=False,
                    show_progress=False,
                )

                if not result and not self.status.cancelling():
                    raise GortError("The observation ended with error state.")

            except asyncio.CancelledError:
                break

            except Exception as err:
                await self.overwatcher.notify(
                    f"An error occurred during the observation: {err} "
                    "Running the cleanup recipe.",
                    level="error",
                )
                await self.gort.cleanup(readout=False)

            finally:
                if self.status.cancelling():
                    self.log.warning("Cancelling observations.")
                    try:
                        if exp and isinstance(exp[0], Exposure):
                            await asyncio.wait_for(exp[0], timeout=80)
                    except Exception:
                        pass

                    break

        self.status &= ~(ObserverStatus.CANCELLING | ObserverStatus.OBSERVING)
        await self.gort.cleanup()

        await self.overwatcher.notify("The observing loop has ended.")
