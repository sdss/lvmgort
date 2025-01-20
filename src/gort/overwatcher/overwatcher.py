#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: overwatcher.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
from time import time

from typing import TYPE_CHECKING, cast

from sdsstools import Configuration
from sdsstools.utils import GatheringTaskGroup

from gort.exceptions import GortError
from gort.overwatcher.alerts import ActiveAlert
from gort.overwatcher.core import OverwatcherBaseTask, OverwatcherModule
from gort.overwatcher.helpers import DomeHelper
from gort.overwatcher.helpers.notifier import NotifierMixIn
from gort.overwatcher.helpers.tasks import DailyTasks
from gort.overwatcher.troubleshooter.troubleshooter import Troubleshooter
from gort.tools import LogNamespace, decap


if TYPE_CHECKING:
    from gort.gort import Gort
    from gort.overwatcher.helpers.notifier import NotificationLevel


@dataclasses.dataclass
class OverwatcherState:
    """Dataclass with the overwatcher state values."""

    running: bool = False
    enabled: bool = False
    observing: bool = False
    calibrating: bool = False
    troubleshooting: bool = False
    focusing: bool = False
    night: bool = False
    safe: bool = False
    allow_calibrations: bool = True
    dry_run: bool = False

    shutdown_pending: bool = False


class OverwatcherTask(OverwatcherBaseTask):
    """Overwatcher task that is aware of the overwatcher instance."""

    def __init__(self, overwatcher: Overwatcher):
        super().__init__()

        self.overwatcher = overwatcher

        # A bit configuring but _log is used internally, mainly for
        # OverwatcherBaseTask.run() and log is for external use.
        self._log = self.overwatcher.log
        self.log = self._log


class OverwatcherMainTask(OverwatcherTask):
    """The main overwatcher task."""

    name = "overwatcher_task"
    keep_alive = True
    restart_on_error = True

    def __init__(self, overwatcher: Overwatcher):
        super().__init__(overwatcher)

        self._pending_close_dome: bool = False

    async def task(self):
        """Main overwatcher task."""

        await asyncio.sleep(1)

        ow = self.overwatcher

        while True:
            try:
                is_safe, _ = ow.alerts.is_safe()
                is_night = ow.ephemeris.is_night()
                is_troubleshooting = ow.troubleshooter.is_troubleshooting()

                ow.state.night = is_night
                ow.state.safe = is_safe

                ow.state.calibrating = ow.calibrations.is_calibrating()
                ow.state.observing = ow.observer.is_observing
                ow.state.focusing = ow.observer.focusing

                ow.state.troubleshooting = (
                    ow.state.troubleshooting or is_troubleshooting
                )

                # TODO: should these handlers be scheduled as tasks? Right now
                # they can block for a good while until the dome is open/closed.

                if not is_safe:
                    await self.handle_unsafe()

                if not is_night:
                    await self.handle_daytime()

                if not ow.state.enabled:
                    await self.handle_disabled()

                if ow.state.shutdown_pending:
                    await ow.shutdown(
                        close_dome=True,
                        retry=True,
                        park=True,
                        disable_overwatcher=True,
                        cancel_safe_calibrations=False,
                    )

                # Run daily tasks.
                await ow.daily_tasks.run_all()

            except Exception as err:
                await ow.notify(
                    f"Error in main overwatcher task: {decap(err)}",
                    error=err,
                    level="error",
                )

                # Avoid rapid fire errors. Sleep a bit longer before trying again.
                await asyncio.sleep(30)

            await asyncio.sleep(5)

    async def handle_unsafe(self):
        """Closes the dome if the conditions are unsafe."""

        ow = self.overwatcher

        closed = await ow.dome.is_closing()

        observing = ow.observer.is_observing
        calibrating = ow.calibrations.is_calibrating()

        _, alerts_status = ow.alerts.is_safe()

        is_raining = bool(alerts_status & ActiveAlert.RAIN)
        e_stops_in = bool(alerts_status & ActiveAlert.E_STOPS)

        close_dome: bool = True
        disable_overwatcher: bool = is_raining or e_stops_in

        # If we are not observing and the e-stops are in, we don't close the dome.
        # Just disable the overwatcher if it is on.
        if not observing and not calibrating and e_stops_in:
            if ow.state.enabled:
                await ow.notify("E-stop buttons are pressed.", level="warning")
                await ow.shutdown(close_dome=False, disable_overwatcher=True)
            return

        # Don't try to do anything fancy here. Just close the dome first.
        if is_raining and not closed and not e_stops_in:
            await ow.notify("Rain detected. Closing the dome.", level="critical")
            await ow.dome.close(retry=True)
            await asyncio.sleep(5)

        if not closed or observing or calibrating:
            alert_names = (
                alerts_status.name.replace("|", " | ")
                if alerts_status.name
                else "UNKNOWN"
            )

            if e_stops_in:
                await ow.notify(
                    "E-stop buttons are pressed. Dome won't be closed.",
                    level="warning",
                )
                close_dome = False

            await ow.shutdown(
                reason=f"Unsafe conditions detected: {alert_names}",
                close_dome=close_dome,
                disable_overwatcher=disable_overwatcher,
            )

            if not closed:
                # If we have to close because of unsafe conditions, we
                # don't want to reopen too soon. We lock the dome for some time.
                timeout = ow.config["overwatcher.lock_timeout_on_unsafe"]
                ow.alerts.locked_until = time() + timeout

                await ow.notify(
                    f"The dome will be locked for {int(timeout)} seconds.",
                    level="warning",
                )

    async def handle_daytime(self):
        """Handles daytime conditions."""

        # Don't do anything if we are calibrating.
        # If a calibration script opened the dome it should close it afterwards.
        if self.overwatcher.calibrations.is_calibrating():
            return

        # Do not disabled the overwatcher. We want to allow users to enable it
        # during the day prior to observations.
        await self.overwatcher.shutdown(
            reason="Daytime conditions detected.",
            level="info",
            close_dome=True,
            retry=True,
            disable_overwatcher=False,
            park=True,
            cancel_safe_calibrations=False,
        )

    async def handle_disabled(self):
        """Handles the disabled state."""

        observing = self.overwatcher.observer.is_observing
        cancelling = self.overwatcher.observer.is_cancelling

        if observing and not cancelling:
            # Disable after this tile.
            await self.overwatcher.observer.stop_observing(
                immediate=False,
                reason="overwatcher was disabled",
            )


