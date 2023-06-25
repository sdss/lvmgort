#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-18
# @Filename: enclosure.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING

from gort.exceptions import GortEnclosureError
from gort.gort import GortDevice


if TYPE_CHECKING:
    from gort import ActorReply
    from gort.gort import GortClient


class Enclosure(GortDevice):
    """Class representing the LVM enclosure."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

        self.status = {}

    async def update_status(self):
        """Retrieves the status of the power outlet."""

        reply: ActorReply = await self.actor.commands.status()
        self.status = reply.flatten()

        return self.status

    async def open(self):
        """Open the enclosure dome."""

        self.write_to_log("Opening the enclosure ...", level="info")
        await self.actor.commands.dome.commands.open()
        self.write_to_log("Enclosure is now open.", level="info")

    async def close(self):
        """Close the enclosure dome."""

        self.write_to_log("Closing the enclosure ...", level="info")
        await self.actor.commands.dome.commands.close()
        self.write_to_log("Enclosure is now closed.", level="info")

    async def stop(self):
        """Stop the enclosure dome."""

        self.write_to_log("Stoping the dome.", level="info")
        await self.actor.commands.dome.commands.stop()

    async def is_local(self):
        """Returns `True` if the enclosure is in local mode."""

        await self.update_status()
        safety_status_labels = self.status.get("safety_status_labels", None)
        if safety_status_labels is None:
            raise GortEnclosureError("Cannot determine if enclosure is in local mode.")

        return "LOCAL" in safety_status_labels
