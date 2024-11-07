#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-11-05
# @Filename: troubleshooter.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from typing import TYPE_CHECKING

from gort.core import LogNamespace
from gort.enums import ErrorCode
from gort.exceptions import (
    GortError,
    TroubleshooterCriticalError,
    TroubleshooterTimeoutError,
)

from .recipes import TroubleshooterRecipe


if TYPE_CHECKING:
    from gort.overwatcher.overwatcher import Overwatcher


@dataclass
class TroubleModel:
    """Base model for describing a problem to troubleshoot."""

    error: GortError
    error_code: ErrorCode
    message: str | None = None
    handled: bool = False


class RecipeBook(dict[str, TroubleshooterRecipe]):
    """A dictionary of recipes."""

    def __init__(self, ts: Troubleshooter):
        recipes_sorted = sorted(
            [Recipe(ts) for Recipe in TroubleshooterRecipe.__subclasses__()],
            key=lambda x: x.priority if x.priority is not None else 1000,
        )

        for recipe in recipes_sorted:
            self[recipe.name] = recipe


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

    def reset(self):
        """Resets the troubleshooter to its initial state."""

        self._event.set()

    async def wait_until_ready(self, timeout: float | None = None):
        """Blocks if the troubleshooter is handling an error until done."""

        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TroubleshooterTimeoutError("Troubleshooter timed out.")

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

        error_model = TroubleModel(
            error=error,
            error_code=error.error_code,
            message=str(error),
        )

        await self.notify(
            f"Troubleshooting error of type {error.error_code.name}: "
            f"{error_model.message}",
            level="warning",
        )

        try:
            self._event.clear()

            for recipe in self.recipes.values():
                # TODO: for now the first recipe that matches is the only one that runs.
                if recipe.match(error_model):
                    await self.notify(f"Running troubleshooting recipe {recipe.name}.")

                    error_model.handled = await recipe.handle(error_model)
                    if error_model.handled:
                        break

            if error_model.handled:
                await self.notify("Error has been handled.")
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
                f"Shutting down due to critical error while troubleshooting: {err!r}",
                level="critical",
            )
            await self.overwatcher.shutdown(disable_overwatcher=True)

        finally:
            self._event.set()
