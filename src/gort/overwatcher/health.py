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
from lvmopstools.utils import Trigger, with_timeout

from clu.tools import CommandStatus

from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.overwatcher.helpers import get_actor_ping, restart_actors
from gort.overwatcher.overwatcher import Overwatcher
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
                await self.emit_heartbeat()
            except Exception as err:
                self.overwatcher.log.error(
                    f"Failed to set overwatcher heartbeat: {decap(err)}",
                    exc_info=err,
                )

            await asyncio.sleep(self.INTERVAL)

    @Retrier(max_attempts=3, delay=1)
    async def emit_heartbeat(self):
        """Emits the heartbeats, with retries."""

        cmd = await self.gort.send_command(
            "lvmbeat",
            "set overwatcher",
            time_limit=5,
        )

        if cmd.status.did_fail:
            if cmd.status & CommandStatus.TIMEDOUT:
                raise RuntimeError("Timed out setting overwatcher heartbeat.")
            raise RuntimeError("Failed to set overwatcher heartbeat.")


class ActorHealthMonitorTask(OverwatcherModuleTask["HealthOverwatcher"]):
    """Monitors the health of actors."""

    name = "actor_health_monitor_task"
    keep_alive = True
    restart_on_error = True

    INTERVAL: ClassVar[float] = 30

    def __init__(self):
        super().__init__()

        self.ping_triggers: dict[str, Trigger] = {}

    async def task(self):
        """Monitors the health of actors."""

        while True:
            actor_status: dict[str, bool] = {}

            try:
                actor_status = await get_actor_ping(
                    discard_disabled=True,
                    discard_overwatcher=True,
                )

                # Update failed_actors. We want allow a grace window of 1 failed ping.
                for actor in actor_status:
                    if actor not in self.ping_triggers:
                        self.ping_triggers[actor] = Trigger(n=2)

                    if actor_status[actor]:
                        self.ping_triggers[actor].reset()
                    else:
                        self.ping_triggers[actor].set()

                # Now check which actors have failed twice (trigger is set).
                failed = [
                    actor
                    for actor, trigger in self.ping_triggers.items()
                    if trigger.is_set()
                ]

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
                self.module.troubleshooting = False

            await asyncio.sleep(self.INTERVAL)

    @Retrier(max_attempts=2, delay=5)
    async def restart_actors(self, failed_actors):
        """Restarts actors that have failed."""

        ow = self.overwatcher
        self.module.troubleshooting = True

        is_observing = ow.observer.is_observing
        is_calibrating = ow.calibrations.is_calibrating()

        actors_join = ", ".join(failed_actors)
        await self.notify(f"Found unresponsible actors: {', '.join(failed_actors)}.")

        if is_observing:
            if (
                self.gort.specs.last_exposure
                and not self.gort.specs.last_exposure.done()
                and await self.gort.specs.are_reading()
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

        for actor in failed_actors:
            self.ping_triggers[actor].reset()

        await self.notify("Actor restart complete. Resuming normal operations.")

        self.module.troubleshooting = False


class HealthOverwatcher(OverwatcherModule):
    """Monitors health."""

    name = "health"
    tasks = [EmitHeartbeatTask(), ActorHealthMonitorTask()]
    delay = 0

    def __init__(self, overwatcher: Overwatcher):
        super().__init__(overwatcher)

        self.troubleshooting: bool = False
