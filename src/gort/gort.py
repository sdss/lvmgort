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
from gort.kubernetes import Kubernetes
from gort.tools import register_observation


__all__ = ["GortClient", "Gort", "GortDeviceSet", "GortDevice"]


class GortClient(AMQPClient):
    """The main ``gort`` client, used to communicate with the actor system.

    A subclass of `~clu.client.AMQPClient` with defaults for host and logging,
    it loads the `.GortDeviceSet` and `.RemoteActor` instances for the LVM system.

    Parameters
    ----------
    host
        The host on which the RabbitMQ exchange is running.
    port
        The port on which the RabbitMQ exchange listens to connections.
    user
        The user to connect to the exchange.
    password
        The password to connect to the exchange.

    """

    def __init__(
        self,
        host: str = "lvm-hub.lco.cl",
        port: int = 5672,
        user: str = "guest",
        password: str = "guest",
    ):
        from gort.ag import AGSet
        from gort.enclosure import Enclosure
        from gort.guider import GuiderSet
        from gort.nps import NPSSet
        from gort.spec import SpectrographSet
        from gort.telescope import TelescopeSet

        client_uuid = str(uuid.uuid4()).split("-")[1]

        super().__init__(
            f"Gort-client-{client_uuid}",
            host=host,
            port=port,
            user=user,
            password=password,
            log=log,
        )

        # Reset verbosity.
        self.set_verbosity()

        self.actors: dict[str, RemoteActor] = {}

        self.ags = AGSet(self, config["ags"]["devices"])
        self.guiders = GuiderSet(self, config["guiders"]["devices"])
        self.telescopes = TelescopeSet(self, config["telescopes"]["devices"])
        self.nps = NPSSet(self, config["nps"]["devices"])
        self.specs = SpectrographSet(self, config["specs"]["devices"])
        self.enclosure = Enclosure(self, name="enclosure", actor="lvmecp")

    async def init(self) -> Self:
        """Initialises the client.

        Returns
        -------
        object
            The same instance of `.GortClient` after initialisation.

        """

        if not self.connected:
            await self.start()

        await asyncio.gather(*[ractor.init() for ractor in self.actors.values()])

        return self

    @property
    def connected(self):
        """Returns `True` if the client is connected."""

        return self.connection and self.connection.connection is not None

    def add_actor(self, actor: str):
        """Adds an actor to the programmatic API.

        Parameters
        ----------
        actor
            The name of the actor to add.

        """

        if actor not in self.actors:
            self.actors[actor] = RemoteActor(self, actor)

        return self.actors[actor]

    def set_verbosity(self, verbosity: str | int | None = None):
        """Sets the level of verbosity to ``debug``, ``info``, or ``warning``.

        Parameters
        ----------
        verbosity
            The level of verbosity. Can be a string level name, an integer, or `None`,
            in which case the default verbosity will be used.

        """

        verbosity = verbosity or "warning"

        if isinstance(verbosity, int):
            self.log.sh.setLevel(verbosity)
            return

        verbosity = verbosity.lower()
        if verbosity not in ["debug", "info", "warning"]:
            raise ValueError("Invalid verbosity value.")

        self.log.sh.setLevel(logging.getLevelName(verbosity.upper()))


GortDeviceType = TypeVar("GortDeviceType", bound="GortDevice")


class GortDeviceSet(dict[str, GortDeviceType], Generic[GortDeviceType]):
    """A set to gort-managed devices.

    Devices can be accessed as items of the `.GortDeviceSet` dictionary
    or ussing dot notation, as attributes.

    Parameters
    ----------
    gort
        The `.GortClient` instance.
    data
        A mapping of device to device info. Each device must at least include
        an ``actor`` key with the actor to use to communicated with the device.
        Any other information is passed to the `.GortDevice` on instantiation.
    kwargs
        Other keyword arguments to pass wo the device class.

    """

    __DEVICE_CLASS__: ClassVar[Type["GortDevice"]]

    def __init__(self, gort: GortClient, data: dict[str, dict], **kwargs):
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
                **kwargs,
            )

        dict.__init__(self, _dict_data)

    def __getattribute__(self, __name: str) -> Any:
        if __name in self:
            return self.__getitem__(__name)
        return super().__getattribute__(__name)

    async def call_device_method(self, method: Callable, *args, **kwargs):
        """Calls a method in each one of the devices.

        Parameters
        ----------
        method
            The method to call. This must be the abstract class method,
            not the method from an instantiated object.
        args,kwargs
            Arguments to pass to the method.

        """

        if not callable(method):
            raise GortError("Method is not callable.")

        if hasattr(method, "__self__"):
            # This is a bound method, so let's get the class method.
            method = method.__func__

        if not hasattr(self.__DEVICE_CLASS__, method.__name__):
            raise GortError("Method does not belong to this class devices.")

        devices = self.values()

        return await asyncio.gather(*[method(dev, *args, **kwargs) for dev in devices])

    async def _send_command_all(self, command: str, *args, **kwargs):
        """Sends a command to all the devices.

        Parameters
        ----------
        command
            The command to call.
        args, kwargs
            Arguments to pass to the `.RemoteCommand`.

        """

        tasks = []
        for dev in self.values():
            actor_command = dev.actor.commands[command]
            tasks.append(actor_command(*args, **kwargs))

        return await asyncio.gather(*tasks)

    def write_to_log(
        self,
        message: str,
        level: str = "debug",
        header: str | None = None,
    ):
        """Writes a message to the log with a custom header.

        Parameters
        ----------
        message
            The message to log.
        level
            The level to use for logging: ``'debug'``, ``'info'``, ``'warning'``, or
            ``'error'``.
        header
            The header to prepend to the message. By default uses the class name.

        """

        if header is None:
            header = f"({self.__class__.__name__}) "

        message = f"{header}{message}"

        level = logging.getLevelName(level.upper())
        assert isinstance(level, int)

        self.gort.log.log(level, message)


