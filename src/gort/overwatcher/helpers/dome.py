#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-10-27
# @Filename: dome.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import enum

from typing import TYPE_CHECKING

from lvmopstools.retrier import Retrier

from sdsstools.utils import GatheringTaskGroup

from gort.exceptions import GortError


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


__all__ = ["DomeHelper", "DomeStatus"]


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

        self._action_lock = asyncio.Lock()
        self._move_lock = asyncio.Lock()

    @Retrier(max_attempts=3, delay=1)
    async def status(self):
        """Returns the status of the dome."""

        status = await self.gort.enclosure.status()
        labels = status["dome_status_labels"]

        if "MOTOR_OPENING" in labels:
            return DomeStatus.OPENING | DomeStatus.MOVING
        elif "MOTOR_CLOSING" in labels:
            return DomeStatus.CLOSING | DomeStatus.MOVING
        elif "OPEN" in labels:
            if "MOVING" in labels:
                return DomeStatus.OPENING | DomeStatus.MOVING
            return DomeStatus.OPEN
        elif "CLOSED" in labels:
            if "MOVING" in labels:
                return DomeStatus.CLOSING | DomeStatus.MOVING
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

    async def is_moving(self):
        """Returns True if the dome is moving."""

        status = await self.status()

        if status & DomeStatus.MOVING:
            return True

        return False

    async def wait_until_idle(self):
        """Waits until the dome is idle."""

        while await self.is_moving():
            await asyncio.sleep(5)

    @Retrier(max_attempts=2, delay=5)
    async def _move_with_retries(
        self,
        open: bool = False,
        park: bool = True,
        retry_without_parking: bool = False,
    ):
        """Moves the dome with retries."""

        if open:
            await self.gort.enclosure.open(park_telescopes=park)
        else:
            await self.gort.enclosure.close(
                park_telescopes=park,
                retry_without_parking=retry_without_parking,
                force=True,
            )

    async def _move(
        self,
        status: DomeStatus,
        open: bool = False,
        park: bool = True,
        retry_without_parking: bool = False,
    ):
        """Moves the dome."""

        if open and not self.overwatcher.state.safe:
            raise GortError("Cannot open the dome when conditions are unsafe.")

        is_local = await self.gort.enclosure.is_local()
        if is_local:
            raise GortError("Cannot move the dome in local mode.")

        failed: bool = False

        try:
            if status & DomeStatus.MOVING:
                self.log.warning("Dome is already moving. Stopping.")
                await self.stop()

            await self._move_with_retries(
                open=open,
                park=park,
                retry_without_parking=retry_without_parking,
            )

        except Exception:
            await asyncio.sleep(3)

            status = await self.status()

            if status & DomeStatus.MOVING:
                self.log.warning("Dome is still moving after an error. Stopping.")
                await self.stop()

            # Sometimes the open/close could fail but actually the dome is open/closed.
            if open and not (status & DomeStatus.OPEN):
                failed = True
            elif not open and not (status & DomeStatus.CLOSED):
                failed = True

            if failed:
                await self.overwatcher.notify(
                    "The dome has failed to open/close. Disabling the Overwatcher "
                    "to prevent further attempts. Please check the dome immediately, "
                    "it may be partially or fully open.",
                    level="critical",
                )
                await self.overwatcher.force_disable()
                raise

    async def open(self, park: bool = True):
        """Opens the dome."""

        async with self._move_lock:
            status = await self.status()
            if status == DomeStatus.OPEN:
                self.log.debug("Dome is already open.")
                return

            if status == DomeStatus.OPENING:
                self.log.debug("Dome is already opening.")
                return

            if status == DomeStatus.UNKNOWN:
                self.log.warning("Dome is in an unknown status. Stopping and opening.")
                await self.stop()
                status = await self.status()

            await self._move(status, open=True, park=park)

    async def close(self, park: bool = True, retry: bool = False):
        """Closes the dome."""

        async with self._move_lock:
            status = await self.status()
            if status == DomeStatus.CLOSED:
                self.log.debug("Dome is already closed.")
                return

            if status == DomeStatus.CLOSING:
                self.log.debug("Dome is already closing.")
                return

            if status == DomeStatus.UNKNOWN:
                self.log.warning("Dome is in an unknown status. Stopping and closing.")
                await self.stop()
                status = await self.status()

            await self._move(
                status,
                open=False,
                park=park,
                retry_without_parking=retry,
            )

    async def stop(self):
        """Stops the dome."""

        await self.gort.enclosure.stop()

        await asyncio.sleep(1)
        status = await self.status()

        if status & DomeStatus.MOVING:
            raise GortError("Dome is still moving after a stop command.")

    async def startup(self):
        """Runs the startup sequence."""

        async with self._action_lock:
            self.log.info("Starting the dome startup sequence.")
            await self.gort.startup(open_enclosure=False, focus=False)

            # Now we manually open. We do not focus here since that's handled
            # by the observer module.
            await self.open()

    async def shutdown(
        self,
        retry: bool = False,
        force: bool = False,
        park: bool = True,
    ):
        """Runs the shutdown sequence."""

        is_closing = await self.is_closing()
        if is_closing and not force:
            return

        async with self._action_lock:
            self.log.info("Running the shutdown sequence.")

            async with GatheringTaskGroup() as group:
                self.log.info("Turning off all lamps.")
                group.create_task(self.gort.nps.calib.all_off())

                self.log.info("Making sure guiders are idle.")
                group.create_task(self.gort.guiders.stop())

                self.log.info("Closing the dome.")
                group.create_task(self.close(retry=retry))

            self.log.info("Parking telescopes for the night.")
            if park:
                await self.gort.telescopes.park()
