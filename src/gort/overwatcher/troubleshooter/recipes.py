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

from lvmopstools.utils import is_host_up

from gort.enums import ErrorCode
from gort.exceptions import TroubleshooterCriticalError
from gort.tools import decap, run_lvmapi_task, set_tile_status


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

    async def handle(self, error_model: TroubleModel | None = None) -> bool:
        """Runs the recipe to handle the error."""

        try:
            return await self._handle_internal(error_model=error_model)
        except TroubleshooterCriticalError:
            # Propagate this error, which will be handled by the Troubleshooter class.
            raise
        except Exception as err:
            await self.notify(
                f"Error running recipe {self.name}: {decap(err)}",
                level="error",
            )
            return False

    @abc.abstractmethod
    async def _handle_internal(self, error_model: TroubleModel | None = None) -> bool:
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

    async def _handle_internal(self, error_model: TroubleModel | None = None) -> bool:
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

    def get_camera_ips(self):
        """Returns the IPs of the AG cameras."""

        IPs: dict[str, str] = {}
        for ag in self.gort.ags.values():
            for cam, ip in ag.ips.items():
                if ip is not None:
                    IPs[f"{ag.name}-{cam}"] = ip

        return IPs

    async def ping_ag_cameras(self):
        """Pings the AG cameras to see if they are all up."""

        IPs = self.get_camera_ips()
        pings = await asyncio.gather(*[is_host_up(IP) for IP in IPs.values()])

        return {cam_name: pings[ii] for ii, cam_name in enumerate(IPs)}

    async def all_cameras_alive(self):
        """Returns True if all cameras are alive."""

        return len(await self.gort.ags.list_alive_cameras()) == self.gort.ags.n_cameras

    async def _handle_disconnected_cameras(self):
        """Handle disconnected cameras."""

        # First check if all the cameras are connected.
        # If not, for now we don't have an automatic way to recover.
        pings = await self.ping_ag_cameras()
        if all(pings.values()):
            if await self.all_cameras_alive():
                # Return false because we don't know why the acquisition failed.
                # This will cause the tile to be disabled.
                return False

            await self.notify(
                "All AG cameras ping but some are not responding status. "
                "Reconnecting cameras."
            )

            # Reconnect the cameras.
            await self.gort.guiders.stop()  # Stop the guiders to avoid conflicts.
            await self.gort.ags.reconnect()

            # Refresh the remote actors (should not be necessary).
            for ag in self.gort.ags.values():
                await ag.actor.refresh()

            if await self.all_cameras_alive():
                return True

            await self.notify("Unable to reconnect AG cameras.")
            return False  # False means the error was probably not handled

        # At this point we know that some cameras are failing. Stop any other guiders.
        # There is an issue reconnecting cameras while they are exposing, they go into
        # an "access-denied" state that can only be fixed by power cycling
        # the switch port or restarting the actor.
        await self.gort.guiders.stop()

        # Create a list of failed cameras.
        failed_cameras: list[str] = []
        for camera, ping in pings.items():
            if not ping:
                failed_cameras.append(camera)

        # Power cycle the switch ports to those cameras.
        failed_str = ", ".join(failed_cameras)
        await self.notify(
            f"Found {len(failed_cameras)} AG cameras that are down: {failed_str}. "
            "Power cycling switch ports. This will take several minutes.",
            level="warning",
        )

        # Use the API since it already includes all the logic.
        try:
            await run_lvmapi_task(
                "/macros/power_cycle_ag_cameras",
                params={"cameras": failed_cameras},
                timeout=300,
            )
        except asyncio.TimeoutError:
            raise TroubleshooterCriticalError("Power cycling AG cameras timed out.")

        await asyncio.sleep(30)

        # Check pings again. If they are not yet pinging, we have a problem.
        pings = await self.ping_ag_cameras()
        if not all(pings.values()):
            raise TroubleshooterCriticalError("Unable to reconnect AG cameras.")

        # All cameras are pinging. Check that they respond.
        if await self.all_cameras_alive():
            await self.notify("AG cameras have been power cycled and are now pinging.")
            return True

        # If they don't all respond some cameras may be in an "access-denied" state.
        # We restart the actor as a last resort.
        await self.notify(
            "AG cameras have been power cycled but some are not responding. "
            "Restarting actors as a last resort."
        )

        await self.gort.ags.restart()
        await asyncio.sleep(30)

        # Refresh the command models for all the actors.
        await asyncio.gather(*[actor.refresh() for actor in self.gort.actors.values()])

        if await self.all_cameras_alive():
            await self.notify("All AG cameras are alive and responding.")
            return True

        raise TroubleshooterCriticalError(
            "Unable to reconnect AG cameras. The AG cameras where power cycled "
            "and are now pinging but they cannot be reconnected."
        )

    async def _handle_internal(self, error_model: TroubleModel) -> bool:
        """Handle the error."""

        error = error_model.error

        # First check if all the cameras are connected and pinging.
        try:
            if await self._handle_disconnected_cameras():
                return True
        except Exception as err:
            await self.notify(
                f"Error handling disconnected cameras: {decap(err)}",
                error=err,
                level="error",
            )

        # If that didn't work, try to disable the tile. However, if this error has
        # happened several times, there must be some other problem so we shut down.
        if error_model.tracking_data and error_model.tracking_data["count"] >= 3:
            raise TroubleshooterCriticalError(
                "Acquisition failed multiple times. "
                "Finishing the observe loop and disabling the overwatcher."
            )

        tile_id: int | None = error.payload.get("tile_id", None)
        if tile_id is None:
            await self.notify(
                'Cannot disable tile without a "tile_id. '
                "Continuing observations without disabling tile.",
                level="error",
            )
        else:
            await set_tile_status(
                tile_id,
                note=f"Acquisition failed: {decap(error_model.message)}",
            )
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
