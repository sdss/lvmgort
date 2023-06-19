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

from typing import Any, ClassVar, Generic, Self, Type, TypeVar

from clu.client import AMQPClient

from gort import config, log
from gort.core import RemoteActor
from gort.tools import register_observation


__all__ = ["GortClient", "Gort"]


class GortClient(AMQPClient):
    """The main ``lvmgort`` client, used to communicate with the actor system."""

    def __init__(
        self,
        host="lvm-hub.lco.cl",
        user: str = "guest",
        password="guest",
    ):
        from gort.enclosure import Enclosure
        from gort.guider import GuiderSet
        from gort.nps import NPSSet
        from gort.spec import SpectrographSet
        from gort.telescope import TelescopeSet

        client_uuid = str(uuid.uuid4()).split("-")[1]

        super().__init__(
            f"Gort-client-{client_uuid}",
            host=host,
            user=user,
            password=password,
        )

        self.actors: dict[str, RemoteActor] = {}

        self.telescope = TelescopeSet(self, config["telescope"]["devices"])
        self.nps = NPSSet(self, config["nps"]["devices"])
        self.spec = SpectrographSet(self, config["spec"]["devices"])
        self.enclosure = Enclosure(self, name="enclosure", actor="lvmecp")
        self.guider = GuiderSet(self, config["guider"]["devices"])

    async def init(self) -> Self:
        """Initialises the client."""

        if not self.connected:
            await self.start()

        await asyncio.gather(*[ractor.init() for ractor in self.actors.values()])

        return self

    @property
    def connected(self):
        """Returns `True` if the client is connected."""

        return self.connection and self.connection.connection is not None

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


class GortDevice:
    """A gort-managed device."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        self.gort = gort
        self.name = name
        self.actor = gort.add_actor(actor)


gortDeviceType = TypeVar("gortDeviceType", bound=GortDevice)


class GortDeviceSet(dict[str, gortDeviceType], Generic[gortDeviceType]):
    """A set to gort-managed devices."""

    __DEVICE_CLASS__: ClassVar[Type[GortDevice]]

    def __init__(self, gort: GortClient, data: dict[str, dict]):
        self.gort = gort

        _dict_data = {}
        for device_name in data:
            device_data = data[device_name].copy()
            actor_name = device_data.pop("actor")
            _dict_data[device_name] = self.__DEVICE_CLASS__(
                gort,
                device_name,
                actor_name,
                **device_data,
            )

        dict.__init__(self, _dict_data)

    def __getattribute__(self, __name: str) -> Any:
        if __name in self:
            return self.__getitem__(__name)
        return super().__getattribute__(__name)

    async def _send_command_all(self, command: str, *args, **kwargs):
        """Calls a command in all the devices."""

        tasks = []
        for dev in self.values():
            actor_command = dev.actor.commands[command]
            tasks.append(actor_command(*args, **kwargs))

        return await asyncio.gather(*tasks)


class Gort(GortClient):
    """Gort's robotic functionality."""

    def __init__(self, *args, verbosity="info", **kwargs):
        super().__init__(*args, **kwargs)
        self.set_verbosity(verbosity)

    async def observe_tile(self, tile_id: int | None = None):
        """Performs all the operations necessary to observe a tile.

        Parameters
        ----------
        tile_id
            The ``tile_id`` to observe. If not provided, observes the next tile
            suggested by the scheduler.

        """

        tile_id_data = await self.telescope.goto_tile_id(tile_id)

        tile_id = tile_id_data["tile_id"]
        dither_pos = tile_id_data["dither_pos"]

        exp_tile_data = {
            "tile_id": (tile_id, "The tile_id of this observation"),
            "dpos": (dither_pos, "Dither position"),
        }
        exp_nos = await self.spec.expose(tile_data=exp_tile_data, show_progress=True)

        if len(exp_nos) < 1:
            raise ValueError("No exposures to be registered.")

        log.info("Registering observation.")
        registration_payload = {
            "dither": dither_pos,
            "tile_id": tile_id,
            "jd": tile_id_data["jd"],
            "seeing": 10,
            "standards": tile_id_data["standard_pks"],
            "skies": tile_id_data["sky_pks"],
            "exposure_no": exp_nos[0],
        }
        log.debug(f"Registration payload {registration_payload}")
        await register_observation(registration_payload)
        log.debug("Registration complete.")
