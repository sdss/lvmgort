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
    idle: bool = True
    observing: bool = False
    calibrating: bool = False
    troubleshooting: bool = False
    focusing: bool = False
    night: bool = False
    safe: bool = False
    alerts: list[str] = dataclasses.field(default_factory=list)
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
                ow.state.night = ow.ephemeris.is_night()

                ow.state.safe, alerts_bits = await ow.alerts.is_safe()
                ow.state.alerts = [
                    alert.name
                    for alert in ActiveAlert
                    if (alerts_bits & alert) and alert.name
                ]

                ow.state.calibrating = ow.calibrations.is_calibrating()
                ow.state.observing = ow.observer.is_observing
                ow.state.focusing = ow.observer.focusing

                ow.state.troubleshooting = ow.is_troubleshooting()

                ow.state.idle = await ow.dome.is_opening() and not (
                    ow.state.observing
                    or ow.state.calibrating
                    or ow.state.focusing
                    or ow.state.troubleshooting
                    or ow.observer._starting_observations
                )

                # TODO: should these handlers be scheduled as tasks? Right now
                # they can block for a good while until the dome is open/closed.

                if ow.state.shutdown_pending:
                    await ow.shutdown(
                        close_dome=True,
                        retry=True,
                        park=True,
                        disable_overwatcher=True,
                        cancel_safe_calibrations=False,
                    )

                if not ow.state.safe:
                    await self.handle_unsafe()

                if not ow.ephemeris.is_night(mode="observer"):
                    # We use the observer mode to allow stopping some minutes
                    # before the end of the night but will check if an exposure
                    # is running in handle_daytime().
                    await self.handle_daytime()

                if not ow.state.enabled:
                    await self.handle_disabled()

                if await ow.dome.is_closing():
                    # If the dome is closed or closing and we have not commanded
                    # that, ensure that the observations are stopped.
                    await self.handle_dome_closing()

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

        dome_closed = await ow.dome.is_closing()
        dome_locked = ow.dome.locked

        observing = ow.observer.is_observing
        calibrating = ow.calibrations.is_calibrating()

        _, alerts_status = await ow.alerts.is_safe()

        # If the only alert is that the dome is locked there is not much we
        # can do, so we just continue ...
        if alerts_status & ActiveAlert.DOME_LOCKED and not dome_closed:
            if alerts_status == ActiveAlert.DOME_LOCKED:
                msg = (
                    "Dome is locked and cannot be closed. This should be checked "
                    "but continuing observations for now since conditions are safe."
                )
                level = "warning"
            else:
                msg = (
                    "Dome is locked and cannot be closed. Other alerts are present. "
                    "Please check this situation ASAP."
                )
                level = "critical"

            await ow.notify(msg, level=level, min_time_between_repeat_notifications=600)
            return

        is_raining = bool(alerts_status & ActiveAlert.RAIN)
        e_stops_in = bool(alerts_status & ActiveAlert.E_STOPS)

        # Don't try to do anything fancy here. Just close the dome first. Then we'll
        # deal with cancelling observing and so on.
        if is_raining and not e_stops_in and not dome_closed and not dome_locked:
            await ow.notify("Rain detected. Closing the dome.", level="critical")
            await ow.dome.close(retry=True)
            await asyncio.sleep(5)

        # Close the dome always if the Overwatcher is enabled or if the alert(s) are
        # one of the list that always forces the dome to close.
        close_dome = ow.state.enabled or bool(alerts_status & ActiveAlert.ALWAYS_CLOSE)

        # If we have a no-close alert (e-stops or local mode), we don't close the dome.
        if bool(alerts_status & ActiveAlert.NO_CLOSE):
            close_dome = False

        # If it's raining or the e-stops are in, we disable the overwatcher. Otherwise
        # we keep it enabled and allow it to reopen the dome later.
        disable_overwatcher: bool = is_raining or e_stops_in

        if not dome_closed or observing or calibrating:
            alert_names = (
                alerts_status.name.replace("|", " | ")
                if alerts_status.name
                else "UNKNOWN"
            )

            try:
                await ow.shutdown(
                    reason=f"Unsafe conditions detected: {alert_names}",
                    close_dome=close_dome,
                    disable_overwatcher=disable_overwatcher,
                )
            except Exception as err:
                await ow.notify(
                    f"Error executing the shutdown procedure: {decap(err)}",
                    level="critical",
                    error=err,
                )

    async def handle_daytime(self):
        """Handles daytime conditions."""

        # Don't do anything if we are calibrating.
        # If a calibration script opened the dome it should close it afterwards.
        if self.overwatcher.calibrations.is_calibrating():
            return

        time_to_morning = self.overwatcher.ephemeris.time_to_morning_twilight() or 0

        # If we are observing, we cancel the tile but wait until it completes.
        # But include a timeout to avoid waiting forever.
        if self.overwatcher.observer.is_observing:
            if time_to_morning and time_to_morning < -600:
                # If it's been more than 10 minutes since morning twilight, cancel.
                await self.overwatcher.observer.stop_observing(
                    immediate=True,
                    block=True,
                )
            elif not self.overwatcher.observer.is_cancelling:
                # We have already cancelled the loop and are waiting for the
                # current exposure to finish. We don't do anything.
                return
            else:
                # It's been fewer than 10 minutes since morning twilight. Cancel
                # the current exposure but allow it to finish.
                await self.overwatcher.observer.stop_observing(
                    immediate=False,
                    reason="daytime conditions detected",
                )
                return

        # If the overwatcher is disabled, we don't do anything. There is a lower-level
        # check in the lvmecp that will close the dome during daytime and it can be
        # disabled for engineering. We don't want to interfere with that.
        if not self.overwatcher.state.enabled:
            return

        # If the overwatcher is enabled but it's daytime, we close the dome. We do not
        # disable the overwatcher here since it's difficult to determine when it's ok
        # to do it (we still want to allow observers to enable it in the evening).
        # The overwatcher is disabled when the SJD changes.
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

    async def handle_dome_closing(self):
        """Stop observations and calibrations if the dome is closing."""

        ow = self.overwatcher

        if ow.observer._starting_observations:
            # Do not interfere with the observer trying to open the dome for observing.
            return

        if ow.observer.is_observing and not ow.observer.is_cancelling:
            await ow.shutdown(
                "dome is closing",
                level="warning",
                close_dome=False,
                park=False,
                disable_overwatcher=True,
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

    async def startup(self, open_dome: bool = True, focus: bool = False):
        """Runs the startup sequence."""

        # We run up the startup recipe but do not open the dome or focus.

        self.log.info("Running the dome startup sequence.")
        await self.gort.startup(open_enclosure=False, focus=False)

        # Now we manually open.
        if open_dome:
            await self.dome.open()

            if focus:
                await self.gort.guiders.focus()

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
        dome_locked = self.dome.locked
        local_mode = await self.gort.enclosure.is_local()

        calibrating = self.calibrations.is_calibrating()

        if disable_overwatcher and self.state.enabled:
            self.state.enabled = False
            await self.notify("The Overwatcher has been disabled.")

        # Check if we have already safe and shut down, and if so, return.
        if not force and await self.is_shutdown():
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
                reason="shutdown triggered",
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
        if close_dome and not dome_closed and not dome_locked:
            if local_mode:
                await self.notify(
                    "Enclosure is in local mode. Dome will not be closed.",
                    min_time_between_repeat_notifications=600,
                    level="warning",
                )
                return
            else:
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
        if park and not local_mode:
            try:
                await asyncio.wait_for(
                    self.gort.telescopes.park(disable=True),
                    timeout=120,
                )
            except Exception as err:
                await self.notify(
                    f"Error parking the telescopes during shutdown: {decap(err)}",
                    level="error",
                    error=err,
                )

        # Acknowledge any pending shutdown.
        self.state.shutdown_pending = False

        await self.notify("Shutdown complete.", level="info")

    async def is_shutdown(self):
        """Determines whether the observatory is safely shut down."""

        dome_closed = await self.dome.is_closed()
        dome_locked = self.dome.locked

        observing = self.observer.is_observing
        calibrating = self.calibrations.is_calibrating()

        return (dome_closed or dome_locked) and not observing and not calibrating

    def is_troubleshooting(self) -> bool:
        """Returns whether the Overwatcher is currently troubleshooting."""

        if self.troubleshooter.troubleshooting:
            return True

        if self.health.troubleshooting:
            return True

        return False

    async def cancel(self):
        """Cancels the overwatcher tasks."""

        for module in OverwatcherModule.instances:
            self.gort.log.debug(f"Cancelling overwatcher module {module.name!r}")
            await module.cancel()

        for task in self.tasks:
            await task.cancel()

        self.state.running = False
