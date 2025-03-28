#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-13
# @Filename: nps.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING

from gort.devices.core import GortDevice, GortDeviceSet


if TYPE_CHECKING:
    from gort import ActorReply
    from gort.gort import Gort


__all__ = ["NPS", "NPSSet"]


class NPS(GortDevice):
    """Class representing a networked power switch."""

    def __init__(self, gort: Gort, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

    async def status(self, outlet: int | str | None = None):
        """Retrieves the status of the power outlet."""

        if outlet is None:
            reply: ActorReply = await self.actor.commands.status(n_retries=3)
            return reply.flatten()["outlets"]
        else:
            reply: ActorReply = await self.actor.commands.status(outlet, n_retries=3)
            return reply.flatten()["outlet_info"]

    async def on(self, outlet: str):
        """Turns an outlet on."""

        await self.actor.commands.on(outlet, n_retries=3)

    async def off(self, outlet: str):
        """Turns an outlet on."""

        await self.actor.commands.off(outlet, n_retries=3)

    async def all_off(self):
        """Turns off all the outlets."""

        self.write_to_log("Turning off all outlets.")
        await self.actor.commands.all_off(n_retries=3)


class NPSSet(GortDeviceSet[NPS]):
    """A set of networked power switches."""

    __DEVICE_CLASS__ = NPS
    __DEPLOYMENTS__ = ["lvmnps"]
