#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-27
# @Filename: observer.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import Coroutine

from gort.overwatcher import OverwatcherModule
from gort.tools import cancel_task, get_lvmapi_route


__all__ = ["ObserverOverwatcher"]


class ObserverOverwatcher(OverwatcherModule):

    name = "observer"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.observing: bool = False
        self.cancelling: bool = False

        self.observe_loop: asyncio.Task | None = None

    def list_task_coros(self) -> list[Coroutine]:
        """Returns a list of coroutines to schedule as tasks."""

        return [self.observe_monitor()]

    async def observe_monitor(self):
        """Handles whether we should start the observing loop."""

        # Give some time to the other watchers to gather and update data.
        await asyncio.sleep(5)

        while True:
            self.log("Running observing monitor.")

            if self.cancelling:
                self.log("Observations have already been cancelled.")

            if not (self.overwatcher.ephemeris.is_night()):
                if self.observing:
                    self.log("Daytime reached. Stopping observations.", "waning")
                    asyncio.create_task(self.stop_observing())

                self.log("Not night time. Skipping observations.")
                await self.stop_observing()

            elif not self.overwatcher.allow_observations:
                self.log("Observations are not allowed.", "warning")
                await self.stop_observing(immediate=True)

            elif not (await self.is_enabled()):
                self.log("Observing is disabled.")
                await self.stop_observing()

            elif self.observing:
                self.log("Already observing.")

            elif not self.overwatcher.weather.can_open():
                self.log("Weather conditions are not safe. Not observing.", "warning")
                await self.stop_observing(immediate=True)

            else:
                await self.start_observing()

            await asyncio.sleep(60)

    async def start_observing(self):
        """Starts observations."""

        if self.observing:
            self.cancelling = False
            return

        self.observing = True
        self.cancelling = False

        self.log("Starting overatcher observations.", "warning")

        if not (await self.gort.enclosure.is_open()):
            self.log("Opening the dome.", "info")
            await self.gort.startup(confirm_open=False)

        self.observe_loop = asyncio.create_task(self.observe_loop_task())

    async def stop_observing(self, immediate: bool = False):
        """Stops observations."""

        if not self.observing:
            return

        if immediate:
            self.observing = False

            await cancel_task(self.observe_loop)
            self.observe_loop = None

        elif not self.cancelling:
            self.log("Stopping observations after this tile.", "warning")
            self.cancelling = True

    async def observe_loop_task(self):
        """Runs the observing loop."""

        await self.gort.cleanup(readout=True)

        while True:
            # TODO: add some checks here.

            exp: Exposure | list[Exposure] | bool = False
            try:
                exp = await self.gort.observe_tile(
                    run_cleanup=False,
                    cleanup_on_interrrupt=False,
                    show_progress=False,
                )

            except Exception as err:
                self.log(f"Error during observation: {err!r}", "error")

                await self.gort.cleanup(readout=False)

            finally:
                if self.cancelling:
                    self.log("Cancelling observations.", "warning")

                    if exp and isinstance(exp, Exposure):
                        await exp

                    break

        self.observing = False
        self.cancelling = False

    async def is_enabled(self):
        """Is observing enabled?"""

        try:
            return await get_lvmapi_route("/overwatcher/enabled")
        except Exception as err:
            self.log(f"Cannot determine if observing is enabled: {err!r}", "error")

        return False
