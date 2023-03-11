#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-10
# @Filename: telescope.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from lvmbrain.core import ActorReply, LVMBrain


class Telescope:
    """Class representing an LVM telescope functionality.

    Parameters
    ----------
    brain
        The `.LVMBrain` instance used to communicate with the actor system.
    name
        The name of the telescope.
    pwi_actor
        The PlaneWave mount control actor. If `None`, uses ``lvm.{name}.pwi``.
    agcam_actor
        The autoguide camera actor. If `None`, uses ``lvm.{name}.agcam``.
    agp_actor
        The autoguide actor. If `None`, uses ``lvm.{name}.agp``.

    """

    def __init__(
        self,
        brain: LVMBrain,
        name: str,
        pwi_actor: str | None = None,
        agcam_actor: str | None = None,
        agp_actor: str | None = None,
    ):
        self.name = name

        self.brain = brain

        self.pwi_actor_name = pwi_actor or f"lvm.{name}.pwi"
        self.agcam_actor_name = agcam_actor or f"lvm.{name}.agcam"
        self.agp_actor_name = agp_actor or f"lvm.{name}.agp"

        self.status = {}

    async def prepare(self):
        """Prepares the telescope class for asynchronous access."""

        await self.brain.add_actor(self.pwi_actor_name)
        # await self.brain.add_actor(self.agcam_actor)
        # await self.brain.add_actor(self.agp_actor)

        self.pwi = self.brain.actors[self.pwi_actor_name]

    async def update_status(self):
        """Retrieves the status of the telescope."""

        reply: ActorReply = await self.pwi.status()
        self.status = reply.flatten()

        return self.status

    async def is_ready(self):
        """Checks if the telescope is ready to be moved."""

        status = await self.update_status()

        is_connected = status.get("is_connected", False)
        is_enabled = status.get("is_enabled", False)

        return is_connected and is_enabled

    async def initialise(self):
        """Connects to the telescope and initialises the axes."""

        if not (await self.is_ready()):
            await self.pwi.setConnected(True)
            await self.pwi.setEnabled(True)

    async def park(self, disable=False):
        """Parks the telescope."""

        await self.initialise()
        await self.pwi.park()

        if disable:
            await self.pwi.setEnabled(False)


class TelescopeSet(SimpleNamespace):
    """A representation of a set of telescopes."""

    def __init__(self, brain: LVMBrain, names: list):
        self.names = names

        for name in names:
            setattr(self, name, Telescope(brain, name))

    def __getitem__(self, key: str):
        return getattr(self, key)

    async def prepare(self):
        """Prepares the set of telescopes for asynchronous access."""

        await asyncio.gather(*[self[tel].prepare() for tel in self.names])

    async def initialise(self):
        """Initialise all telescopes."""

        await asyncio.gather(*[self[tel].initialise() for tel in self.names])

    async def park(self, disable=False):
        """Parks the telescopes."""

        await asyncio.gather(*[self[tel].park(disable) for tel in self.names])