class GortDevice:
    """A gort-managed device.

    Parameters
    ----------
    gort
        The `.GortClient` instance.
    name
        The name of the device.
    actor
        The name of the actor used to interface with this device. The actor is
        added to the list of `.RemoteActor` in the `.GortClient`.


    """

    def __init__(self, gort: GortClient, name: str, actor: str):
        self.gort = gort
        self.name = name
        self.actor = gort.add_actor(actor)

    def write_to_log(
        self,
        message: str,
        level: str = "debug",
        header: str | None = None,
    ):
        """Writes a message to the log with a custom header.

        Parameters
        ----------
        message
            The message to log.
        level
            The level to use for logging: ``'debug'``, ``'info'``, ``'warning'``, or
            ``'error'``.
        header
            The header to prepend to the message. By default uses the device name.

        """

        if header is None:
            header = f"({self.name}) "

        message = f"{header}{message}"

        level = logging.getLevelName(level.upper())
        assert isinstance(level, int)

        self.gort.log.log(level, message)

    def print_reply(self, reply):
        """Outputs command replies."""

        if reply.body:
            self.write_to_log(str(reply.body))


class Gort(GortClient):
    """Gort's robotic functionality.

    `.Gort` is subclass of `.GortClient` that implements higher-level robotic
    functionality. This is the class a user will normally instantiate and
    interact with.

    Parameters
    ----------
    args, kwargs
        Arguments to pass to `.GortClient`.
    verbosity
        The level of logging verbosity.

    """

    def __init__(self, *args, verbosity: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)

        try:
            self.kubernetes = Kubernetes()
        except Exception:
            self.log.warning(
                "Gort cannot access the Kubernets cluster. "
                "The Kubernetes module won't be available."
            )
            self.kubernetes = None

        if verbosity:
            self.set_verbosity(verbosity)

    async def emergency_close(self):
        """Parks and closes the telescopes."""

        tasks = []
        tasks.append(self.telescopes.park(disable=True))
        tasks.append(self.enclosure.close())

        self.log.warning("Closing and parking telescopes.")
        await asyncio.gather(*tasks)

    async def observe_tile(
        self,
        tile_id: int | None = None,
        ra: float | None = None,
        dec: float | None = None,
        expose: bool = True,
    ):
        """Performs all the operations necessary to observe a tile.

        Parameters
        ----------
        tile_id
            The ``tile_id`` to observe. If not provided, observes the next tile
            suggested by the scheduler.
        ra,dec
            The RA and Dec where to point the science telescopes. The other
            telescopes are pointed to calibrators that fit the science pointing.
            Cannot be used with ``tile_id``.
        expose
            Exposes the spectrographs. Otherwise only slews.

        """

        tile_id_data = await self.telescopes.goto_tile_id(
            tile_id=tile_id,
            ra=ra,
            dec=dec,
        )

        if expose:
            tile_id = tile_id_data["tile_id"]
            dither_pos = tile_id_data["dither_pos"]

            exp_tile_data = {
                "tile_id": (tile_id, "The tile_id of this observation"),
                "dpos": (dither_pos, "Dither position"),
            }
            exp_nos = await self.specs.expose(
                tile_data=exp_tile_data,
                show_progress=True,
            )

            if len(exp_nos) < 1:
                raise ValueError("No exposures to be registered.")

            self.log.info("Registering observation.")
            registration_payload = {
                "dither": dither_pos,
                "tile_id": tile_id,
                "jd": tile_id_data["jd"],
                "seeing": 10,
                "standards": tile_id_data["standard_pks"],
                "skies": tile_id_data["sky_pks"],
                "exposure_no": exp_nos[0],
            }
            self.log.debug(f"Registration payload {registration_payload}")
            await register_observation(registration_payload)
            self.log.debug("Registration complete.")
