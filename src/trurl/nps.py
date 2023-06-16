#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-13
# @Filename: nps.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from trurl.core import ActorReply, Trurl


class NPS:
    """Class representing a networked power switch.

    Parameters
    ----------
    trurl
        The `.Trurl` instance used to communicate with the actor system.
    name
        The name of the power switch.

    """

    def __init__(self, trurl: Trurl, name: str):
        self.name = name
        self.trurl = trurl

        self.status = {}

    async def prepare(self):
        """Prepares the NPS class for asynchronous access."""

        await self.trurl.add_actor(f"lvmnps.{self.name}")

        self.nps = self.trurl.actors[f"lvmnps.{self.name}"]
        await self.update_status()

    async def update_status(self):
        """Retrieves the status of the power outlet."""

        reply: ActorReply = await self.nps.commands.status()
        self.status = reply.flatten()["status"][self.name]

        return self.status

    async def on(self, outlet: str):
        """Turns an outlet on."""

        await self.nps.commands.on(outlet)
        await self.update_status()

    async def off(self, outlet: str):
        """Turns an outlet on."""

        await self.nps.commands.off(outlet)
        await self.update_status()


class NPSSet(SimpleNamespace):
    """A set of networked power switches."""

    def __init__(self, trurl: Trurl, names: list):
        self.names = names

        for name in names:
            setattr(self, name, NPS(trurl, name))

    def __getitem__(self, key: str):
        return getattr(self, key)

    async def prepare(self):
        """Prepares the set of NPSs for asynchronous access."""

        await asyncio.gather(*[self[nps].prepare() for nps in self.names])
