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

        self._move_lock = asyncio.Lock()
        self.locked: bool = False  # Prevents the dome from operating.

    def reset(self):
        """Resets the dome lock."""

        self.locked = False

    @Retrier(max_attempts=3, delay=1)
    async def status(self, force: bool = False):
        """Returns the status of the dome.

        Parameters
        ----------
        force
            If :obj:`True`, will force the status check by issuing a command to
            the ``lvmecp`` actor. If :obj:`False` and the ``lvmecp`` actor model
            is being tracked by the `.Gort` instance, uses the last seen value.

        """

        if force:
            status = await self.gort.enclosure.status()
            labels = status["dome_status_labels"]
        else:
            try:
                labels = self.gort.models["lvmecp"]["dome_status_labels"].value
                if labels is None:
                    raise ValueError("dome_status_labels is None.")
            except Exception:
                return await self.status(force=True)

        labels = labels.split(",")

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

    async def is_closed(self):
        """Returns True if the dome is closed."""

        status = await self.status()

        if status & DomeStatus.CLOSED:
            return True

        return False

    async def is_closing(self):
        """Returns True if the dome is closed or closing."""

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
                await self.gort.enclosure.close(park_telescopes=park)

        except Exception as err:
            if open or not retry_on_close:
                # Do not retry if opening failed.
                raise

            # If closing, try a second time with the overcurrent mode
            # and without parking the telescopes.
            await self.overwatcher.notify(
                "The dome has failed to close. Retrying in overcurrent mode. "
                "The original error was:",
                level="error",
                error=err,
            )

            await asyncio.sleep(5)  # Wait a bit to give the PLC time to clear.
            await self.gort.enclosure.close(
                park_telescopes=park,
                # With force=True will try to park the telescopes
                # if park=True but will try to close if parking fails.
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

        if self.locked:
            raise GortError("Dome is locked. Cannot open/close.")

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

            await self.overwatcher.shutdown(
                "Dome failed to open/close. Disabling the overwatcher "
                "and locking the dome. No further attempts will be made to "
                "open/close until the dome lock is reset.",
                disable_overwatcher=True,
                close_dome=False,
            )
            self.locked = True

            raise

    async def open(self, park: bool = True):
        """Opens the dome.

        Parameters
        ----------
        park
            Whether to park the telescopes before opening the dome.

        """

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
        """Closes the dome.

        Parameters
        ----------
        park
            Whether to park the telescopes before closing the dome.
        retry
            If :obj:`True`, retries closing the dome in overcurrent mode if the first
            attempt fails.

        """

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
