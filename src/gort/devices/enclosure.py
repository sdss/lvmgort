#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-18
# @Filename: enclosure.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING

from gort import config
from gort.exceptions import GortEnclosureError
from gort.gort import GortDevice


if TYPE_CHECKING:
    from gort import ActorReply
    from gort.gort import GortClient


__all__ = ["Enclosure"]


class Enclosure(GortDevice):
    """Class representing the LVM enclosure."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

    async def status(self):
        """Retrieves the status of the power outlet."""

        reply: ActorReply = await self.actor.commands.status()

        return reply.flatten()

    async def open(self):
        """Open the enclosure dome."""

        self.write_to_log("Opening the enclosure ...", level="info")
        await self.actor.commands.dome.commands.open()
        self.write_to_log("Enclosure is now open.", level="info")

    async def close(self, force: bool = False):
        """Close the enclosure dome."""

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
        if config["enclosure"].get("bypass_local_mode", False) is True:
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