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
import sys

import httpx

from gort.core import LogNamespace
from gort.exceptions import GortError
from gort.gort import Gort
from gort.overwatcher.core import OverwatcherModule, OverwatcherTask


GORT_ICON_URL = "https://github.com/sdss/lvmgort/blob/overwatcher/docs/sphinx/_static/gort_logo_slack.png?raw=true"


@dataclasses.dataclass
class OverwatcherState:
    """Dataclass with the overwatcher state values."""

    running: bool = False
    enabled: bool = False
    observing: bool = False
    calibrating: bool = False
    allow_observing: bool = False
    allow_dome_calibrations: bool = False


class OverwatcherMainTask(OverwatcherTask):
    """The main overwatcher task."""

    name = "overwatcher_task"
    keep_alive = True
    restart_on_error = True

    def __init__(self, overwatcher: Overwatcher):
        super().__init__()

        self.overwatcher = overwatcher
        self.log = self.overwatcher.log

    async def task(self):
        """Main overwatcher task."""

        while True:
            await asyncio.sleep(1)

            is_safe = self.overwatcher.weather.is_safe()
            is_night = self.overwatcher.ephemeris.is_night()

            if is_safe and is_night:
                self.overwatcher.state.allow_observing = True
            else:
                self.overwatcher.state.allow_observing = False


class Overwatcher:
    """Monitors the observatory."""

    instance: Overwatcher | None = None

    def __new__(cls, *args, **kwargs):
        if not cls.instance:
            cls.instance = super(Overwatcher, cls).__new__(cls)
        return cls.instance

    def __init__(
        self,
        gort: Gort | None = None,
        verbosity: str = "debug",
        calibrations_file: str | pathlib.Path | None = None,
        **kwargs,
    ):
        from gort.overwatcher import (
            CalibrationsOverwatcher,
            EphemerisOverwatcher,
            NotificationsOverwatcher,
            ObserverOverwatcher,
            WeatherOverwatcher,
        )

        # Check if the instance already exists, in which case do nothing.
        if hasattr(self, "gort"):
            return

        self.gort = gort or Gort(verbosity=verbosity, **kwargs)
        self.log = LogNamespace(self.gort.log, header=f"({self.__class__.__name__}) ")

        self.state = OverwatcherState()

        self.tasks: list[OverwatcherTask] = [OverwatcherMainTask(self)]
        self.ephemeris = EphemerisOverwatcher(self)
        self.calibrations = CalibrationsOverwatcher(self, calibrations_file)
        self.observer = ObserverOverwatcher(self)
        self.weather = WeatherOverwatcher(self)
        self.notifications = NotificationsOverwatcher(self)

    async def run(self):
        """Starts the overwatcher tasks."""

        if self.state.running:
            raise GortError("Overwatcher is already running.")

        if not self.gort.is_connected():
            await self.gort.init()

        for module in OverwatcherModule.instances:
            self.log.info(f"Starting overwatcher module {module.name!r}")
            await module.run()

        for task in self.tasks:
            await task.run()

        self.state.running = True
        await self.write_to_slack("Overwatcher is starting.")

        return self

    async def cancel(self):
        """Cancels the overwatcher tasks."""

        for module in OverwatcherModule.instances:
            self.gort.log.debug(f"Cancelling overwatcher module {module.name!r}")
            await module.cancel()

        for task in self.tasks:
            await task.cancel()

        self.state.running = False

    async def write_to_slack(
        self,
        text: str,
        as_overwatcher: bool = True,
        mentions: list[str] = [],
        log: bool = True,
        log_level: str = "info",
    ):
        """Writes a message to Slack."""

        if log is True:
            getattr(self.log, log_level)(text)

        username = "Overwatcher" if as_overwatcher else None
        icon_url = GORT_ICON_URL if as_overwatcher else None

        host, port = self.gort.config["services"]["lvmapi"].values()

        try:
            async with httpx.AsyncClient(
                base_url=f"http://{host}:{port}",
                follow_redirects=True,
            ) as client:
                response = await client.post(
                    "/slack/message",
                    json={
                        "text": text,
                        "username": username,
                        "icon_url": icon_url,
                        "mentions": mentions,
                    },
                )

                if response.status_code != 200:
                    raise ValueError(response.text)
        except Exception as err:
            self.log.error(f"Failed to send message to Slack: {err}")

    async def emergency_shutdown(self, block: bool = True, reason: str = "undefined"):
        """Shuts down the observatory in case of an emergency."""

        await self.write_to_slack(
            f"Triggering emergency shutdown. Reason: {reason}.",
            log=True,
            log_level="warning",
        )

        stop_task = asyncio.create_task(self.observer.stop_observing(immediate=True))
        shutdown_task = asyncio.create_task(self.gort.shutdown())

        if block:
            await asyncio.gather(stop_task, shutdown_task)

    def handle_error(
        self,
        message: str | Exception | None = None,
        error: Exception | None = None,
        log: bool = True,
        slack: bool = False,
        slack_mentions: list[str] = [],
    ):
        """Handles an error in the overwatcher."""

        # TODO: actually handle the error and do troubleshooting. Call something
        # like troubleshoot_error(error).

        if isinstance(message, Exception):
            error = message
            message = str(error)

        if message is None and error is None:
            message = "An unknown error was reported."
        elif error is not None:
            if message is not None:
                message = f"{message}: {error!s}"
            else:
                message = f"{error!s}"

        assert isinstance(message, str)

        if log:
            if error is None:
                self.log.error(message, exc_info=error)
            else:
                self.log.exception(message, exc_info=sys.exc_info())

        if slack:
            asyncio.create_task(
                self.write_to_slack(
                    message,
                    as_overwatcher=True,
                    log=False,
                    mentions=slack_mentions,
                )
            )