class OverwatcherPingTask(OverwatcherTask):
    """Emits a ping notification every five minutes."""

    name = "overwatcher_ping"
    keep_alive = True
    restart_on_error = True

    delay: float = 900

    async def task(self):
        """Ping task."""

        while True:
            await asyncio.sleep(self.delay)
            self.log.debug("I am alive!")


class Overwatcher(NotifierMixIn):
    """Monitors the observatory."""

    instance: Overwatcher | None = None

    def __new__(cls, *args, **kwargs):
        if not cls.instance:
            cls.instance = super(Overwatcher, cls).__new__(cls)
        return cls.instance

    def __init__(
        self,
        gort: Gort | None = None,
        verbosity: str = "warning",
        calibrations_file: str | pathlib.Path | None = None,
        dry_run: bool = False,
        **kwargs,
    ):
        from gort import Gort  # Needs to be imported here to avoid circular imports.
        from gort.overwatcher import (
            AlertsOverwatcher,
            CalibrationsOverwatcher,
            EphemerisOverwatcher,
            EventsOverwatcher,
            HealthOverwatcher,
            ObserverOverwatcher,
            SafetyOverwatcher,
            TransparencyOverwatcher,
        )

        # Check if the instance already exists, in which case do nothing.
        if hasattr(self, "gort"):
            return

        self.gort = gort or Gort(verbosity=verbosity, **kwargs)
        self.config = cast(Configuration, self.gort.config)
        self.log = LogNamespace(self.gort.log, header=f"({self.__class__.__name__}) ")

        self.state = OverwatcherState()
        self.state.dry_run = dry_run

        self.dome = DomeHelper(self)
        self.troubleshooter = Troubleshooter(self)
        self.tasks: list[OverwatcherTask] = [
            OverwatcherMainTask(self),
            OverwatcherPingTask(self),
        ]

        # A series of tasks that must be run once every day.
        self.daily_tasks = DailyTasks(self)

        self.safety = SafetyOverwatcher(self)
        self.ephemeris = EphemerisOverwatcher(self)
        self.calibrations = CalibrationsOverwatcher(self, calibrations_file)
        self.observer = ObserverOverwatcher(self)
        self.alerts = AlertsOverwatcher(self)
        self.transparency = TransparencyOverwatcher(self)
        self.events = EventsOverwatcher(self)
        self.health = HealthOverwatcher(self)

    async def run(self):
        """Starts the overwatcher tasks."""

        if self.state.running:
            raise GortError("Overwatcher is already running.")

        if not self.gort.is_connected():
            await self.gort.init()

        async with GatheringTaskGroup() as group:
            for module in OverwatcherModule.instances:
                self.log.info(f"Starting overwatcher module {module.name!r}")
                group.create_task(module.run())

        async with GatheringTaskGroup() as group:
            for task in self.tasks:
                group.create_task(task.run())

        self.state.running = True
        await self.notify(
            "Overwatcher is now running.",
            payload={"dry-run": self.state.dry_run},
        )

        if self.state.dry_run:
            self.log.warning("Overatcher is running in dry-mode.")

        return self

    async def shutdown(
        self,
        reason: str | None = None,
        level: NotificationLevel = "info",
        close_dome: bool = True,
        retry: bool = True,
        park: bool = True,
        disable_overwatcher: bool = True,
        cancel_safe_calibrations: bool = False,
        force: bool = False,
    ):
        """Shuts down the observatory.

        Parameters
        ----------
        reason
            The reason for the shutdown.
        level
            The level of the notification.
        close_dome
            Whether to close the dome.
        retry
            If :obj:`True`, retries closing the dome in overcurrent mode if the first
            attempt fails.
        park
            Whether to ensure that the telescopes are parked before closing the dome.
        disable_overwatcher
            If :obj:`True`, disables the overwatcher after the shutdown.
        cancel_safe_calibrations
            If :obj:`True`, cancels any safe calibrations that are currently running
            even if the dome is closed.
        force
            If :obj:`True`, forces the shutdown even if the dome is already closed.
            Otherwise only the overwatcher is disabled if necessary but the
            other tasks are ignored.

        """

        dome_closed = await self.dome.is_closing()
        observing = self.observer.is_observing
        calibrating = self.calibrations.is_calibrating()

        if disable_overwatcher and self.state.enabled:
            self.state.enabled = False
            await self.notify("The Overwatcher has been disabled.")

        # Check if we have already safe and shut down, and if so, return.
        if dome_closed and not force and not observing and not calibrating:
            return

        if dome_closed and calibrating and not cancel_safe_calibrations:
            return

        # Notify about the shutdown.
        if not reason:
            message = "Triggering shutdown."
        else:
            if not reason.endswith("."):
                reason += "."
            message = f"Triggering shutdown. Reason: {decap(reason)}"

        await self.notify(message, level=level)

        if self.state.dry_run:
            self.log.warning("Dry run enabled. Not shutting down.")
            return

        # Step 1: cancel observations and calibrations.
        try:
            stop_observing = self.observer.stop_observing(
                immediate=True,
                reason=reason or "shutdown triggered",
            )

            self.log.info("Cancelling observing loop and calibrations.")
            await asyncio.wait_for(
                asyncio.gather(stop_observing, self.calibrations.cancel()),
                timeout=30,
            )
        except Exception as err:
            await self.notify(
                f"Error cancelling observations during shutdown: {decap(err)}",
                level="error",
                error=err,
            )

        # Step 2: cancel guiders and turn off lamps.
        try:
            self.log.info("Turning off guiders and lamps.")
            await asyncio.wait_for(
                asyncio.gather(self.gort.nps.calib.all_off(), self.gort.guiders.stop()),
                timeout=60,
            )
        except Exception as err:
            await self.notify(
                f"Error running shutdown tasks: {decap(err)}",
                level="error",
                error=err,
            )

        # Step 3: close the dome.
        if close_dome and not dome_closed:
            try:
                await asyncio.wait_for(
                    self.dome.close(retry=retry, park=park),
                    timeout=360,
                )
            except Exception as err:
                # Check if the dome is closed to determine
                # the level of the notification.
                level = "warning" if await self.dome.is_closed() else "critical"

                await self.notify(
                    f"Error closing the dome during shutdown: {decap(err)}",
                    level=level,
                    error=err,
                )
                return

        # Step 4: park and disable the telescopes.
        if park:
            await asyncio.wait_for(self.gort.telescopes.park(disable=True), timeout=120)

        # Acknowledge any pending shutdown.
        self.state.shutdown_pending = False

    async def cancel(self):
        """Cancels the overwatcher tasks."""

        for module in OverwatcherModule.instances:
            self.gort.log.debug(f"Cancelling overwatcher module {module.name!r}")
            await module.cancel()

        for task in self.tasks:
            await task.cancel()

        self.state.running = False
