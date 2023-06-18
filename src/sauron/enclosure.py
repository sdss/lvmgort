#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-18
# @Filename: enclosure.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING

from sauron import log
from sauron.core import SauronDevice


if TYPE_CHECKING:
    from sauron import ActorReply
    from sauron.sauron import Sauron


class Enclosure(SauronDevice):
    """Class representing the LVM enclosure."""

    def __init__(self, sauron: Sauron, name: str, actor: str, **kwargs):
        super().__init__(sauron, name, actor)

        self.status = {}

    async def update_status(self):
        """Retrieves the status of the power outlet."""

        reply: ActorReply = await self.actor.commands.status()
        self.status = reply.flatten()

        return self.status

    async def open(self):
        """Open the enclosure dome."""

        log.info("Opening the enclosure ...")
        await self.actor.commands.dome.commands.open()
        log.info("Enclosure is now open.")

    async def close(self):
        """Close the enclosure dome."""

        log.info("Closing the enclosure ...")
        await self.actor.commands.dome.commands.close()
        log.info("Enclosure is now closed.")

    async def stop(self):
        """Stop the enclosure dome."""

        log.info("Stoping the dome.")
        await self.actor.commands.dome.commands.stop()
