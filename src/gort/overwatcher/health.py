#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-12-26
# @Filename: health.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import ClassVar

from lvmopstools.retrier import Retrier
from lvmopstools.utils import with_timeout

from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.overwatcher.helpers import get_failed_actors, restart_actors
from gort.tools import decap


__all__ = ["HealthOverwatcher"]


class EmitHeartbeatTask(OverwatcherModuleTask["HealthOverwatcher"]):
    """Emits the heartbeat to indicate that the Overwatcher is alive."""

    name = "emit_heartbeat_task"
    keep_alive = True
    restart_on_error = True

    INTERVAL: ClassVar[float] = 5

    def __init__(self):
        super().__init__()

    async def task(self):
        """Sets the overwatcher heartbeat in ``lvmbeat``."""

        while True:
            try:
                cmd = await self.gort.send_command("lvmbeat", "set overwatcher")
                if cmd.status.did_fail:
                    raise RuntimeError("Failed to set overwatcher heartbeat.")

            except Exception as err:
                self.overwatcher.log.error(
                    f"Failed to set overwatcher heartbeat: {decap(err)}",
                    exc_info=err,
                )

            await asyncio.sleep(self.INTERVAL)


class ActorHealthMonitorTask(OverwatcherModuleTask["HealthOverwatcher"]):
    """Monitors the health of actors."""

    name = "actor_health_monitor_task"
    keep_alive = True
    restart_on_error = True

    INTERVAL: ClassVar[float] = 60

    def __init__(self):
        super().__init__()

    async def task(self):
        """Monitors the health of actors."""

        while True:
            failed: list[str] = []

            try:
                failed = await get_failed_actors(
                    discard_disabled=True,
                    discard_overwatcher=True,
                )
                if len(failed) > 0:
                    if self.overwatcher.state.enabled:
                        await self.restart_actors(failed)
                    else:
                        self.log.info(
                            f"Found unresponsible actors: {', '.join(failed)}. "
                            "Not restarting because the Overwatcher is disabled."
                        )

            except Exception as err:
                self.log.error(
                    f"Failed to check actor health: {err}",
                    exc_info=err,
                )

                if len(failed) > 0:
                    await self.overwatcher.shutdown(
                        "actors found unresponsible and cannot be restarted.",
                        disable_overwatcher=True,
                    )

            finally:
                self.overwatcher.state.troubleshooting = False

            await asyncio.sleep(self.INTERVAL)

    @Retrier(max_attempts=2, delay=5)
    async def restart_actors(self, failed_actors):
        """Restarts actors that have failed."""

        ow = self.overwatcher
        ow.state.troubleshooting = True

        actors_join = ", ".join(failed_actors)
        is_observing = ow.observer.is_observing
        is_calibrating = ow.calibrations.get_running_calibration() is not None

        await self.notify(f"Found unresponsible actors: {', '.join(failed_actors)}.")

        if is_observing:
            if (
                self.gort.specs.last_exposure
                and not self.gort.specs.last_exposure.done()
                and not await self.gort.specs.are_reading()
            ):
                await self.notify("Waiting to read exposures before cancelling.")
                await with_timeout(
                    self.gort.specs.last_exposure,  # type: ignore
                    timeout=60,
                    raise_on_timeout=False,
                )

            await ow.observer.stop_observing(
                immediate=True,
                reason=f"Found unresponsible actors: {actors_join}. "
                "Cancelling observations and restarting them.",
            )

        if is_calibrating:
            await self.notify(
                f"Found unresponsible actors: {actors_join}. "
                "Cancelling calibrations and restarting them.",
            )
            await ow.calibrations.cancel()

        await self.notify("Restarting actors.")
        await restart_actors(failed_actors, self.gort)

        await self.notify("Actor restart complete. Resuming normal operations.")

        ow.state.troubleshooting = False


class HealthOverwatcher(OverwatcherModule):
    """Monitors health."""

    name = "health"
    tasks = [EmitHeartbeatTask(), ActorHealthMonitorTask()]
    delay = 0
