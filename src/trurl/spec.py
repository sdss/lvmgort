#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-15
# @Filename: spec.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from trurl import config


if TYPE_CHECKING:
    from trurl.core import ActorReply, RemoteActor, Trurl


class Spectrograph:
    """Class representing an LVM spectrograph functionality.

    Parameters
    ----------
    trurl
        The `.Trurl` instance used to communicate with the actor system.
    name
        The name of the spectrograph.

    """

    def __init__(
        self,
        trurl: Trurl,
        name: str,
    ):
        self.name = name
        self.trurl = trurl

        self._scp_actor_name = f"lvmscp.{name}"
        self.scp: RemoteActor | None = None

        self.status = {}

    async def prepare(self):
        """Prepares the spectrograph class for asynchronous access."""

        self.scp = await self.trurl.add_actor(self._scp_actor_name)

    async def update_status(self):
        """Retrieves the status of the telescope."""

        assert self.scp

        reply: ActorReply = await self.scp.commands.status()
        self.status = reply.flatten()

        return self.status

    async def initialise(self):
        """Connects to the telescope and initialises the axes."""

        assert self.scp

        await self.scp.commands.init()

    async def expose(self, **kwargs):
        """Exposes the spectrograph."""

        assert self.scp

        await self.scp.commands.expose(**kwargs)


class SpectrographSet:
    """A set of LVM spectrographs."""

    def __init__(self, trurl: Trurl, names: list):
        self.names = names

        for name in names:
            setattr(self, name, Spectrograph(trurl, name))

    def __getitem__(self, key: str):
        return getattr(self, key)

    async def prepare(self):
        """Prepares the set of spectrographs for asynchronous access."""

        await asyncio.gather(*[self[spec].prepare() for spec in self.names])

    async def expose(self, specs: list[str] | None = None, **kwargs):
        """Exposes the spectrographs."""

        if specs is None:
            specs = self.names.copy()

        next_exposure_number_path = config["specs"]["nextExposureNumber"]
        with open(next_exposure_number_path, "r") as fd:
            data = fd.read().strip()
            seqno = int(data) if data != "" else 1

        await asyncio.gather(
            *[self[spec].expose(seqno=seqno, **kwargs) for spec in specs]
        )
