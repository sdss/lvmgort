#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-10-27
# @Filename: helpers.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import enum

from typing import TYPE_CHECKING

from sdsstools.utils import GatheringTaskGroup

from gort.exceptions import GortError


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


class DomeStatus(enum.Flag):
    """An enumeration of dome statuses."""

    OPEN = enum.auto()
    CLOSED = enum.auto()
    OPENING = enum.auto()
    CLOSING = enum.auto()
    MOVING = enum.auto()
    UNKNOWN = enum.auto()


class DomeHelper:
    """Handle dome movement."""

    def __init__(self, overwatcher: Overwatcher):
        self.overwatcher = overwatcher
        self.gort = overwatcher.gort
        self.log = overwatcher.log

        self._moving: bool = False

        self._action_lock = asyncio.Lock()
        self._move_lock = asyncio.Lock()

    async def status(self):
        """Returns the status of the dome."""

        status = await self.gort.enclosure.status()
        labels = status["dome_status_labels"]

        if "MOTOR_OPENING" in labels:
            return DomeStatus.OPENING | DomeStatus.MOVING
        elif "MOTOR_CLOSING" in labels:
            return DomeStatus.CLOSING | DomeStatus.MOVING
        elif "OPEN" in labels:
            if self._moving:
                return DomeStatus.CLOSING | DomeStatus.MOVING
            return DomeStatus.OPEN
        elif "CLOSED" in labels:
            if self._moving:
                return DomeStatus.OPENING | DomeStatus.MOVING
            return DomeStatus.CLOSED

        return DomeStatus.UNKNOWN

    async def is_opening(self):
        """Returns True if the dome is open or opening."""

        status = await self.status()

        if status & DomeStatus.OPENING:
            return True
        if status & DomeStatus.OPEN:
            return True

        return False

    async def is_closing(self):
        """Returns True if the dome is closed or closed."""

        status = await self.status()

        if status & DomeStatus.CLOSING:
            return True
        if status & DomeStatus.CLOSED:
            return True

        return False

    async def _move(
        self,
        open: bool = False,
        park: bool = True,
        retry: bool = False,
    ):
        """Moves the dome."""

        if self._moving:
            self.log.debug("Dome is already moving. Stopping before moving again.")
            await self.stop()

        self._moving = True

        try:
            if open:
                await self.gort.enclosure.open(park_telescopes=park)
            else:
                await self.gort.enclosure.close(
                    park_telescopes=park,
                    retry_without_parking=retry,
                )

        except Exception:
            status = await self.status()
            if status & DomeStatus.MOVING:
                self.log.warning("Dome is still moving after an error. Stopping.")
                await self.stop()

            raise

        self._moving = False

    async def open(self, park: bool = True):
        """Opens the dome."""

        async with self._move_lock:
            current = await self.status()
            if current == DomeStatus.OPEN:
                self.log.debug("Dome is already open.")
                return

            if current == DomeStatus.OPENING:
                self.log.debug("Dome is already opening.")
                return

            if current == DomeStatus.UNKNOWN:
                self.log.warning("Dome is in an unknown status. Stopping and opening.")
                await self.stop()

            self.log.info("Opening the dome ...")
            await self._move(open=True, park=park)

    async def close(self, park: bool = True, retry: bool = False):
        """Closes the dome."""

        async with self._move_lock:
            current = await self.status()
            if current == DomeStatus.CLOSED:
                self.log.debug("Dome is already closed.")
                return

            if current == DomeStatus.CLOSING:
                self.log.debug("Dome is already closing.")
                return

            if current == DomeStatus.UNKNOWN:
                self.log.warning("Dome is in an unknown status. Stopping and closing.")
                await self.stop()

            self.log.info("Closing the dome ...")
            await self._move(
                open=False,
                park=park,
                retry=retry,
            )

    async def stop(self):
        """Stops the dome."""

        await self.gort.enclosure.stop()

        status = await self.status()
        self._moving = bool(status & DomeStatus.MOVING)

        if self._moving:
            raise GortError("Dome is still moving after a stop command.")

    async def startup(self):
        """Runs the startup sequence."""

        async with self._action_lock:
            self._moving = True

            self.log.info("Starting the dome startup sequence.")
            await self.gort.startup(open_enclosure=False, focus=False)

            # Now we manually open. We do not focus here since that's handled
            # by the observer module.
            await self.open()

    async def shutdown(self, retry: bool = False, force: bool = False):
        """Runs the shutdown sequence."""

        is_closing = await self.is_closing()
        if is_closing and not force:
            return

        async with self._action_lock:
            self._moving = True

            self.log.info("Running the shutdown sequence.")

            async with GatheringTaskGroup() as group:
                self.log.info("Turning off all lamps.")
                group.create_task(self.gort.nps.calib.all_off())

                self.log.info("Making sure guiders are idle.")
                group.create_task(self.gort.guiders.stop())

                self.log.info("Closing the dome.")
                group.create_task(self.close(retry=retry))

            self.log.info("Parking telescopes for the night.")
            await self.gort.telescopes.park()
