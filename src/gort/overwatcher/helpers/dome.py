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

from typing import TYPE_CHECKING, Coroutine

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
        labels = status["dome_status_labels"].split(",")

        if "MOTOR_OPENING" in labels:
            return DomeStatus.OPENING | DomeStatus.MOVING
        elif "MOTOR_CLOSING" in labels:
            return DomeStatus.CLOSING | DomeStatus.MOVING
        elif "OPEN" in labels:
            if "MOVING" in labels:  # This probably does not happen.
                return DomeStatus.OPENING | DomeStatus.MOVING
            return DomeStatus.OPEN
        elif "CLOSED" in labels:
            if "MOVING" in labels:  # This probably does not happen.
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

    async def wait_until_idle(self, timeout: float | None = None):
        """Waits until the dome is idle."""

        elapsed: float = 0

        while await self.is_moving():
            await asyncio.sleep(2)
            elapsed += 2

            if timeout and elapsed >= timeout:
                raise GortError("Timed out waiting for the dome to become idle.")

    async def _move_with_retries(
        self,
        open: bool = False,
        park: bool = True,
        retry_on_close: bool = True,
    ):
        """Moves the dome with retries."""

        try:
            if open:
                await self.gort.enclosure.open(park_telescopes=park)
            else:
                await self.gort.enclosure.close(park_telescopes=park, force=True)

        except Exception as err:
            if open or not retry_on_close:
                # Do not retry if opening failed.
                raise

            # If closing, try a second time with the overcurrent mode
            # and without parking the telescopes.
            await self.overwatcher.notify(
                "The dome has failed to close. Retrying in overcurrent mode ",
                level="error",
                error=err,
            )

            await asyncio.sleep(5)  # Wait a bit to give the PLC time to clear.
            await self.gort.enclosure.close(
                park_telescopes=False,
                force=True,
                mode="overcurrent",
            )

    async def _move(
        self,
        status: DomeStatus,
        open: bool = False,
        park: bool = True,
        retry_on_close: bool = True,
    ):
        """Moves the dome."""

        if open and not self.overwatcher.state.safe:
            raise GortError("Cannot open the dome when conditions are unsafe.")

        if not await self.gort.enclosure.allowed_to_move():
            raise GortError("Dome found in invalid or unsafe state.")

        try:
            if status & DomeStatus.MOVING:
                self.log.warning("Dome is already moving. Stopping.")
                await self.stop()

            await self._move_with_retries(
                open=open,
                park=park,
                retry_on_close=retry_on_close,
            )

        except Exception:
            await asyncio.sleep(3)

            status = await self.status()

            if status & DomeStatus.MOVING:
                self.log.warning("Dome is still moving after an error. Stopping.")
                await self.stop()

            # Sometimes the open/close could fail but actually the dome is open/closed.
            if open and not (status & DomeStatus.OPEN):
                raise GortError("Dome is not open after a move command.")
            elif not open and not (status & DomeStatus.CLOSED):
                raise GortError("Dome is not closed after a move command.")

    async def _run_or_disable(self, coro: Coroutine):
        """Runs a coroutine or disables the overwatcher if it fails."""

        try:
            await coro
        except Exception as err:
            await self.overwatcher.notify(
                "The dome has failed to open/close. Disabling the Overwatcher "
                "to prevent further attempts. Please check the dome immediately, "
                "it may be partially or fully open.",
                level="critical",
                error=err,
            )

            # Release the lock here.
            if self._move_lock.locked():
                self._move_lock.release()

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
                self.log.debug("Dome is already opening. Waiting ...")
                await self.wait_until_idle(timeout=250)
                return

            if status == DomeStatus.UNKNOWN:
                self.log.warning("Dome is in an unknown status. Stopping and opening.")
                await self.stop()
                status = await self.status()

            await self._run_or_disable(self._move(status, open=True, park=park))

    async def close(self, park: bool = True, retry: bool = True):
        """Closes the dome."""

        async with self._move_lock:
            status = await self.status()
            if status == DomeStatus.CLOSED:
                self.log.debug("Dome is already closed.")
                return

            if status == DomeStatus.CLOSING:
                self.log.debug("Dome is already closing. Waiting ...")
                await self.wait_until_idle(timeout=250)

            if status == DomeStatus.UNKNOWN:
                self.log.warning("Dome is in an unknown status. Stopping and closing.")
                await self.stop()
                status = await self.status()

            await self._run_or_disable(
                self._move(
                    status,
                    open=False,
                    park=park,
                    retry_on_close=retry,
                )
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
