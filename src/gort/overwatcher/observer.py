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

from gort.exposure import Exposure
from gort.overwatcher import OverwatcherModule
from gort.overwatcher.core import OverwatcherModuleTask
from gort.tools import cancel_task, redis_client


__all__ = ["ObserverOverwatcher"]


class ObserverStatus(enum.Flag):
    """An enumeration of observer statuses."""

    OBSERVING = 1 << 0
    CANCELLED = 1 << 1
    NIGHT = 1 << 2
    ENABLED = 1 << 3
    ALLOWED = 1 << 4
    WEATHER_SAFE = 1 << 5

    def observing(self):
        return self & ObserverStatus.OBSERVING

    def cancelled(self):
        return self & ObserverStatus.CANCELLED

    def can_observe(self):
        return (
            (self & self.NIGHT)
            and (self & self.ENABLED)
            and (self & self.ALLOWED)
            and (self & self.WEATHER_SAFE)
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

            if not (self.status & ObserverStatus.WEATHER_SAFE):
                await self.module.stop_observing(
                    immediate=True,
                    reason="weather not safe",
                )

            elif not (self.status & ObserverStatus.NIGHT):
                await self.module.stop_observing(
                    immediate=True,
                    reason="not night time",
                )

            elif not (self.status & ObserverStatus.ALLOWED):
                await self.module.stop_observing(
                    immediate=True,
                    reason="observations not allowed by the overwatcher",
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
        if self.status.cancelled():
            new_status |= ObserverStatus.CANCELLED

        if (
            self.status.observing()
            and self.module.observe_loop
            and not self.module.observe_loop.done()
        ):
            new_status |= ObserverStatus.OBSERVING

        if self.overwatcher.ephemeris.is_night():
            new_status |= ObserverStatus.NIGHT

        if self.overwatcher.allow_observations:
            new_status |= ObserverStatus.ALLOWED

        if await self.module.is_enabled():
            new_status |= ObserverStatus.ENABLED

        if self.overwatcher.weather.is_safe():
            new_status |= ObserverStatus.WEATHER_SAFE

        # Warn about change in enabled status.
        if self.status.enabled() != new_status.enabled():
            if new_status.enabled():
                self.log.info("Overwatcher is now enabled.")
            else:
                self.log.info("Overwatcher is now disabled")

        self.status = new_status


class ObserverOverwatcher(OverwatcherModule):
    name = "observer"

    tasks = [ObserverMonitorTask()]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.status = ObserverStatus(0)
        self.observe_loop: asyncio.Task | None = None

    async def start_observing(self):
        """Starts observations."""

        if self.status.observing():
            return

        weather = self.overwatcher.weather
        if not weather.is_running or not weather.is_safe():
            raise GortError("Cannot safely open the telescope.")

        await self.overwatcher.write_to_slack("Starting observations.")

        if not (await self.gort.enclosure.is_open()):
            # TODO: this should be an overwatcher function with exception wrapping.
            self.log.info("Opening the dome.")
            await self.gort.startup(confirm_open=False)

        self.observe_loop = asyncio.create_task(self.observe_loop_task())
        self.status |= ObserverStatus.OBSERVING

    async def stop_observing(
        self,
        immediate: bool = False,
        reason: str = "undefined",
    ):
        """Stops observations."""

        if not self.status.observing():
            return

        if immediate:
            await self.overwatcher.write_to_slack(
                f"Stopping observations immediately. Reason: {reason}.",
                log=True,
                log_level="warning",
            )

            await cancel_task(self.observe_loop)
            self.observe_loop = None

            await self.gort.cleanup(readout=False)

        elif not self.status.cancelled():
            await self.overwatcher.write_to_slack(
                f"Stopping observations after this tile: {reason}",
                log=True,
                log_level="warning",
            )

        self.status.cancel()

    async def observe_loop_task(self):
        """Runs the observing loop."""

        await self.gort.cleanup(readout=True)

        while True:
            # TODO: add some checks here.

            exp: Exposure | list[Exposure] | bool = False
            try:
                # The exposure will complete in 900 seconds + acquisition + readout
                self.next_exposure_completes = time() + 90 + 900 + 60
                exp = await self.gort.observe_tile(
                    run_cleanup=False,
                    cleanup_on_interrrupt=False,
                    show_progress=False,
                )

            except Exception as err:
                self.overwatcher.handle_error(err)
                await self.gort.cleanup(readout=False)

            finally:
                if self.status.cancelled():
                    self.log.warning("Cancelling observations.")
                    try:
                        if exp and isinstance(exp[0], Exposure):
                            await asyncio.wait_for(exp[0], timeout=80)
                    except Exception as err:
                        self.overwatcher.handle_error(
                            f"Error cancelling observation: {err!r}",
                            err,
                        )

                    break

        self.status &= ~(ObserverStatus.CANCELLED | ObserverStatus.OBSERVING)
        await self.gort.cleanup()

        await self.overwatcher.write_to_slack(
            "The observing loop has ended.",
            log=True,
            log_level="info",
        )

    async def is_enabled(self):
        """Is observing enabled?"""

        try:
            client = redis_client()
            enabled: str | None = await client.get("gort:overwatcher:enabled")
            if not enabled:
                raise ValueError("cannot determine if observing is enabled.")
            return bool(int(enabled))
        except Exception as err:
            self.overwatcher.handle_error(
                f"Cannot determine if observing is enabled: {err!r}",
                err,
            )

        return False
