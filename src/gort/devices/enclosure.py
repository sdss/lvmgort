#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-18
# @Filename: enclosure.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from gort.exceptions import GortEnclosureError
from gort.gort import GortDevice, GortDeviceSet


if TYPE_CHECKING:
    from gort import ActorReply
    from gort.gort import GortClient


__all__ = ["Enclosure"]


class Enclosure(GortDevice):
    """Class representing the LVM enclosure."""

    __DEPLOYMENTS__ = ["lvmecp"]

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

    async def restart(self):
        """Restarts the ``lvmecp`` deployment."""

        # HACK: GortDevice has the everything that is needed to run restart.
        await GortDeviceSet.restart(self)  # type: ignore

    async def status(self):
        """Retrieves the status of the power outlet."""

        reply: ActorReply = await self.actor.commands.status()

        return reply.flatten()

    async def _prepare_telescopes(self):
        """Moves telescopes to park position before opening/closing the enclosure."""

        telescopes = list(self.gort.telescopes)
        is_parked = await asyncio.gather(
            *[self.gort.telescopes[tel].is_parked() for tel in telescopes]
        )

        if all(is_parked):
            return True

        self.write_to_log(
            "Moving telescopes to park before operating the dome.",
            "warning",
        )

        park_coros = [
            self.gort.telescopes[tel].goto_named_position("park")
            for itel, tel in enumerate(telescopes)
            if not is_parked[itel]
        ]

        await asyncio.gather(*park_coros)

    async def open(self, park_telescopes: bool = True):
        """Open the enclosure dome.

        Parameters
        ----------
        park_telescopes
            Move the telescopes to the park position before opening the
            enclosure to prevent dust or other debris falling on them.

        """

        if park_telescopes:
            await self._prepare_telescopes()

        self.write_to_log("Opening the enclosure ...", level="info")
        await self.actor.commands.dome.commands.open()
        self.write_to_log("Enclosure is now open.", level="info")

    async def close(self, park_telescopes: bool = True, force: bool = False):
        """Close the enclosure dome.

        Parameters
        ----------
        park_telescopes
            Move the telescopes to the park position before closing the
            enclosure to prevent dust or other debris falling on them.
        force
            Tries to closes the dome even if the system believes it is
            already closed.

        """

        if park_telescopes:
            try:
                await self._prepare_telescopes()
            except Exception as err:
                self.write_to_log(
                    f"Failed determining the status of the telescopes: {err}",
                    "warning",
                )
                if force is False:
                    raise GortEnclosureError(
                        "Not closing without knowing where the telescopes are. "
                        "If you really need to close call again with force=True."
                    )
                else:
                    self.write_to_log("Closing anyway because force=True", "warning")

        self.write_to_log("Closing the enclosure ...", level="info")
        await self.actor.commands.dome.commands.close(force=force)
        self.write_to_log("Enclosure is now closed.", level="info")

    async def stop(self):
        """Stop the enclosure dome."""

        self.write_to_log("Stoping the dome.", level="info")
        await self.actor.commands.dome.commands.stop()

    async def is_local(self):
        """Returns `True` if the enclosure is in local mode."""

        status = await self.status()
        safety_status_labels = status.get("safety_status_labels", None)
        if safety_status_labels is None:
            raise GortEnclosureError(
                "Cannot determine if enclosure is in local mode.",
                error_code=501,
            )

        # This should generally not be on, but it's useful as a way of disabling
        # the local mode when the lock or door are not working.
        if self.gort.config["enclosure"].get("bypass_local_mode", False) is True:
            return False

        return "LOCAL" in safety_status_labels

    async def get_door_status(self):
        """Returns the status of the door and lock."""

        status = await self.status()
        safety_status_labels = status.get("safety_status_labels", None)
        if safety_status_labels is None:
            raise GortEnclosureError(
                "Cannot determine door status.",
                error_code=502,
            )

        reply = {
            "door_closed": "DOOR_CLOSED" in safety_status_labels,
            "door_locked": "DOOR_LOCKED" in safety_status_labels,
            "local": await self.is_local(),
        }

        return reply
