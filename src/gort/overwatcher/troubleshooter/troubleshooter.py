#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-11-05
# @Filename: troubleshooter.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from time import time

from typing import TYPE_CHECKING, TypedDict

from gort.enums import ErrorCode
from gort.exceptions import (
    GortError,
    TroubleshooterCriticalError,
    TroubleshooterTimeoutError,
)
from gort.tools import LogNamespace, decap

from .recipes import TroubleshooterRecipe


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


@dataclass
class TroubleModel:
    """Base model for describing a problem to troubleshoot."""

    error: GortError
    error_code: ErrorCode
    hash: int
    message: str | None = None
    handled: bool = False
    tracking_data: ErrorTrackingDict | None = None


class RecipeBook(dict[str, TroubleshooterRecipe]):
    """A dictionary of recipes."""

    def __init__(self, ts: Troubleshooter):
        recipes_sorted = sorted(
            [Recipe(ts) for Recipe in TroubleshooterRecipe.__subclasses__()],
            key=lambda x: x.priority if x.priority is not None else 1000,
        )

        for recipe in recipes_sorted:
            self[recipe.name] = recipe


class ErrorTrackingDict(TypedDict):
    hash: int
    count: int
    last_seen: float
    reset_on_success: bool


class Troubleshooter:
    """Handles troubleshooting for the Overwatcher."""

    def __init__(self, overwatcher: Overwatcher):
        self.overwatcher = overwatcher
        self.notify = overwatcher.notify

        self.recipes = RecipeBook(self)

        self.log = LogNamespace(
            self.overwatcher.gort.log,
            header=f"({self.__class__.__name__}) ",
        )

        self._event = asyncio.Event()
        self._event.set()

        self.error_tracking: dict[int, ErrorTrackingDict] = {}

    def reset(self, clear_all_tracking: bool = False):
        """Resets the troubleshooter to its initial state."""

        self._event.set()

        for hash in self.error_tracking.copy():
            error = self.error_tracking[hash]
            if error["reset_on_success"] or clear_all_tracking:
                error["count"] = 0
                error["last_seen"] = 0

    @property
    def troubleshooting(self) -> bool:
        """Returns ``True`` if the troubleshooter is currently handling an error."""

        return not self._event.is_set()

    async def wait_until_ready(self, timeout: float | None = None):
        """Blocks if the troubleshooter is handling an error until done."""

        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TroubleshooterTimeoutError("Troubleshooter timed out.")

    def create_hash(self, error: GortError) -> int:
        """Creates a hash for the error to track it."""

        root = f"{error.error_code.name}_{str(error)}"
        return int(hashlib.sha1(root.encode("utf-8")).hexdigest(), 16) % (10**8)

    async def handle(self, error: Exception | str):
        """Handles an error and tries to troubleshoot it.

        Parameters
        ----------
        error
            The error to troubleshoot. This can be an exception (usually an instance
            of :obj:`.GortError`) or a string with the error message.

        """

        if not isinstance(error, (Exception, str)):
            raise RuntimeError("error must be an exception or a string.")

        if isinstance(error, str):
            error = GortError(error, error_code=ErrorCode.UNCATEGORISED_ERROR)
        elif not isinstance(error, GortError):
            error = GortError(str(error), error_code=ErrorCode.UNCATEGORISED_ERROR)

        hash = self.create_hash(error)
        if hash in self.error_tracking:
            self.error_tracking[hash]["count"] += 1
            self.error_tracking[hash]["last_seen"] = time()
        else:
            self.error_tracking[hash] = {
                "hash": hash,
                "count": 1,
                "last_seen": time(),
                "reset_on_success": True,
            }

        error_model = TroubleModel(
            error=error,
            error_code=error.error_code,
            message=str(error),
            hash=hash,
            tracking_data=self.error_tracking[hash],
        )

        await self.notify(
            f"Troubleshooting error of type {error.error_code.name}: "
            f"{error_model.message}",
            level="warning",
        )

        try:
            self._event.clear()

            for recipe in self.recipes.values():
                if recipe == "cleanup":  # This is a last resort recipe
                    continue

                # TODO: for now the first recipe that matches is the only one that runs.
                if recipe.match(error_model):
                    await self.notify(f"Running troubleshooting recipe {recipe.name}.")

                    error_model.handled = await recipe.handle(error_model)
                    if error_model.handled:
                        break

            if error_model.handled:
                return True

            await self.notify(
                "Error could not be handled. Running clean-up recipe.",
                level="warning",
            )
            cleanup = self.recipes["cleanup"]
            await cleanup.handle(error_model)

            return True

        except TroubleshooterCriticalError as err:
            await self.notify(
                "Shutting down due to critical error while "
                f"troubleshooting: {decap(err)}",
                level="critical",
            )
            await self.overwatcher.shutdown(
                disable_overwatcher=True,
                close_dome=err.close_dome,
            )

        finally:
            self._event.set()

        return False

    async def run_cleanup(self):
        """Runs the cleanup recipe."""

        cleanup = self.recipes["cleanup"]
        await cleanup.handle()
