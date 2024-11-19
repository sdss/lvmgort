#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-11-05
# @Filename: recipes.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import abc
import asyncio

from typing import TYPE_CHECKING, ClassVar

from gort.enums import ErrorCode
from gort.exceptions import TroubleshooterCriticalError
from gort.tools import set_tile_status


if TYPE_CHECKING:
    from .troubleshooter import TroubleModel, Troubleshooter


__all__ = ["TroubleshooterRecipe"]


class TroubleshooterRecipe(metaclass=abc.ABCMeta):
    """Base class for troubleshooter recipes."""

    priority: ClassVar[int | None] = None
    name: ClassVar[str]

    def __init__(self, troubleshooter: Troubleshooter):
        self.troubleshooter = troubleshooter

        self.overwatcher = troubleshooter.overwatcher
        self.gort = troubleshooter.overwatcher.gort

        self.notify = troubleshooter.overwatcher.notify

        assert hasattr(self, "name"), "Recipe must have a name."

    @abc.abstractmethod
    def match(self, error_model: TroubleModel) -> bool:
        """Returns True if the recipe can handle the error."""

        raise NotImplementedError

    async def handle(self, error_model: TroubleModel) -> bool:
        """Runs the recipe to handle the error."""

        try:
            return await self._handle_internal(error_model)
        except TroubleshooterCriticalError:
            # Propagate this error, which will be handled by the Troubleshooter class.
            raise
        except Exception as err:
            await self.notify(
                f"Error running recipe {self.name}: {err!r}",
                level="error",
            )
            return False

    @abc.abstractmethod
    async def _handle_internal(self, error_model: TroubleModel) -> bool:
        """Internal implementation of the recipe."""

        raise NotImplementedError


class CleanupRecipe(TroubleshooterRecipe):
    """A recipe that cleans up after an error."""

    priority = 500
    name = "cleanup"

    def match(self, error_model: TroubleModel) -> bool:
        """Returns True if the recipe can handle the error."""

        # Always return False. This is a last resort recipe.
        return False

    async def _handle_internal(self, error_model: TroubleModel) -> bool:
        """Run the cleanup recipe."""

        await self.overwatcher.gort.cleanup(readout=False)

        return True


class AcquisitionFailedRecipe(TroubleshooterRecipe):
    """Handle acquisition failures."""

    priority = 1
    name = "acquisition_failed"

    def match(self, error_model: TroubleModel) -> bool:
        """Returns True if the recipe can handle the error."""

        if error_model.error_code == ErrorCode.ACQUISITION_FAILED:
            return True

        return False

    async def _handle_internal(self, error_model: TroubleModel) -> bool:
        """Handle the error."""

        error = error_model.error

        tile_id: int | None = error.payload.get("tile_id", None)
        if tile_id is None:
            await self.notify(
                'Cannot disable tile without a "tile_id. '
                "Continuing observations without disabling tile.",
                level="error",
            )
        else:
            await set_tile_status(tile_id, enabled=False)
            await self.notify(
                f"tile_id={tile_id} has been disabled. Continuing observations.",
                level="warning",
            )

        return True


class SchedulerFailedRecipe(TroubleshooterRecipe):
    """Handle acquisition failures."""

    priority = 1
    name = "scheduler_failed"

    def match(self, error_model: TroubleModel) -> bool:
        """Returns True if the recipe can handle the error."""

        if error_model.error_code.is_scheduler_error():
            return True

        return False

    async def _handle_internal(self, error_model: TroubleModel) -> bool:
        """Handle the error."""

        await self.notify(
            "The scheduler was not able to find a valid tile to "
            "observe. Waiting 60 seconds before trying again.",
            level="warning",
        )
        await asyncio.sleep(60)

        return True


class SpectrographNotIdleRecipe(TroubleshooterRecipe):
    """Handle acquisition failures."""

    priority = 1
    name = "spectrograph_not_idle"

    def match(self, error_model: TroubleModel) -> bool:
        """Returns True if the recipe can handle the error."""

        if error_model.error_code == ErrorCode.SECTROGRAPH_NOT_IDLE:
            return True

        return False

    async def _handle_internal(self, error_model: TroubleModel) -> bool:
        """Handle the error."""

        await self.notify("Resetting spectrographs.", level="warning")
        await self.gort.specs.reset()

        return True
