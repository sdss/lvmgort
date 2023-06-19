
#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-16
# @Filename: gort.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

import asyncio
import logging
import uuid

from typing import Self

from clu.client import AMQPClient

from gort import config, log
from gort.core import RemoteActor
from gort.enclosure import Enclosure
from gort.guider import GuiderSet
from gort.nps import NPSSet
from gort.spec import SpectrographSet
from gort.telescope import TelescopeSet


__all__ = ["Gort"]


class Gort:
    """The main ``lvmgort`` client, used to communicate with the actor system."""

    def __init__(
        self,
        client: AMQPClient | None = None,
        host="lvm-hub.lco.cl",
        user: str = "guest",
        password="guest",
    ):
        if client:
            self.client = client
        else:
            client_uuid = str(uuid.uuid4()).split("-")[1]

            self.client = AMQPClient(
                f"Gort-client-{client_uuid}",
                host=host,
                user=user,
                password=password,
            )

        self.actors: dict[str, RemoteActor] = {}

        self.telescopes = TelescopeSet(self, config["telescopes"]["devices"])
        self.nps = NPSSet(self, config["nps"]["devices"])
        self.specs = SpectrographSet(self, config["specs"]["devices"])
        self.enclosure = Enclosure(self, name="enclosure", actor="lvmecp")
        self.guiders = GuiderSet(self, config["guiders"]["devices"])

    async def init(self) -> Self:
        """Initialises the client."""

        if not self.connected:
            await self.client.start()

        await asyncio.gather(*[ractor.init() for ractor in self.actors.values()])

        return self

    @property
    def connected(self):
        """Returns `True` if the client is connected."""

        return self.client.connection and self.client.connection.connection is not None

    def add_actor(self, actor: str):
        """Adds an actor to the programmatic API."""

        if actor not in self.actors:
            self.actors[actor] = RemoteActor(self, actor)

        return self.actors[actor]

    def set_verbosity(self, verbosity: str | int = "info"):
        """Sets the level of verbosity to ``debug``, ``info``, or ``warning``."""

        if isinstance(verbosity, int):
            log.sh.setLevel(verbosity)
            return

        verbosity = verbosity.lower()
        if verbosity not in ["debug", "info", "warning"]:
            raise ValueError("Invalid verbosity value.")

        log.sh.setLevel(logging.getLevelName(verbosity.upper()))
