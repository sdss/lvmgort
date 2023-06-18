#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-13
# @Filename: nps.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING

from sauron.core import SauronDevice, SauronDeviceSet


if TYPE_CHECKING:
    from sauron import ActorReply
    from sauron.sauron import Sauron


class NPS(SauronDevice):
    """Class representing a networked power switch."""

    def __init__(self, sauron: Sauron, name: str, actor: str, **kwargs):
        super().__init__(sauron, name, actor)

        self.status = {}

    async def update_status(self):
        """Retrieves the status of the power outlet."""

        reply: ActorReply = await self.actor.commands.status()
        self.status = reply.flatten()["status"][self.name]

        return self.status

    async def on(self, outlet: str):
        """Turns an outlet on."""

        await self.actor.commands.on(outlet)
        await self.update_status()

    async def off(self, outlet: str):
        """Turns an outlet on."""

        await self.actor.commands.off(outlet)
        await self.update_status()


class NPSSet(SauronDeviceSet[NPS]):
    """A set of networked power switches."""

    __DEVICE_CLASS__ = NPS
