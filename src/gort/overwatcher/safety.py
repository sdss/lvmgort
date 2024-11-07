#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-11-01
# @Filename: safety.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from time import time

from typing import ClassVar

from lvmopstools.retrier import Retrier

from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask


__all__ = ["SafetyOverwatcher"]


class SafetyMonitorTask(OverwatcherModuleTask["SafetyOverwatcher"]):
    """Monitors the safety status."""

    name = "safety_monitor"
    keep_alive = False
    restart_on_error = True

    INTERVAL: ClassVar[float] = 30

    def __init__(self):
        super().__init__()

        self.unsafe_since: float | None = None
        self.failed: bool = False

        self.n_get_data_failures: int = 0

    async def task(self):
        """Checks safety conditions and closes the dome after a delay."""

        while True:
            try:
                is_safe, dome_open = await self.get_data()
                self.n_get_data_failures = 0
            except Exception as err:
                if self.n_get_data_failures == 0:
                    self.overwatcher.log.error(
                        f"Error getting safety data: {err}",
                        exc_info=err,
                    )
                elif self.n_get_data_failures >= 3:
                    await self.overwatcher.notify(
                        "The safety monitor task has failed to get safety data "
                        "three times in a row. The task will stop.",
                        level="critical",
                    )
                    break

                self.n_get_data_failures += 1
                await asyncio.sleep(self.INTERVAL)
                continue

            try:
                if not self.failed and not is_safe and dome_open:
                    # Conditions are unsafe and the dome is open. We give the
                    # main task 5 minutes to close the dome itself. If it fails,
                    # we close the dome ourselves.

                    now = time()

                    if self.unsafe_since is None:
                        self.unsafe_since = now
                    elif now - self.unsafe_since > 5 * 60:
                        await self.overwatcher.notify(
                            "Safety alerts detected and the dome remains open. "
                            "Closing the dome.",
                            level="error",
                        )
                        await self.module.close_dome()

                        # Now run a shutdown. This should not try to close the dome
                        # since that's already done, but it will stop the observe loop,
                        # clean-up, etc.
                        await self.overwatcher.shutdown(
                            reason="safety alerts detected",
                            disable_overwatcher=True,
                        )

                elif self.failed:
                    # We have failed closing the dome as a last resort. We have issued
                    # a critical alert. We don't try closing the dome again.
                    pass

                else:
                    # Conditions are safe or the dome is closed. Reset variables.
                    self.unsafe_since = None
                    self.failed = False

            except Exception as err:
                await self.overwatcher.notify(
                    "The safety monitor task has failed to close the dome. "
                    "Unsafe conditions have been detected. "
                    "Please close the dome immediately!!!",
                    level="critical",
                )

                # Record the task error in the log.
                self.overwatcher.log.critical(
                    f"Error in safety monitor task: {err}", exc_info=err
                )

                self.failed = True

            await asyncio.sleep(self.INTERVAL)

    @Retrier(max_attempts=3, delay=5)
    async def get_data(self):
        """Returns the safety status and whether the dome is open."""

        is_safe, _ = self.overwatcher.alerts.is_safe()
        dome_open = await self.overwatcher.gort.enclosure.is_open()

        return is_safe, dome_open


class SafetyOverwatcher(OverwatcherModule):
    """Monitors alerts."""

    name = "safety"
    tasks = [SafetyMonitorTask()]
    delay = 10

    @Retrier(max_attempts=3, delay=5)
    async def close_dome(self):
        """Closes the dome. Bypasses all safety checks."""

        # Closes the dome at an extremely low level without parking the telescopes,
        # checking the local status, issuing notifications, etc. This is a last
        # resort to close the dome in case of an emergency and we want to avoid
        # as many steps that could fail as possible.

        await self.gort.enclosure.actor.commands.dome.commands.close(force=True)
