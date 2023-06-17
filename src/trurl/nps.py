#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-13
# @Filename: nps.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from trurl.core import TrurlDevice, TrurlDeviceSet


if TYPE_CHECKING:
    from trurl import ActorReply
    from trurl.trurl import Trurl


class NPS(TrurlDevice):
    """Class representing a networked power switch."""

    def __init__(self, trurl: Trurl, name: str, actor: str, **kwargs):
        super().__init__(trurl, name, actor)

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


class NPSSet(TrurlDeviceSet[NPS]):
    """A set of networked power switches."""

    __DEVICE_CLASS__ = NPS
